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
import stat
import threading


ORIGINAL_BATCH = '__root__'
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TRACE_BYTES = 2 * 1024 * 1024
MAX_TRACE_RECORDS = 1500
RUN_MARKERS = ('job-settings.json', 'transcript.jsonl', 'draft.md', 'checked-statement.md')
DOWNLOADS = {'transcript.jsonl', 'final.tex', 'final.pdf'}
# A queued job that never started may already hold the controller's notes about it (its related works,
# a time limit set while it waited), as small transcript records; only then is its transcript read for
# a list, and at most this much of it.
MAX_NOTES_BYTES = 256 * 1024
# The controller's own copies of a conversation and a related-works record are read up to this size.
MAX_CONTROLLER_BYTES = 16 * 1024 * 1024


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


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def job_tone(job):
    """One job row's status, classified as jobTone and completedRunOutcome in app.js classify it.

    "running", "paused", "queued" (waiting for a running slot), "waiting" (for the user), or a finished
    job's tone: "done" for every successful outcome (counted under Complete), else "failed" or
    "stopped" (both counted under Incomplete).
    """
    phase = job.get('phase')
    if phase in {'reviewing', 'running', 'stopping', 'pausing'}:
        return 'running'
    if phase == 'paused':
        return 'paused'
    if phase == 'prepared' and job.get('launchQueued'):
        return 'queued'
    if phase in {'reviewed', 'prepared'}:
        return 'waiting'
    if job.get('manuallyStopped'):
        return 'stopped'
    if phase != 'done' or job.get('interrupted'):
        return 'failed'
    kind = job.get('resultKind')
    if kind == 'review' or job.get('statementReviewOnly'):
        return 'done'
    if kind in {'needs-revision', 'failure'} or job.get('revisionRequired') or job.get('error'):
        return 'failed'
    return 'done' if kind in {'proof', 'final'} or job.get('canDownloadTex') else 'failed'


