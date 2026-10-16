"""Select the current run without restoring a second owner of live jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import http.client
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / '.experiment-control' / 'current-run.json'
HOST = '127.0.0.1'
PROBE_TIMEOUT_SECONDS = 2
MAX_STATUS_BYTES = 1024 * 1024
MAX_JOBS_BYTES = 8 * 1024 * 1024


class CurrentRunError(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    runs_root: Path | None = None
    url: str = field(default='', repr=False)

    @property
    def attached(self):
        return bool(self.url)


@dataclass(frozen=True)
class _Endpoint:
    pid: int
    port: int
    token: str = field(repr=False)
    url: str = field(repr=False)


def _read_json(path):
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError
        with path.open('rb') as stream:
            raw = stream.read(MAX_STATUS_BYTES + 1)
        value = json.loads(raw)
        if len(raw) > MAX_STATUS_BYTES or not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise CurrentRunError('The current-run configuration or controller status is unreadable.') from None


def _absolute(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise CurrentRunError('Current-run paths must be absolute local paths.')
    return Path(value)


def _endpoint(record):
    try:
        pid = record['controllerPid']
        raw = record['url']
        if type(pid) is not int or pid <= 0 or not isinstance(raw, str):
            raise ValueError
        url = urlsplit(raw)
        if (url.scheme != 'http' or url.hostname != HOST or not url.port
                or url.username is not None or url.password is not None
                or url.path not in ('', '/') or url.query or not url.fragment
                or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', url.fragment)):
            raise ValueError
        return _Endpoint(pid, url.port, url.fragment, raw)
    except (KeyError, TypeError, ValueError):
        raise CurrentRunError('The current-run controller status is invalid.') from None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return None


def _expected_jobs(record):
    try:
        rows = record['runs']
        if not isinstance(rows, list) or not rows:
            raise ValueError
        identifiers = [row['runId'] for row in rows]
        if (any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', key)
                or key in ('.', '..') for key in identifiers)
                or len(identifiers) != len(set(identifiers))):
            raise ValueError
        return set(identifiers)
    except (KeyError, TypeError, ValueError):
        raise CurrentRunError('The current-run controller does not identify its jobs.') from None


def _authenticated_jobs(endpoint, expected_jobs):
    connection = http.client.HTTPConnection(HOST, endpoint.port, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        connection.request('GET', '/jobs', headers={
            'X-TCS-Prover-Token': endpoint.token,
            'Origin': f'http://{HOST}:{endpoint.port}',
        })
        response = connection.getresponse()
        if response.status != 200 or not response.getheader('Content-Type', '').startswith('application/json'):
            return False
        raw = response.read(MAX_JOBS_BYTES + 1)
        if len(raw) > MAX_JOBS_BYTES:
            return False
        jobs = json.loads(raw)['jobs']
        if not isinstance(jobs, list):
            return False
        identifiers = [row['runId'] for row in jobs]
        if (any(not isinstance(key, str) or not key for key in identifiers)
                or len(identifiers) != len(set(identifiers))):
            return False
        # The gateway may also contain separately created jobs, but it must
        # include every job owned by the selected controller.
        return expected_jobs <= set(identifiers)
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
        return False
    finally:
        connection.close()


def select_current_dashboard(config_path=None):
    """Attach to a live owner, or choose its runs only when both owners are dead.

    Missing local configuration retains the ordinary launcher behavior. A
    configured but unreachable owner fails closed instead of restoring duplicate
    jobs. Probes are authenticated read-only GET requests and never redirect.
    """
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    if not path.exists() and not path.is_symlink():
        return Selection()
    config = _read_json(path)
    if (set(config) != {'version', 'runsRoot', 'controllerStatus', 'dashboardStatus'}
            or type(config['version']) is not int or config['version'] != 1):
        raise CurrentRunError('The current-run configuration has an unsupported format.')
    runs_root = _absolute(config['runsRoot']).resolve()
    if not runs_root.is_dir():
        raise CurrentRunError('The configured current-run directory is unavailable.')
    controller_path = _absolute(config['controllerStatus'])
    dashboard_path = _absolute(config['dashboardStatus'])
    controller_record = _read_json(controller_path)
    expected = _expected_jobs(controller_record)
    controller = _endpoint(controller_record)
    dashboard = None
    try:
        record = _read_json(dashboard_path)
        if 'legacyRuns' in record and _absolute(record['legacyRuns']).resolve() != runs_root:
            raise CurrentRunError('The dashboard belongs to another run directory.')
        dashboard = _endpoint(record)
    except CurrentRunError:
        # A missing dashboard record does not prevent reusing the authoritative
        # controller. It does prevent assuming that no dashboard still owns it.
        pass
    for endpoint in (dashboard, controller):
        if endpoint is not None and _authenticated_jobs(endpoint, expected):
            return Selection(runs_root, endpoint.url)
    if dashboard is None or _pid_alive(controller.pid) is not False or _pid_alive(dashboard.pid) is not False:
        raise CurrentRunError(
            'The current dashboard is unavailable while its controller may still be running. '
            'No second controller was started; retry after the existing dashboard responds.')
    if any(row.get('phase') not in ('done', 'paused') or row.get('pid') is not None
           for row in controller_record['runs']):
        # Workflow children use independent process groups and can outlive their
        # controller. A stale "running" record is not evidence of an idle run,
        # even when the original worker PID has since disappeared.
        raise CurrentRunError(
            'The saved controller status still records unfinished or active work. '
            'No second controller was started; verify remaining workers before explicit recovery.')
    return Selection(runs_root)
