"""Browse saved run folders without restoring jobs or changing their artifacts.

The catalog never constructs an App or reads whole historical transcripts.
Only the selected job's bounded recent transcript is loaded for its viewer.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import threading


ORIGINAL_BATCH = '__root__'
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TRACE_BYTES = 2 * 1024 * 1024
MAX_TRACE_RECORDS = 1500
RUN_MARKERS = ('job-settings.json', 'transcript.jsonl', 'draft.md', 'checked-statement.md')
DOWNLOADS = {'transcript.jsonl', 'final.tex', 'final.pdf'}
MEMORY_FILES = {'INITIAL_PROMPT.md', 'APPROACHES.md', 'PROVED.md', 'audit.md'}
APPROACH_FILE = re.compile(r'APPROACHES/(?:index|INDEX|A[0-9]{3,}(?:-[A-Za-z0-9_-]+)?)\.md\Z')
AUDIT_FILE = re.compile(r'(?:AUDITS|audit_history)/[A-Za-z0-9][A-Za-z0-9_.-]*\.md\Z')


def _component(value):
    return (isinstance(value, str) and bool(value) and value not in {'.', '..'}
            and not any(char in value for char in '/\\\0'))


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _text(value):
    return value if isinstance(value, str) else ''


def _finite_number(value):
    try:
        return type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        return False


class BatchCatalog:
    """Read direct jobs and one level of batch folders beneath a fixed root."""

    def __init__(self, runs_root, current_root=None):
        supplied = Path(runs_root).expanduser().absolute()
        if supplied.is_symlink():
            raise ValueError('The runs directory cannot be a symbolic link.')
        self.root = supplied.resolve()
        self.current_root = Path(current_root).resolve() if current_root is not None else None
        self._trace_cache = OrderedDict()
        self._cache_lock = threading.Lock()

    def _directory(self, path):
        try:
            if not path.is_relative_to(self.root):
                return False
            parts = path.relative_to(self.root).parts
            current = self.root
            for part in parts:
                current /= part
                if current.is_symlink() or not current.is_dir():
                    return False
            return path.is_dir() and path.resolve() == path
        except OSError:
            return False

    @contextmanager
    def _open(self, directory, name):
        """Pin every directory component before opening a regular artifact."""
        parts = Path(name).parts
        if not parts or any(not _component(part) for part in parts) or Path(name).is_absolute():
            raise ValueError('Invalid saved artifact.')
        relative = directory.relative_to(self.root).parts + parts
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            with ExitStack() as opened:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(self.root, flags)
                opened.callback(os.close, descriptor)
                for part in relative[:-1]:
                    descriptor = os.open(part, flags, dir_fd=descriptor)
                    opened.callback(os.close, descriptor)
                descriptor = os.open(relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=descriptor)
                opened.callback(os.close, descriptor)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError('Saved artifacts must be regular files.')
                stream = opened.enter_context(os.fdopen(os.dup(descriptor), 'rb'))
                yield stream
        else:
            path = directory / name
            if not self._directory(path.parent) or path.is_symlink() or not path.is_file():
                raise ValueError('Saved artifacts must be regular files.')
            with path.open('rb') as stream:
                yield stream

    def _read(self, directory, name, limit=MAX_ARTIFACT_BYTES):
        try:
            with self._open(directory, name) as stream:
                raw = stream.read(limit + 1)
            return raw.decode('utf-8') if len(raw) <= limit else ''
        except (OSError, ValueError, UnicodeError):
            return ''

    def _json(self, directory, name):
        try:
            value = json.loads(self._read(directory, name))
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _info(self, directory, name):
        try:
            with self._open(directory, name) as stream:
                return os.fstat(stream.fileno())
        except (OSError, ValueError):
            return None

    def _children(self, directory):
        if not self._directory(directory):
            return []
        try:
            return sorted((entry for entry in directory.iterdir()
                           if _component(entry.name) and self._directory(entry)), key=lambda entry: entry.name)
        except OSError:
            return []

    def _is_run(self, directory):
        return any(self._info(directory, name) is not None for name in RUN_MARKERS)

    def _runs(self, directory):
        return [entry for entry in self._children(directory) if self._is_run(entry)]

    def _batches(self):
        result = {}
        if self._runs(self.root):
            result[ORIGINAL_BATCH] = self.root
        for folder in self._children(self.root):
            if folder.name != ORIGINAL_BATCH and not self._is_run(folder) and self._runs(folder):
                result[folder.name] = folder
        return result

    def batch_root(self, batch_id):
        try:
            return self._batches()[batch_id]
        except (KeyError, TypeError):
            raise ValueError('Unknown saved batch.') from None

    def batch_id(self, directory):
        directory = Path(directory).resolve()
        return next((key for key, path in self._batches().items() if path == directory), None)

    def _run(self, batch_id, run_id):
        if not _component(run_id):
            raise ValueError('Invalid saved job.')
        path = self.batch_root(batch_id) / run_id
        if not self._directory(path) or not self._is_run(path):
            raise ValueError('Unknown saved job.')
        return path

    def _started_at(self, directory, settings):
        if isinstance(settings.get('startedAt'), str) and settings['startedAt']:
            return settings['startedAt']
        for name in ('draft.md', 'checked-statement.md', 'job-settings.json', 'transcript.jsonl'):
            info = self._info(directory, name)
            if info:
                return _iso(info.st_mtime)
        return ''

    @staticmethod
    def _retrieval_settings(directory, settings):
        """Recover old path-only policies without scanning their search logs."""
        # Main used unrestricted search before this setting was introduced.
        # Historical path-only jobs below still take precedence over that default.
        mode = _text(settings.get('retrievalMode')) or 'live'
        cutoff = _text(settings.get('retrievalCutoffUtc'))
        policy_path = _text(settings.get('retrievalPolicyPath'))
        if policy_path:
            import retrieval_runtime
            mode = 'historical'
            try:
                path = Path(policy_path)
                if not path.is_absolute() or path.is_symlink():
                    raise ValueError
                policy = retrieval_runtime.load_policy(path)
                if Path(policy['isolation']['workspace']).resolve() != directory:
                    raise ValueError
                cutoff = policy['cutoffExclusiveUtc']
            except (OSError, ValueError, TypeError, KeyError, retrieval_runtime.RetrievalPolicyError):
                pass
        if mode in {'live', 'disabled'}:
            cutoff = '-1' if mode == 'disabled' else ''
        return {'mode': mode, 'cutoffExclusiveUtc': cutoff, 'fixed': mode == 'historical',
                'available': False, 'error': 'Open this saved job to see its recorded search statistics.'}

    def list_batches(self):
        cards = []
        for identity, directory in self._batches().items():
            jobs = self._runs(directory)
            started = [self._started_at(job, self._json(job, 'job-settings.json')) for job in jobs]
            counts = {'running': 0, 'done': 0, 'paused': 0, 'prepared': 0}
            for path in jobs:
                phase = self._summary(path)['phase']
                counts[phase] = counts.get(phase, 0) + 1
            name = 'Original batch' if identity == ORIGINAL_BATCH else directory.name
            cards.append({'id': identity, 'name': name, 'label': name,
                          'folder': 'runs' if identity == ORIGINAL_BATCH else 'runs/' + directory.name,
                          'jobCount': len(jobs), 'startedAt': min(filter(None, started), default=''),
                          'current': directory == self.current_root, 'readOnly': directory != self.current_root,
                          'counts': counts})
        return sorted(cards, key=lambda card: (not card['current'], card['id'] != ORIGINAL_BATCH, card['label']))

    def _summary(self, directory):
        settings = self._json(directory, 'job-settings.json')
        pause = self._json(directory, 'pause.json')
        stopped = self._json(directory, 'manual-stop.json')
        draft = self._read(directory, 'draft.md').strip()
        if draft.startswith('# Draft problem'):
            draft = draft[len('# Draft problem'):].lstrip()
        started = self._started_at(directory, settings)
        trace_info = self._info(directory, 'transcript.jsonl')
        final = self._info(directory, 'final.tex')
        failed = self._info(directory, 'failure-summary.md')
        phase = 'paused' if _text(pause.get('status')) in {'paused', 'pausing'} else 'done'
        if final and final.st_size:
            phase = 'done'
        if settings.get('preparedRun') is True and (trace_info is None or not trace_info.st_size) and not final and not failed:
            phase, started = 'prepared', ''
        stage = _text(stopped.get('stage') or pause.get('stage'))
        active_node = _text(pause.get('node')) or {
            'review': 'statement_reviewer', 'solve': 'author', 'repair': 'author',
            'critic': 'critic', 'final': 'latex_editor', 'failure': 'failure_summary',
        }.get(stage, '')
        elapsed = pause.get('elapsedSeconds', settings.get('elapsedSeconds', 0))
        elapsed = max(0, elapsed) if _finite_number(elapsed) else 0
        retrieval = self._retrieval_settings(directory, settings)
        return {'runId': directory.name, 'phase': phase, 'draft': draft, 'title': draft.split('\n', 1)[0],
                'sourceFile': _text(settings.get('sourceFile')), 'startedAt': started,
                'lastActivityAt': _iso(trace_info.st_mtime) if trace_info else '',
                'finishedAt': _iso(trace_info.st_mtime) if trace_info else '',
                'stage': stage, 'activeNode': active_node, 'stageLabel': '',
                'error': 'Stopped.' if stopped else _text(pause.get('error')),
                'manuallyStopped': bool(stopped), 'stoppedStage': _text(stopped.get('stage')),
                'problemMode': _text(settings.get('problemMode')) or 'statement',
                'resultKind': 'final' if final and final.st_size else 'failure' if failed and failed.st_size else '',
                'settingsWarning': '', 'launchHold': _text(settings.get('launchHold')),
                'retrieval': retrieval, 'retrievalMode': retrieval['mode'],
                'retrievalCutoffUtc': retrieval['cutoffExclusiveUtc'], 'retrievalPolicyPath': '',
                'workerActive': False, 'processAlive': False, 'activeElapsedSeconds': elapsed,
                'canResumeCritic': False, 'canContinueStopped': False, 'checkpoints': [],
                'readOnly': True, 'readOnlyReason': 'Saved batch — viewing does not resume or change this job.'}

    def jobs(self, batch_id):
        return sorted(({**self._summary(path), 'batchId': batch_id}
                       for path in self._runs(self.batch_root(batch_id))),
                      key=lambda job: job['startedAt'], reverse=True)

    def _trace(self, directory):
        try:
            with self._open(directory, 'transcript.jsonl') as stream:
                info = os.fstat(stream.fileno())
                signature = (info.st_ino, info.st_mtime_ns, info.st_size)
                with self._cache_lock:
                    saved = self._trace_cache.get(directory)
                    if saved and saved[0] == signature:
                        self._trace_cache.move_to_end(directory)
                        return saved[1], saved[2], saved[3]
                offset = max(0, info.st_size - MAX_TRACE_BYTES)
                stream.seek(offset)
                raw = stream.read(MAX_TRACE_BYTES)
                if offset:
                    raw = raw.partition(b'\n')[2]
            records = []
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
                except (ValueError, UnicodeError):
                    continue
            truncated = bool(offset or len(records) > MAX_TRACE_RECORDS)
            records = records[-MAX_TRACE_RECORDS:]
            with self._cache_lock:
                previous = self._trace_cache.get(directory)
                version = (previous[3] + max(1, len(records) - len(previous[1]))) if previous else len(records)
                self._trace_cache[directory] = (signature, records, truncated, version)
                self._trace_cache.move_to_end(directory)
                while len(self._trace_cache) > 16:
                    self._trace_cache.popitem(last=False)
            return records, truncated, version
        except (OSError, ValueError):
            return [], False, 0

    def state(self, batch_id, run_id, after=None):
        from .server import (DIRECT_GRAPH, LATEX_GRAPH, REVIEW_ONLY_GRAPH,
                             empty_state, event_node)
        directory = self._run(batch_id, run_id)
        state = empty_state()
        state.update(self._summary(directory))
        state['batchId'] = batch_id
        settings = self._json(directory, 'job-settings.json')
        policy_path = _text(settings.get('retrievalPolicyPath'))
        if policy_path:
            from .retrieval import public_retrieval_info
            state['retrieval'] = public_retrieval_info(policy_path, run_dir=directory)
            state['retrievalMode'] = state['retrieval']['mode']
            state['retrievalCutoffUtc'] = state['retrieval']['cutoffExclusiveUtc']
        for key in ('sourceFile', 'reviewModel', 'authorModel', 'criticModel', 'writerModel',
                    'reviewEffort', 'authorEffort', 'criticEffort', 'writerEffort', 'reasoningEffort',
                    'speedMode', 'reasoningSummary', 'goalThreadId', 'authorSteerDelivered'):
            if isinstance(settings.get(key), str):
                state[key] = settings[key]
        for key in ('fileManagement', 'preparedRun', 'skipStatementReview', 'statementReviewOnly'):
            if isinstance(settings.get(key), bool):
                state[key] = settings[key]
        for key in ('thinkingHours', 'criticRounds'):
            if _finite_number(settings.get(key)):
                state[key] = settings[key]
        for role in ('review', 'author', 'critic', 'final'):
            prompt = self._read(directory, f'prompts/{role}.txt')
            if prompt:
                state[role + 'Prompt'] = prompt
        checked = self._read(directory, 'checked-statement.md').lstrip()
        for heading in ('# Checked statement', '# Statement sent directly to the proof author'):
            if checked.startswith(heading):
                checked = checked[len(heading):].lstrip('\r\n')
                break
        state['statement'] = checked.split('\n# Reviewer notes', 1)[0].rsplit(
            '\n# Statement review\n\nSkipped by the user.', 1)[0].strip() or state['draft']
        state['reviewStatement'] = state['statement']
        if state['statementReviewOnly']:
            state['workflow'] = REVIEW_ONLY_GRAPH
        elif state['problemMode'] in {'latex', 'final-resume'}:
            state['workflow'] = LATEX_GRAPH
        elif state['skipStatementReview']:
            state['workflow'] = DIRECT_GRAPH
        records, truncated, version = self._trace(directory)
        for record in records:
            kind = _text(record.get('kind'))
            if isinstance(record.get('stage'), str) and record['stage']:
                state['stage'] = record['stage']
                state['activeNode'] = event_node({'stage': record['stage'], 'node': _text(record.get('node'))}, state['activeNode'])
            if type(record.get('round')) is int:
                state['round'] = record['round']
            if kind == 'review_result' and isinstance(record.get('review'), dict):
                state['review'] = record['review']
            if kind in {'final_result', 'failure_result'}:
                state['resultKind'] = 'final' if kind == 'final_result' else 'failure'
        if records:
            state['lastActivityAt'] = _text(records[-1].get('time')) or state['lastActivityAt']
            if not truncated:
                state['startedAt'] = _text(records[0].get('time')) or state['startedAt']
        for filename in ('final.tex', 'failure-summary.md', 'partial-output.md', 'SOLUTION.md'):
            output = self._read(directory, filename)
            if output:
                state['output'] = output
                break
        state['canDownloadTex'] = bool(self._info(directory, 'final.tex'))
        pdf = self._info(directory, 'final.pdf')
        state['canDownloadPdf'] = bool(pdf and pdf.st_size)
        state['traceVersion'] = version
        state['traceFrom'] = after if after == version else 0
        state['trace'] = [] if after == version else list(records)
        state['traceTruncated'] = truncated
        state['traceNotice'] = ('Showing recent saved events. Download the transcript for the complete history.'
                                if truncated else '')
        state['processHealthSource'] = 'Saved batch (read only)'
        return state

    def artifact(self, batch_id, run_id, name):
        if name not in DOWNLOADS:
            raise ValueError('Unknown saved download.')
        directory = self._run(batch_id, run_id)
        if self._info(directory, name) is None:
            raise ValueError('Saved download is unavailable.')
        return directory / name

    @contextmanager
    def open_artifact(self, batch_id, run_id, name):
        if name not in DOWNLOADS:
            raise ValueError('Unknown saved download.')
        with self._open(self._run(batch_id, run_id), name) as stream:
            yield stream

    def response(self, path, batch, job='', query=None):
        """Return a JSON value or a validated download path for a GET route.

        Callers stream Path results through open_artifact; this avoids reading
        multi-gigabyte transcripts into memory and pins paths against symlinks.
        """
        query = query or {}
        headers = {}
        content_type = 'application/json'
        if path == '/jobs':
            body = {'jobs': self.jobs(batch)}
        elif path == '/state':
            try:
                after = int(query['after']) if 'after' in query else None
            except (ValueError, TypeError):
                raise ValueError('Invalid saved transcript position.') from None
            body = self.state(batch, job, after)
        elif path == '/memory':
            body = self.memory(batch, job, query.get('file', ''), query.get('version', ''))
        elif path == '/approach-graph':
            body = self.approach_graph(batch, job)
        elif path in {'/transcript', '/download-tex', '/download-pdf'}:
            name = {'/transcript': 'transcript.jsonl', '/download-tex': 'final.tex',
                    '/download-pdf': 'final.pdf'}[path]
            body = self.artifact(batch, job, name)
            content_type = {'/transcript': 'application/x-ndjson', '/download-tex': 'application/x-tex',
                            '/download-pdf': 'application/pdf'}[path]
            headers['Content-Disposition'] = f'attachment; filename="{name}"'
        else:
            raise ValueError('Unknown saved batch request.')
        return body, content_type, headers

    def memory(self, batch_id, run_id, name, version=''):
        directory = self._run(batch_id, run_id)
        if not isinstance(name, str):
            raise ValueError('Unknown research memory file.')
        approach = name in {'APPROACHES', 'APPROACHES.md'} or bool(APPROACH_FILE.fullmatch(name))
        audit = name in {'AUDITS', 'audit_history', 'audit.md'} or bool(AUDIT_FILE.fullmatch(name))
        if name not in MEMORY_FILES and not approach and not audit:
            raise ValueError('Unknown research memory file.')
        result = {'name': name, 'status': 'missing', 'workspace': directory.name}
        if approach or audit:
            folder = 'APPROACHES' if approach else 'audit_history' if name == 'audit.md' or name.startswith('audit_history') else 'AUDITS'
            matcher = APPROACH_FILE if approach else AUDIT_FILE
            location = directory / folder
            entries = []
            if self._directory(location):
                try:
                    entries = [f'{folder}/{entry.name}' for entry in location.iterdir()
                               if matcher.fullmatch(f'{folder}/{entry.name}')
                               and self._info(directory, f'{folder}/{entry.name}') is not None]
                except OSError:
                    pass
            entries.sort(reverse=not approach)
            if approach:
                index = next((item for item in entries if Path(item).name in {'index.md', 'INDEX.md'}), '')
                entries = ([index] if index else []) + [item for item in entries if item != index]
            legacy = 'APPROACHES.md' if approach else 'audit.md'
            if (approach or folder == 'audit_history') and self._info(directory, legacy) is not None:
                entries.append(legacy)
            result['files'] = entries
            if name in {'APPROACHES', 'AUDITS', 'audit_history'}:
                if not entries:
                    return result
                name = entries[0]
                result['name'] = name
        info = self._info(directory, name)
        if info is None:
            return result
        current = f'{info.st_ino}:{info.st_mtime_ns}:{info.st_size}'
        result.update(status='ready', version=current, modifiedAt=_iso(info.st_mtime))
        if version == current:
            return {**result, 'unchanged': True}
        if info.st_size > MAX_ARTIFACT_BYTES:
            return {**result, 'status': 'unavailable'}
        return {**result, 'content': self._read(directory, name)}

    def approach_graph(self, batch_id, run_id):
        from .server import approach_metadata
        listing = self.memory(batch_id, run_id, 'APPROACHES')
        files = listing.get('files', [])
        nodes = []
        for name in files:
            if re.fullmatch(r'APPROACHES/A\d{3,}(?:-[A-Za-z0-9_-]+)?\.md', name):
                document = self.memory(batch_id, run_id, name)
                nodes.append({**approach_metadata(name, document.get('content', '')), 'readStatus': document['status']})
        return {'nodes': nodes, 'files': files, 'status': listing['status'],
                'indexFile': next((name for name in files if Path(name).name in {'index.md', 'INDEX.md'}), '')}