def batch_counts(rows):
    """A batch card's counts, each job once: by phase, except that a finished job counts as "done" only
    for a successful outcome (the check mark) and as "incomplete" otherwise (failed, stopped or
    interrupted), as the batch page's Complete and Incomplete filters count them."""
    counts = {}
    for row in rows:
        phase = row.get('phase') or 'unknown'
        if phase == 'done' and job_tone(row) != 'done':
            phase = 'incomplete'
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def _active_seconds(phase, settings, pause, started, end):
    """Mirror App._elapsed_seconds using only artifacts the catalog already read.

    A paused checkpoint holds a frozen total. Every other saved job adds its
    last running segment, which ends at its latest activity, to the time
    recorded when that segment started.
    """
    if phase == 'prepared':
        return 0
    if phase == 'paused' and _finite_number(pause.get('elapsedSeconds')):
        return max(0, pause['elapsedSeconds'])
    base = settings.get('elapsedSeconds')
    if not _finite_number(base):
        # A leftover 'pausing' checkpoint beside a result is already a total.
        base = pause.get('elapsedSeconds') if pause.get('status') == 'running' else 0
    base = max(0, base) if _finite_number(base) else 0
    if phase == 'paused':
        return base
    try:
        start = _timestamp(_text(settings.get('resumedAt')) or started)
        return base + max(0, (_timestamp(end) - start).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return base


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
        """A saved text file; '' when it is missing, unreadable or (with a ``limit``) larger than it."""
        try:
            with self._open(directory, name) as stream:
                raw = stream.read() if limit is None else stream.read(limit + 1)
            return raw.decode('utf-8') if limit is None or len(raw) <= limit else ''
        except (OSError, ValueError, UnicodeError):
            return ''

    def _too_large(self, directory, name):
        """A notice for a saved file too large to show here, else ''."""
        info = self._info(directory, name)
        if info is None or info.st_size <= MAX_ARTIFACT_BYTES:
            return ''
        return (f'{name} is too large to show here ({info.st_size / 1048576:.1f} MB). '
                f'See {name} in the run folder {directory}.')

    def _json(self, directory, name, limit=MAX_ARTIFACT_BYTES):
        try:
            value = json.loads(self._read(directory, name, limit))
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
            # Hidden folders are never batches or runs: deleted jobs live in runs/.trash.
            return sorted((entry for entry in directory.iterdir()
                           if _component(entry.name) and not entry.name.startswith('.')
                           and self._directory(entry)), key=lambda entry: entry.name)
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

    def _run(self, batch_id, run_id):
        if not _component(run_id) or run_id.startswith('.'):
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
            from retrieval import runtime as retrieval_runtime
            from .retrieval import display_policy
            mode = 'historical'
            try:
                path = Path(policy_path)
                if not path.is_absolute() or path.is_symlink():
                    raise ValueError
                # A full load rehashes the pinned Codex binary on every poll.
                policy = display_policy(path)
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
            counts = {'running': 0, 'done': 0, 'paused': 0, 'prepared': 0, 'incomplete': 0,
                      **batch_counts([self._summary(path) for path in jobs])}
            name = 'Original batch' if identity == ORIGINAL_BATCH else directory.name
            cards.append({'id': identity, 'name': name, 'label': name,
                          'folder': 'runs' if identity == ORIGINAL_BATCH else 'runs/' + directory.name,
                          'jobCount': len(jobs), 'startedAt': min(filter(None, started), default=''),
                          'current': directory == self.current_root, 'readOnly': directory != self.current_root,
                          'counts': counts})
        return sorted(cards, key=lambda card: (not card['current'], card['id'] != ORIGINAL_BATCH, card['label']))

    def _summary(self, directory):
        from .server import saved_convention_warning, saved_workflow_stages

        # Controller state is read whole: a paused job must never look finished.
        settings = self._json(directory, 'job-settings.json', None)
        try:
            selected = saved_workflow_stages(settings)
        except ValueError:
            selected = saved_workflow_stages({key: value for key, value in settings.items() if key != 'workflowStages'})
        pause = self._json(directory, 'pause.json', None)
        stopped = self._json(directory, 'manual-stop.json')
        draft = self._read(directory, 'draft.md').strip()
        if draft.startswith('# Draft problem'):
            draft = draft[len('# Draft problem'):].lstrip()
        started = self._started_at(directory, settings)
        trace_info = self._info(directory, 'transcript.jsonl')
        final = self._info(directory, 'final.tex')
        proof = self._info(directory, 'proof-output.md')
        failed = self._info(directory, 'failure-summary.md')
        phase = 'paused' if _text(pause.get('status')) in {'paused', 'pausing'} else 'done'
        if (final and final.st_size) or (proof and proof.st_size):
            phase = 'done'
        if (settings.get('preparedRun') is True and self._never_started(directory, trace_info)
                and not final and not proof and not failed):
            phase, started = 'prepared', ''
        stage = _text(stopped.get('stage') or pause.get('stage'))
        # A stop marker names the step it stopped (the compile step, not only the writing stage).
        active_node = _text(stopped.get('node')) or _text(pause.get('node')) or {
            'review': 'statement_reviewer', 'solve': 'author', 'repair': 'author',
            'critic': 'critic', 'final': 'latex_editor', 'failure': 'failure_summary',
        }.get(stage, '')
        last_activity = _iso(trace_info.st_mtime) if trace_info else ''
        # Lists never read transcripts, so the transcript's mtime ends the segment.
        elapsed = _active_seconds(phase, settings, pause, started, last_activity)
        retrieval = self._retrieval_settings(directory, settings)
        # Its last checkpoint says it was running: the run stopped part-way (a crash or a restart).
        interrupted = (phase == 'done' and not stopped and not (final and final.st_size)
                       and not (failed and failed.st_size) and _text(pause.get('status')) == 'running')
        # Why a paused job paused (the time limit, the user, an error, ...), as the live job page says it.
        cause = _text(pause.get('pauseCause')) if phase == 'paused' else ''
        # The convention critic's loophole warning as the job saved it; the list tags it until it was ignored.
        warning = saved_convention_warning(settings.get('conventionWarning'))
        return {'runId': directory.name, 'phase': phase, 'draft': draft, 'title': draft.split('\n', 1)[0],
                'sourceFile': _text(settings.get('sourceFile')), 'startedAt': started,
                'lastActivityAt': last_activity, 'finishedAt': last_activity,
                'stage': stage, 'activeNode': active_node, 'stageLabel': '',
                'error': 'Stopped.' if stopped else _text(pause.get('error')) or (
                    'Interrupted: the run stopped part-way, while its last checkpoint says it was running.'
                    if interrupted else ''),
                'interrupted': interrupted,
                'pauseCause': cause if cause in {'error', 'user', 'interrupted', 'restart', 'time-limit'} else '',
                'manuallyStopped': bool(stopped), 'stoppedStage': _text(stopped.get('stage')),
                'problemMode': _text(settings.get('problemMode')) or 'statement',
                'workflowStages': selected, 'revisionRequired': settings.get('revisionRequired') is True,
                'originalContent': _text(settings.get('originalContent')) or draft,
                'resultKind': ('final' if final and final.st_size else 'proof' if proof and proof.st_size
                               else 'needs-revision' if failed and settings.get('revisionRequired') is True
                               else 'failure' if failed and failed.st_size else
                               'review' if settings.get('resultKind') == 'review' else ''),
                'conventionWarning': warning, 'loopholeWarning': bool(warning and not warning['dismissed']),
                'settingsWarning': '', 'launchHold': _text(settings.get('launchHold')),
                'retrieval': retrieval, 'retrievalMode': retrieval['mode'],
                'retrievalCutoffUtc': retrieval['cutoffExclusiveUtc'], 'retrievalPolicyPath': '',
                'workerActive': False, 'processAlive': False, 'activeElapsedSeconds': elapsed,
                'canResumeCritic': False, 'canContinueStopped': False, 'checkpoints': [],
                'readOnly': True, 'readOnlyReason': 'Saved batch — viewing does not resume or change this job.'}

    def _never_started(self, directory, trace_info):
        """Whether a transcript holds only the controller's notes about a job (restore_saved_app skips them too)."""
        from .server import controller_note

        if trace_info is None or not trace_info.st_size:
            return True
        if trace_info.st_size > MAX_NOTES_BYTES:
            return False
        lines = self._read(directory, 'transcript.jsonl', MAX_NOTES_BYTES).splitlines()
        try:
            records = [json.loads(line) for line in lines if line.strip()]
        except ValueError:
            return False
        return bool(records) and all(isinstance(record, dict) and (controller_note(record)
                                                                   or record.get('label') == 'Total time limit set')
                                     for record in records)

    def _controller_json(self, directory, name):
        """A file of the controller's folder of one saved run (runs/.controller/<run>); {} when unreadable."""
        from .server import CONTROLLER_FOLDER

        return self._json(directory.parent, f'{CONTROLLER_FOLDER}/{directory.name}/{name}', MAX_CONTROLLER_BYTES)

    def _conversation(self, directory, reason):
        """The saved summaries, messages and replies of a run, as the job page lists them (nothing can be sent).

        A conversation too large to read here gets a notice instead of its items.
        """
        from .server import CONTROLLER_FOLDER, CONVERSATION_FILENAME, saved_conversation_items

        conversation = {'available': False, 'reason': reason, 'busy': False, 'readOnly': True, 'items': []}
        info = self._info(directory.parent, f'{CONTROLLER_FOLDER}/{directory.name}/{CONVERSATION_FILENAME}')
        if info is not None and info.st_size > MAX_CONTROLLER_BYTES:
            path = directory.parent / CONTROLLER_FOLDER / directory.name / CONVERSATION_FILENAME
            conversation['notice'] = (f'The saved conversation is too large to show here ({info.st_size / 1048576:.1f} MB). '
                                      f'See {path}.')
            return conversation
        conversation['items'] = [{**item, 'status': 'stopped' if item['status'] in {'queued', 'running'} else item['status']}
                                 for item in saved_conversation_items(self._controller_json(directory, CONVERSATION_FILENAME))]
        return conversation

    def _related_works(self, directory, settings, reason):
        """A saved run's related works, as the job page lists them (none can be added)."""
        from . import related_works
        from .server import CONTROLLER_FOLDER

        workspace = directory
        saved = Path(_text(settings.get('goalWorkspace')) or directory)
        # A continuation reads its source's related works, when that run is in the same saved batch.
        if saved.is_absolute() and saved.parent == directory.parent and self._directory(saved):
            workspace = saved
        manifest = workspace.parent / CONTROLLER_FOLDER / workspace.name
        items = related_works.listing(workspace, manifest) if self._directory(manifest) else []
        return {'available': False, 'reason': reason, 'count': len(items), 'items': items, 'batch': None}

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
        from .server import (RESUME_MARGIN_SECONDS, controller_note, empty_state, event_node, follow_time_limit,
                             model_menu_settings, saved_api_models, saved_time_limit_history, workflow_graph)
        directory = self._run(batch_id, run_id)
        state = empty_state()
        state.update(self._summary(directory))
        state['batchId'] = batch_id
        settings = self._json(directory, 'job-settings.json', None)
        policy_path = _text(settings.get('retrievalPolicyPath'))
        if policy_path:
            from .retrieval import public_retrieval_info
            # The saved cutoff stays visible when the policy metadata cannot load.
            state['retrieval'] = public_retrieval_info(policy_path, run_dir=directory,
                                                       saved_cutoff=state['retrievalCutoffUtc'])
            state['retrievalMode'] = state['retrieval']['mode']
            state['retrievalCutoffUtc'] = state['retrieval']['cutoffExclusiveUtc']
        for key in ('sourceFile', 'reviewModel', 'authorModel', 'criticModel', 'writerModel',
                    'reviewEffort', 'authorEffort', 'criticEffort', 'writerEffort', 'reasoningEffort',
                    'speedMode', 'reasoningSummary', 'goalThreadId', 'authorSteerDelivered'):
            if isinstance(settings.get(key), str):
                state[key] = settings[key]
        # Saved API model settings (never a key) label models that were removed since.
        api_models = saved_api_models(settings.get('apiModels'))
        if api_models:
            state['apiModels'] = api_models
        for key in ('preparedRun', 'skipStatementReview', 'statementReviewOnly'):
            if isinstance(settings.get(key), bool):
                state[key] = settings[key]
        for key in ('thinkingHours', 'criticRounds', 'criticCount', 'authorSubagents'):
            if _finite_number(settings.get(key)):
                state[key] = settings[key]
        state['criticCountRecorded'] = _finite_number(settings.get('criticCount'))
        state['authorSubagentsRecorded'] = _finite_number(settings.get('authorSubagents'))
        for role in ('review', 'author', 'critic', 'final'):
            # Never the current default in place of a saved prompt that is too large to show.
            prompt = (self._too_large(directory, f'prompts/{role}.txt')
                      or self._read(directory, f'prompts/{role}.txt'))
            if prompt:
                state[role + 'Prompt'] = prompt
        checked = self._read(directory, 'checked-statement.md').replace('\r\n', '\n').lstrip()
        for heading in ('# Checked statement', '# Statement sent directly to the proof author'):
            if checked.startswith(heading):
                checked = checked[len(heading):].lstrip('\r\n')
                break
        state['statement'] = checked.split('\n# Reviewer notes', 1)[0].rsplit(
            '\n# Statement review\n\nSkipped by the user.', 1)[0].strip() or state['draft']
        state['reviewStatement'] = state['statement']
        state['workflow'] = workflow_graph(state['workflowStages'])
        state['skipStatementReview'] = not state['workflowStages']['review']
        state['statementReviewOnly'] = state['workflowStages']['review'] and not any(
            state['workflowStages'][name] for name in ('author', 'critic', 'writing'))
        # Every /state carries the model catalog, read per request; the shared
        # graph constants never hold user data.
        graph = state['workflow']
        state['workflow'] = {**graph, 'settings': {**graph['settings'], **model_menu_settings()}}
        records, truncated, version = self._trace(directory)
        # The time limit's history: the job settings keep it whole; older jobs rebuild it from recent records.
        history = saved_time_limit_history(settings.get('timeLimitHistory'))
        rebuild = not history
        for record in records:
            if rebuild:
                follow_time_limit(history, record)
            kind = _text(record.get('kind'))
            # The controller's notes (the paused author's replies, related works) are not workflow steps.
            if isinstance(record.get('stage'), str) and record['stage'] and not controller_note(record):
                state['stage'] = record['stage']
                state['activeNode'] = event_node({'stage': record['stage'], 'node': _text(record.get('node'))}, state['activeNode'])
            if type(record.get('round')) is int:
                state['round'] = record['round']
            if kind == 'review_result' and isinstance(record.get('review'), dict):
                state['review'] = record['review']
            if kind in {'final_result', 'failure_result'}:
                state['resultKind'] = ('proof' if record.get('resultKind') == 'proof' else 'final') if kind == 'final_result' else (
                    'needs-revision' if record.get('resultKind') == 'needs-revision' else 'failure')
                state['revisionRequired'] = state['resultKind'] == 'needs-revision'
                if state['revisionRequired']:
                    state['criticBugs'] = record.get('bugs', '')
                    state['error'] = 'Proof needs revision. See the critic feedback.'
        if records:
            state['lastActivityAt'] = _text(records[-1].get('time')) or state['lastActivityAt']
            if not truncated:
                state['startedAt'] = _text(records[0].get('time')) or state['startedAt']
        state['activeElapsedSeconds'] = _active_seconds(
            state['phase'], settings, self._json(directory, 'pause.json', None),
            state['startedAt'], state['lastActivityAt'])
        # As App._time_limit_exhausted_locked: a paused author whose limit is used up.
        hours = state.get('thinkingHours') if _finite_number(state.get('thinkingHours')) else 0
        state['timeLimitExhausted'] = bool(
            state['phase'] == 'paused' and state['workflowStages']['author'] and state['activeNode'] == 'author'
            and hours > 0 and hours * 3600 - state['activeElapsedSeconds'] < RESUME_MARGIN_SECONDS)
        for filename in ('final.tex', 'proof-output.md', 'failure-summary.md', 'partial-output.md', 'SOLUTION.md'):
            # A file too large to show is named, never replaced by another file.
            output = self._too_large(directory, filename) or self._read(directory, filename)
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
        state['traceNotice'] = ("Showing recent saved events. The complete history is in this job's "
                                'transcript.jsonl in its run folder; read it with '
                                'python3 transcript/view_transcript.py <run folder>.'
                                if truncated else '')
        state['timeLimitHistory'] = history
        state['processHealthSource'] = 'Saved batch (read only)'
        state['conversation'] = self._conversation(directory, state['readOnlyReason'])
        state['relatedWorks'] = self._related_works(directory, settings, state['readOnlyReason'])
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
