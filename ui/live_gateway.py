"""Serve updated UI assets beside an existing controller without restarting it.

The existing controller remains the only owner of its jobs. A separate updated
controller owns a disjoint runs directory. This gateway forwards authenticated
requests over loopback and augments legacy response copies only.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import partial
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
from urllib.parse import parse_qs, urlencode, urlsplit


HOST = '127.0.0.1'
UI = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 100_000
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
GET_TIMEOUT_SECONDS = 15
POST_TIMEOUT_SECONDS = 600
BATCH_TIMEOUT_SECONDS = 1800
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
          '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
GET_ROUTES = {'/batches': set(), '/jobs': set(), '/state': {'job', 'after'},
              '/transcript': {'job'}, '/approach-graph': {'job'},
              '/memory': {'job', 'file', 'version'},
              '/download-tex': {'job'}, '/download-pdf': {'job'}}
CREATE_ROUTES = {'/review', '/direct', '/direct-batch', '/finalize'}
CONTROL_ROUTES = {'/delete-job', '/resume', '/start-research-audit', '/resume-critic',
                  '/resume-checkpoint', '/continue-stopped', '/approve',
                  '/set-author-time-limit', '/set-research-audits', '/steer-author',
                  '/stop', '/pause', '/reset', '/clear-trace'}
# API models belong to the updated controller's installation, not to a job:
# GET lists them; the POST routes save, delete and test one.
API_MODEL_ROUTES = {'/api-models', '/api-models/delete', '/api-models/test', '/api-models/discover'}


class GatewayError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Upstream:
    port: int
    token: str = field(repr=False)

    def __post_init__(self):
        if (type(self.port) is not int or not 1 <= self.port <= 65535
                or not isinstance(self.token, str) or not self.token
                or any(ord(char) < 33 or ord(char) > 126 for char in self.token)):
            raise ValueError('Invalid local controller connection.')

    @property
    def origin(self):
        return f'http://{HOST}:{self.port}'

    @classmethod
    def from_launch_status(cls, path):
        """Read a trusted local launch record, never browser-supplied routing."""
        try:
            value = json.loads(Path(path).read_text())
            url = urlsplit(value['url'])
            if (url.scheme != 'http' or url.hostname != HOST or not url.port
                    or url.username is not None or url.password is not None
                    or url.path not in ('', '/') or url.query or not url.fragment):
                raise ValueError
            return cls(url.port, url.fragment)
        except (OSError, KeyError, TypeError, ValueError):
            raise ValueError('Cannot read a valid loopback controller launch record.') from None

    def request(self, method, path, body=None):
        # Historical job creation performs sandbox preflight before responding.
        # A short POST timeout can leave a started job behind an ambiguous error.
        timeout = (BATCH_TIMEOUT_SECONDS if method == 'POST' and path == '/direct-batch'
                   else POST_TIMEOUT_SECONDS if method == 'POST' else GET_TIMEOUT_SECONDS)
        connection = http.client.HTTPConnection(HOST, self.port, timeout=timeout)
        headers = {'X-TCS-Prover-Token': self.token, 'Origin': self.origin}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise GatewayError('Controller response exceeds the gateway limit.')
            # Redirects are not followed and cannot reveal controller credentials.
            if 300 <= response.status < 400:
                raise GatewayError('Unexpected controller redirect.')
            return (response.status, response.getheader('Content-Type', 'application/json'),
                    response.getheader('Content-Disposition'), payload)
        except (OSError, http.client.HTTPException):
            message = ('The controller did not confirm this action. Refresh job status before retrying.'
                       if method == 'POST' else 'The local controller is temporarily unavailable.')
            raise GatewayError(message) from None
        finally:
            connection.close()

    def jobs(self):
        status, _, _, raw = self.request('GET', '/jobs')
        try:
            value = json.loads(raw)
            jobs = value['jobs']
            if status != 200 or not isinstance(jobs, list):
                raise ValueError
            identifiers = [row['runId'] for row in jobs if isinstance(row, dict)]
            if (len(identifiers) != len(jobs) or any(not isinstance(key, str) or not key for key in identifiers)
                    or len(identifiers) != len(set(identifiers))):
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError):
            raise GatewayError('The local controller returned an invalid job list.') from None


def contains_policy_change(value):
    if isinstance(value, dict):
        return any('retrieval' in key.casefold() or 'cutoff' in key.casefold()
                   or contains_policy_change(child) for key, child in value.items())
    if isinstance(value, list):
        return any(contains_policy_change(child) for child in value)
    return False


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second gateway bind a port that is in use.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, address, *, legacy, current, augment_legacy, ui_directory=UI,
                 catalog=None, legacy_batch=None, current_batch=None, token=None):
        if address[0] != HOST:
            raise ValueError('The UI gateway must bind to loopback.')
        if legacy.port == current.port:
            raise ValueError('Existing and new controllers must be distinct.')
        self.legacy, self.current = legacy, current
        self.augment_legacy, self.ui_directory = augment_legacy, Path(ui_directory)
        self.catalog = catalog
        self.legacy_batch, self.current_batch = legacy_batch, current_batch
        if token is not None:
            Upstream(1, token)  # Apply the same strict token validation.
        super().__init__(address, GatewayHandler)
        self.token = token or secrets.token_urlsafe(32)
        self.origin = f'http://{HOST}:{self.server_port}'

    def job_lists(self):
        # Membership is fetched from the authoritative processes for every
        # routing decision. No cached job identifier may select the wrong owner.
        legacy, current = self.legacy.jobs(), self.current.jobs()
        old_ids = {row['runId'] for row in legacy['jobs']}
        new_ids = {row['runId'] for row in current['jobs']}
        if old_ids & new_ids:
            raise GatewayError('Job identifiers overlap between controllers.', 409)
        return legacy, current, old_ids, new_ids

    def owner(self, run_id, lists=None):
        _, _, old_ids, new_ids = lists or self.job_lists()
        if run_id in old_ids:
            return self.legacy
        if run_id in new_ids:
            return self.current
        raise GatewayError('Unknown job.', 404)

    def combined_jobs(self, lists=None):
        legacy, current, _, _ = lists or self.job_lists()
        augmented = self.augment_legacy(legacy, is_job_list=True)
        jobs = ([{**row, **({'batchId': self.legacy_batch} if self.legacy_batch else {})}
                 for row in augmented['jobs']]
                + [{**row, **({'batchId': self.current_batch} if self.current_batch else {})}
                   for row in current['jobs']])
        return {'jobs': sorted(jobs, key=lambda row: row.get('startedAt') or '', reverse=True)}

    def batch_upstream(self, batch):
        if batch and batch == self.legacy_batch:
            return self.legacy
        if batch and batch == self.current_batch:
            return self.current
        return None

    def batch_cards(self):
        cards = self.catalog.list_batches() if self.catalog else []
        legacy, current = self.legacy.jobs(), self.current.jobs()
        for ident, rows in ((self.legacy_batch, legacy['jobs']),
                            (self.current_batch, current['jobs'])):
            if not ident:
                continue
            card = next((item for item in cards if item['id'] == ident), None)
            if card is None:
                if not rows:
                    continue
                card = {'id': ident, 'name': ident, 'folder': ident}
                cards.append(card)
            counts = {}
            for row in rows:
                phase = row.get('phase') or 'unknown'
                counts[phase] = counts.get(phase, 0) + 1
            starts = [row['startedAt'] for row in rows if row.get('startedAt')]
            card.update(jobCount=len(rows), counts=counts, readOnly=False,
                        current=ident == self.legacy_batch,
                        startedAt=min(starts) if starts else card.get('startedAt', ''))
        cards.sort(key=lambda card: (bool(card.get('current')), card.get('startedAt') or ''), reverse=True)
        return {'batches': cards, 'currentBatch': self.legacy_batch}


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, status=200, content_type='application/json', disposition=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        origin = self.headers.get('Origin')
        token = self.headers.get('X-TCS-Prover-Token', '')
        return (self.headers.get('Host') == f'{HOST}:{self.server.server_port}'
                and (not origin or origin == self.server.origin)
                and bool(token) and secrets.compare_digest(token, self.server.token))

    def parsed_request(self):
        request = urlsplit(self.path)
        if request.scheme or request.netloc or request.fragment:
            raise GatewayError('Invalid local request target.', 400)
        query = parse_qs(request.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise GatewayError('Duplicate request parameters are not allowed.', 400)
        return request.path, {key: values[0] for key, values in query.items()}

    @staticmethod
    def request_path(path, query):
        return path + ('?' + urlencode(query) if query else '')

    def forward_api_models(self, method, path, body=None):
        """Pass one API model request to the updated controller unchanged.

        Its responses never contain a key; the legacy controller has no API models.
        """
        status, content_type, disposition, raw = self.server.current.request(method, path, body)
        self.send(raw, status, content_type, disposition)

    def forward(self, upstream, method, path, query, body=None, batch=None):
        status, content_type, disposition, raw = upstream.request(
            method, self.request_path(path, query), body)
        if status == 200 and content_type.startswith('application/json'):
            try:
                value = json.loads(raw)
            except ValueError:
                raise GatewayError('The local controller returned invalid JSON.') from None
            if isinstance(value, dict):
                if 'jobs' in value:
                    # A create/delete response also refreshes both sets of jobs.
                    if batch:
                        if upstream is self.server.legacy:
                            value = self.server.augment_legacy(value, is_job_list=True)
                        value['jobs'] = [{**row, 'batchId': batch} for row in value['jobs']]
                    else:
                        value['jobs'] = self.server.combined_jobs()['jobs']
                elif upstream is self.server.legacy:
                    value = self.server.augment_legacy(value, is_job_list=False)
                batch_id = batch or (self.server.legacy_batch if upstream is self.server.legacy
                                     else self.server.current_batch)
                if batch_id:
                    value['batchId'] = batch_id
                raw = json.dumps(value).encode()
        self.send(raw, status, content_type, disposition)

    def do_GET(self):
        try:
            if self.headers.get('Host') != f'{HOST}:{self.server.server_port}':
                raise GatewayError('Untrusted local host.', 403)
            path, query = self.parsed_request()
            if path in STATIC:
                # The page reads job, batch and new; theme previews a light or dark page unsaved.
                if query and (path != '/' or set(query) - {'job', 'batch', 'new', 'theme'}):
                    raise GatewayError('Not found.', 404)
                filename, kind = STATIC[path]
                return self.send((self.server.ui_directory / filename).read_bytes(), content_type=kind)
            if not self.authorized():
                raise GatewayError('Open TCS Prover from its launch URL.', 403)
            if path == '/api-models' and not query:
                return self.forward_api_models('GET', path)
            if path not in GET_ROUTES or set(query) - (GET_ROUTES[path] | {'batch'}):
                raise GatewayError('Not found.', 404)
            if path == '/batches':
                return self.send(self.server.batch_cards())
            batch = query.pop('batch', None)
            if batch is not None:
                upstream = self.server.batch_upstream(batch)
                if upstream is None:
                    if self.server.catalog is None:
                        raise GatewayError('Unknown batch.', 404)
                    try:
                        body, kind, headers = self.server.catalog.response(path, batch, query.get('job', ''), query)
                    except (ValueError, OSError):
                        raise GatewayError('Unknown batch, job, or saved artifact.', 404) from None
                    if isinstance(body, Path):
                        with self.server.catalog.open_artifact(batch, query.get('job', ''), body.name) as stream:
                            self.send_response(200)
                            self.send_header('Content-Type', kind)
                            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                            self.send_header('Cache-Control', 'no-store')
                            for name, value in headers.items():
                                self.send_header(name, value)
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile)
                        return
                    return self.send(body, content_type=kind, disposition=headers.get('Content-Disposition'))
                run_id = query.get('job', '')
                if path not in {'/jobs', '/state'} and not run_id:
                    raise GatewayError('A job is required.', 400)
                if run_id and run_id not in {row['runId'] for row in upstream.jobs()['jobs']}:
                    raise GatewayError('Unknown job in this batch.', 404)
                return self.forward(upstream, 'GET', path, query, batch=batch)
            if path == '/jobs':
                return self.send(self.server.combined_jobs())
            run_id = query.get('job', '')
            if not run_id and path != '/state':
                raise GatewayError('A job is required.', 400)
            upstream = self.server.owner(run_id) if run_id else self.server.current
            self.forward(upstream, 'GET', path, query)
        except GatewayError as error:
            self.send({'error': str(error)}, error.status)
        except (OSError, ValueError, TypeError, KeyError):
            self.send({'error': 'The local dashboard could not complete this request.'}, 502)

    def do_POST(self):
        try:
            if not self.authorized():
                raise GatewayError('Unauthorized local request.', 403)
            path, query = self.parsed_request()
            api_models = path in API_MODEL_ROUTES
            if api_models and query:
                raise GatewayError('Not found.', 404)
            if not api_models and (path not in CREATE_ROUTES | CONTROL_ROUTES or set(query) - {'job', 'batch'}):
                raise GatewayError('Not found.', 404)
            batch = query.pop('batch', None)
            batch_upstream = self.server.batch_upstream(batch)
            if batch is not None and batch_upstream is None:
                raise GatewayError('Saved batches are read-only.', 403)
            if self.headers.get_content_type() != 'application/json':
                raise GatewayError('Expected a JSON request.', 400)
            if self.headers.get('Transfer-Encoding'):
                raise GatewayError('Transfer encoding is not supported.', 400)
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise GatewayError('Invalid request length.', 400) from None
            limit = MAX_BATCH_BYTES if path == '/direct-batch' else MAX_REQUEST_BYTES
            if not 0 <= size <= limit:
                raise GatewayError('Request is too large.', 413)
            raw = self.rfile.read(size)
            try:
                body = json.loads(raw or b'{}')
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise GatewayError('Expected a JSON object.', 400) from None
            if api_models:
                return self.forward_api_models('POST', path, raw or b'{}')
            run_id = query.get('job', '')
            if run_id:
                if batch_upstream:
                    upstream = batch_upstream
                    if run_id not in {row['runId'] for row in upstream.jobs()['jobs']}:
                        raise GatewayError('Unknown job in this batch.', 404)
                else:
                    upstream = self.server.owner(run_id)
                if upstream is self.server.legacy:
                    if path in CREATE_ROUTES:
                        raise GatewayError('Start a new job from the home screen.', 400)
                    if contains_policy_change(body):
                        raise GatewayError('Existing historical retrieval settings are fixed.', 400)
            elif path in CREATE_ROUTES:
                if batch_upstream is self.server.legacy:
                    raise GatewayError('Start a new job from the home screen.', 400)
                # Check for collisions even before creating unrelated new work.
                if not batch_upstream:
                    self.server.job_lists()
                upstream = self.server.current
            else:
                raise GatewayError('A job is required.', 400)
            self.forward(upstream, 'POST', path, query, raw or b'{}', batch=batch)
        except GatewayError as error:
            self.send({'error': str(error)}, error.status)
        except (OSError, ValueError, TypeError, KeyError):
            self.send({'error': 'The local dashboard could not complete this request.'}, 502)


def write_launch_status(path, record):
    """Replace the launch record atomically with a private new file.

    A failed write never leaves an empty record that blocks the next launch.
    os.fchmod is unnecessary here and is missing on Windows before 3.13.
    """
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(8)}.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(json.dumps(record, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_separate_runs(legacy_runs, new_runs):
    legacy, current = Path(legacy_runs).resolve(), Path(new_runs).resolve()
    if legacy == current or legacy.is_relative_to(current) or current.is_relative_to(legacy):
        raise ValueError('New jobs need a separate, non-overlapping runs directory.')
    return legacy, current


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-launch-status', type=Path, required=True)
    parser.add_argument('--legacy-runs', type=Path, required=True)
    parser.add_argument('--new-runs', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8772)
    parser.add_argument('--status-file', type=Path, required=True)
    args = parser.parse_args(argv)
    legacy_runs, new_runs = validate_separate_runs(args.legacy_runs, args.new_runs)
    status_path = args.status_file.resolve()
    if (status_path == args.legacy_launch_status.resolve()
            or status_path.is_relative_to(legacy_runs) or status_path.is_relative_to(new_runs)):
        raise ValueError('Gateway status must be separate from existing status and solver runs.')
    legacy = Upstream.from_launch_status(args.legacy_launch_status)
    from .server import Server
    from .legacy_overview import augment_legacy
    from .batches import BatchCatalog
    # Preserve browser sessions when upgrading only this dashboard process.
    token = None
    if status_path.is_file():
        previous = Upstream.from_launch_status(status_path)
        if previous.port == args.port:
            token = previous.token
    current = Server((HOST, 0), runs=new_runs)
    try:
        gateway = GatewayServer((HOST, args.port), legacy=legacy,
            current=Upstream(current.server_port, current.token),
            augment_legacy=partial(augment_legacy, runs_root=legacy_runs,
                                   status_path=args.legacy_launch_status),
            catalog=BatchCatalog(legacy_runs.parent, current_root=legacy_runs),
            legacy_batch=legacy_runs.name, current_batch=new_runs.name, token=token)
    except BaseException:
        current.server_close()
        raise
    worker = threading.Thread(target=current.serve_forever, daemon=True)
    worker.start()
    try:
        # Validate reachability and disjoint ownership before advertising a URL.
        gateway.job_lists()
        import os
        from datetime import datetime, timezone
        status_path.parent.mkdir(parents=True, exist_ok=True)
        record = {'controllerPid': os.getpid(), 'url': f'{gateway.origin}/#{gateway.token}',
                  'updatedAtUtc': datetime.now(timezone.utc).isoformat(),
                  'legacyPort': legacy.port, 'newControllerPort': current.server_port,
                  'newRuns': str(new_runs), 'legacyRuns': str(legacy_runs)}
        write_launch_status(status_path, record)
        print(f'Updated dashboard listening on {gateway.origin}', flush=True)
        gateway.serve_forever()
    finally:
        gateway.server_close()
        current.shutdown()
        current.server_close()
        worker.join(timeout=5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
