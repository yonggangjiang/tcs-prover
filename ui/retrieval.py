"""Controller-owned historical search setup and content-free UI metadata."""
from __future__ import annotations

import copy
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from retrieval import historical_search
from retrieval import isolated_compiler
from retrieval import runtime as retrieval_runtime
import workflow_runner as runtime

ROOT = Path(__file__).resolve().parents[1]
LIMITATION = ('Current discovery suggests URLs; only verified archive text strictly before the cutoff is returned. '
              'Coverage is incomplete and model training knowledge is not time limited.')
PLATFORM_REASON = ('Historical search currently requires macOS: the isolated LaTeX compiler '
                   'has only a macOS Seatbelt adapter.')
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_POLICY_MEMO = {}
# The dashboard folds at most this much new audit log per refresh; a larger
# backlog is caught up over the following refreshes (marked catchingUp).
MAX_AUDIT_READ_BYTES = 16 * 1024 * 1024
_AUDIT_TOTALS = OrderedDict()
FINGERPRINT_BYTES = 4096
# New runs page open() (see historical_search.tool_definitions).
NAVIGATION_VERSION = 'bounded-page-v2'
OPEN_DEFAULT_CHARS = 30000
REQUEST_FAILURE_LABELS = {
    'no_capture': 'No verified pre-cutoff archive',
    'transport_failure': 'Archive connection failed',
    'response_too_large': 'Archive response exceeded size limit',
    'deadline': 'Request timed out',
    'busy': 'Retrieval capacity busy',
    'unknown': 'Request failed',
}
REQUEST_TOOLS = {f'{tool}_{suffix}': tool for tool, suffixes in (
    ('search', ('requested', 'completed', 'failed')),
    ('open', ('requested', 'completed', 'rejected')),
    ('find', ('requested', 'completed', 'rejected')),
) for suffix in suffixes}
REQUEST_SUCCESSES = frozenset(('search_completed', 'open_completed', 'find_completed'))
REQUEST_ERRORS = frozenset(('search_failed', 'open_rejected', 'find_rejected'))
MCP_TERMINALS = frozenset(('mcp_response_ready', 'mcp_deadline', 'mcp_cancelled'))


class PreflightRetry(ValueError):
    """A launch check that could not finish (a timeout, no connectivity, an early exit).

    Nothing was proven either way; the server runs it again every minute.
    """


class NeedsAttention(ValueError):
    """A run held until someone fixes the problem its message names."""


def _log(message):
    """One line on the server's console, so a failed check is never silent."""
    try:
        print(f'TCS Prover historical search: {message}', file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _cause(exc):
    """The real reason for a failed check, in plain controller-written words."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f'the check did not finish within {exc.timeout:g} seconds'
    if isinstance(exc, subprocess.CalledProcessError):
        lines = [line.strip() for line in (exc.stderr or '').splitlines() if line.strip()]
        return lines[-1][:500] if lines else f'the archive broker exited with code {exc.returncode}'
    if isinstance(exc, json.JSONDecodeError):
        return 'the archive broker returned unreadable output'
    return runtime.redact(str(exc).strip().rstrip('.'))[:1000] or type(exc).__name__


def _transient(exc):
    return isinstance(exc, (subprocess.TimeoutExpired, retrieval_runtime.IsolationCheckUnavailable)) or (
        isinstance(exc, OSError) and not isinstance(exc, retrieval_runtime.RetrievalPolicyError))


def capabilities():
    # Model roles also have a Windows MXC adapter, but every historical run
    # ends in the isolated compiler, which supports only macOS.
    available = isolated_compiler.supported()
    return {'historicalAvailable': available, 'defaultMode': 'live', 'scope': 'author',
            'reason': '' if available else PLATFORM_REASON,
            'requiresDirect': True, 'requiresCodexModels': True}


def validate_options(mode='live', cutoff='', *, models=(),
                     continuation_source=''):
    """Validate before creating a workspace or starting any model work."""
    if mode not in ('live', 'disabled', 'historical'):
        raise ValueError('Choose unrestricted search or enable the web cutoff.')
    if not isinstance(cutoff, str):
        raise ValueError('The search cutoff must be an exact UTC timestamp.')
    cutoff = cutoff.strip()
    if mode == 'live':
        if cutoff:
            raise ValueError('Clear the cutoff when unrestricted web search is selected.')
        return {'retrievalMode': 'live', 'retrievalCutoffUtc': ''}
    if mode == 'disabled' or cutoff == '-1':
        if cutoff not in ('', '-1'):
            raise ValueError('Use -1 to disable web-search tools, or choose a historical UTC cutoff.')
        return {'retrievalMode': 'disabled', 'retrievalCutoffUtc': '-1'}
    try:
        parsed = historical_search.parse_cutoff(cutoff)
    except historical_search.HistoricalSearchError as exc:
        raise ValueError('Enter an exact UTC cutoff, for example 2026-10-02T14:13:12Z.') from exc
    if parsed >= datetime.now(timezone.utc):
        raise ValueError('The historical search cutoff must be in the past.')
    if not capabilities()['historicalAvailable']:
        raise ValueError(capabilities()['reason'])
    if continuation_source:
        raise ValueError('Historical search cannot be added to an existing conversation. Start a new direct proof.')
    # Claude and API models (including OpenAI-format ones run through Codex)
    # are excluded: historical isolation exists only for native Codex models.
    if any(runtime.model_provider(model) != runtime.OPENAI_PROVIDER for model in models):
        raise ValueError(runtime.HISTORICAL_CODEX_ONLY)
    return {'retrievalMode': mode, 'retrievalCutoffUtc': historical_search.utc_text(parsed)}


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)


def continuation_options(workspace):
    """Keep existing conversations' search mode; policies cannot change workspace."""
    path = Path(workspace).resolve() / 'job-settings.json'
    try:
        settings = retrieval_runtime._read_saved_settings(path, 'Cannot verify saved search settings.') if path.exists() or path.is_symlink() else {}
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ValueError(str(exc)) from exc
    if settings.get('retrievalPolicyPath') or settings.get('retrievalMode') == 'historical':
        raise ValueError('Historical runs keep their original workspace and cutoff. Continue the original job in '
                         'its own run folder; a new run cannot take over its retrieval policy.')
    return validate_options(settings.get('retrievalMode', 'live'), settings.get('retrievalCutoffUtc', ''))


def prepare_policy(workspace, cutoff, *, controller_root=None):
    """Create an immutable policy outside the solver; no model/network activity."""
    workspace = Path(workspace).resolve()
    parent = Path(controller_root or ROOT / '.experiment-control' / 'ui').resolve()
    if parent == workspace or workspace in parent.parents:
        raise ValueError('Historical controller state must be outside the solver workspace.')
    adapter = retrieval_runtime.MACOS_ADAPTER if sys.platform == 'darwin' else retrieval_runtime.WINDOWS_ADAPTER
    try:
        executable = retrieval_runtime.codex_executable({'isolation': {'workspace': str(workspace), 'adapter': adapter}})
    except (retrieval_runtime.RetrievalPolicyError, retrieval_runtime.IsolationCheckUnavailable) as exc:
        raise ValueError(str(exc)) from exc
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    controller = parent / (workspace.name + '-' + uuid.uuid4().hex)
    controller.mkdir(mode=0o700)
    broker_path = controller / 'broker-policy.json'
    audit_path = controller / 'historical-search.jsonl'
    shared_state = os.environ.get('TCS_PROVER_UI_RETRIEVAL_STATE')
    rate_directory = Path(shared_state).expanduser() if shared_state else parent / 'retrieval-state'
    if not rate_directory.is_absolute():
        raise ValueError('The controller retrieval-state directory must be absolute.')
    from retrieval.historical_codex_discovery import build_settings, keep_binary
    discovery = build_settings(executable, controller / 'discovery-auth',
                               controller / 'discovery-state', runtime.AUTHOR_MODEL)
    # A hard link keeps the pinned bytes when a Codex update deletes the original.
    keep_binary(discovery)
    _write_json(broker_path, {
        'schema_version': 1, 'provider': 'codex_wayback', 'max_results': 5,
        'max_candidates': 6, 'max_content_chars': 20000, 'request_timeout_seconds': 15,
        'operation_timeout_seconds': 300, 'solver_workspace': str(workspace),
        'rate_limit_directory': str(rate_directory.resolve()),
        'open_default_chars': OPEN_DEFAULT_CHARS,
        'discovery': discovery,
    })
    path = controller / 'retrieval-policy.json'
    _write_json(path, {
        'schemaVersion': 1, 'mode': 'historical', 'runId': workspace.name,
        'cutoffExclusiveUtc': cutoff,
        # Preserve the virtualenv launcher path: resolving its symlink selects
        # system Python and drops installed PDF/parser dependencies.
        'broker': {'command': str(Path(sys.executable).absolute()),
                   'args': [str(ROOT / 'retrieval' / 'historical_search.py'), '--cutoff', cutoff,
                            '--policy-file', str(broker_path), '--policy-sha256',
                            hashlib.sha256(broker_path.read_bytes()).hexdigest(),
                            '--audit-log', str(audit_path)],
                   'policyFile': str(broker_path), 'auditLog': str(audit_path)},
        'isolation': {'status': 'requires-runtime-preflight',
                      'adapter': adapter,
                      'codexHome': str(controller / 'codex-home'), 'workspace': str(workspace)},
        'metadata': {'source': 'local-ui', 'navigationVersion': NAVIGATION_VERSION,
                     'discoveryProvider': 'codex_wayback',
                     'inputSha256': hashlib.sha256((workspace / 'input.md').read_bytes()).hexdigest()},
    })
    try:
        retrieval_runtime.load_policy(path)
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ValueError('Historical controller configuration is invalid: ' + str(exc)) from exc
    return str(path)


def preflight_policy(policy_path, *, auth_home=None):
    """Copy login only and exercise existing sandbox checks before model work.

    A failure names its real cause, on the console too. One that could not
    finish (a timeout, no connectivity, an early exit) is a PreflightRetry,
    which the server tries again every minute.
    """
    try:
        policy = retrieval_runtime.load_policy(policy_path)
        auth_home = Path(auth_home or os.environ.get('TCS_PROVER_UI_AUTH_HOME') or
                         os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
        source = auth_home / 'auth.json'
        if not source.is_file() or source.is_symlink():
            raise ValueError('Codex login is unavailable. Run codex login before starting historical search.')
        raw = source.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or not (value.get('tokens') or value.get('OPENAI_API_KEY')):
            raise ValueError('Codex login is unavailable. Run codex login before starting historical search.')
        homes = [retrieval_runtime.role_home(policy, role) for role in ('author', 'structured')]
        if policy.get('_discoverySettings'):
            homes.append(Path(policy['_discoverySettings']['auth_home']))
        for home in homes:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = home / 'auth.json'
            if target.is_symlink():
                raise ValueError('Isolated Codex login must not be a symbolic link.')
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(raw)
            target.chmod(0o600)
        # A copy made earlier may be stale: the newest valid login wins.
        retrieval_runtime.sync_login(policy, homes, source=source)
        broker = policy['broker']
        result = subprocess.run([broker['command'], *broker['args'], '--preflight'],
                                capture_output=True, text=True, timeout=30, check=True)
        broker_report = json.loads(result.stdout)
        if (broker_report.get('status') != 'ready'
                or policy.get('_discoverySettings') and (broker_report.get('provider') != 'codex_wayback'
                                                        or broker_report.get('discovery_preflight') != 'ready')):
            raise ValueError('Historical archive broker preflight failed.')
        executable = retrieval_runtime.codex_executable(policy)
        for role in ('author', 'structured'):
            # No process follows this check: its private temp folder goes at once.
            retrieval_runtime.remove_private_temp(
                policy, retrieval_runtime.preflight(policy, role, executable, dict(os.environ)))
    except (retrieval_runtime.RetrievalPolicyError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        message = f'Historical search preflight failed; no model was started: {_cause(exc)}.'
        _log(message)
        if isinstance(exc, retrieval_runtime.NeedsAttention):
            raise NeedsAttention(message) from exc
        raise (PreflightRetry if _transient(exc) else ValueError)(message) from exc
    except ValueError as exc:
        _log(f'Historical search preflight failed; no model was started: {exc}')
        raise


def preflight_compiler(policy_path):
    if not isolated_compiler.supported():
        raise ValueError(PLATFORM_REASON)
    try:
        isolated_compiler.preflight(retrieval_runtime.load_policy(policy_path))
    except (isolated_compiler.CompilerIsolationError, retrieval_runtime.RetrievalPolicyError, OSError,
            subprocess.SubprocessError) as exc:
        message = f'Historical compiler preflight failed; no model was started: {_cause(exc)}.'
        _log(message)
        raise (PreflightRetry if _transient(exc) else ValueError)(message) from exc


def _read_policy_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise NeedsAttention(f'The historical retrieval policy {path} is missing.')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise NeedsAttention(f'The historical retrieval policy {path} cannot be read: {exc}') from exc
    broker, isolation = (value.get('broker'), value.get('isolation')) if isinstance(value, dict) else (None, None)
    if (not isinstance(value, dict) or value.get('mode') != 'historical' or value.get('schemaVersion') != 1
            or not isinstance(broker, dict) or not isinstance(isolation, dict)
            or not {'command', 'args', 'policyFile', 'auditLog'} <= set(broker)
            or not isinstance(broker['args'], list) or not broker['args']
            or not {'codexHome', 'workspace'} <= set(isolation)
            or not isinstance(value.get('cutoffExclusiveUtc'), str)):
        raise NeedsAttention(f'The historical retrieval policy {path} is not a valid policy.')
    return value


def _replace_bytes(path, data):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def relocate_policy(policy_path, run_dir, *, controller_root=None):
    """Adopt this run's own moved controller bundle, rewriting only its paths (audited).

    Covers a moved checkout or runs folder and a recreated virtualenv. The
    bundle must be this run's own: its runId, input digest and pinned broker
    policy hash. The cutoff, provider settings and the author's isolated Codex
    home (its conversation) are kept. Returns the policy path to use.
    """
    path, run_dir = Path(policy_path), Path(run_dir).resolve()
    if path.is_file() and not path.is_symlink():
        old_root = new_root = path.parent
    else:
        new_root = Path(controller_root or ROOT / '.experiment-control' / 'ui').resolve() / path.parent.name
        if not (new_root / 'retrieval-policy.json').is_file():
            raise NeedsAttention(f'The historical retrieval policy {path} is missing and its controller folder '
                                 'was not found in this checkout. Restore the .experiment-control folder or move '
                                 'the checkout back, then continue the run.')
        old_root = path.parent
    target = new_root / 'retrieval-policy.json'
    value = _read_policy_json(target)
    broker, isolation = value.get('broker') or {}, value.get('isolation') or {}
    args = list(broker.get('args') or [])
    old_workspace = Path(str(isolation.get('workspace', '')))
    script = Path(args[0]) if args else None
    command = Path(str(broker.get('command', '')))
    if (old_root == new_root and old_workspace.resolve() == run_dir and command.is_file()
            and script is not None and (script.is_file() or script == retrieval_runtime.LEGACY_BROKER_SCRIPT)):
        return str(target)
    # Bind to this run before rewriting anything.
    input_path = run_dir / 'input.md'
    expected_input = (value.get('metadata') or {}).get('inputSha256')
    broker_path = new_root / 'broker-policy.json'
    try:
        broker_bytes = broker_path.read_bytes()
        pinned = args[args.index('--policy-sha256') + 1]
    except (OSError, ValueError, IndexError) as exc:
        raise NeedsAttention(f'The controller folder {new_root} has no usable broker policy.') from exc
    if (value.get('runId') != run_dir.name or not input_path.is_file()
            or (expected_input and hashlib.sha256(input_path.read_bytes()).hexdigest() != expected_input)
            or hashlib.sha256(broker_bytes).hexdigest() != pinned):
        raise NeedsAttention(f'The controller folder {new_root} does not belong to this run; restore the original '
                             'one, then continue the run.')

    def moved(text):
        text = str(text)
        for old, new in ((str(old_root), str(new_root)), (str(old_workspace), str(run_dir)),
                         (str(old_root.parent), str(new_root.parent))):
            if text == old or text.startswith(old + os.sep):
                return new + text[len(old):]
        return text

    broker_policy = json.loads(broker_bytes)
    for key in ('solver_workspace', 'rate_limit_directory'):
        if isinstance(broker_policy.get(key), str):
            broker_policy[key] = moved(broker_policy[key])
    discovery = broker_policy.get('discovery')
    if isinstance(discovery, dict):
        for key in ('auth_home', 'state_root'):
            discovery[key] = moved(discovery[key])
    new_broker = (json.dumps(broker_policy, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    new_command = str(command) if command.is_file() else str(Path(sys.executable).absolute())
    new_script = (str(script) if script is not None and script.is_file() and moved(script) == str(script)
                  else str(retrieval_runtime.CURRENT_BROKER_SCRIPT))
    relocated = copy.deepcopy(value)
    relocated['broker'] = {**broker, 'command': new_command, 'policyFile': str(broker_path),
                           'auditLog': moved(broker['auditLog'])}
    relocated['broker']['args'] = [new_script, '--cutoff', value['cutoffExclusiveUtc'],
                                   '--policy-file', str(broker_path), '--policy-sha256',
                                   hashlib.sha256(new_broker).hexdigest(), '--audit-log', moved(broker['auditLog'])]
    relocated['isolation'] = {**isolation, 'codexHome': moved(isolation['codexHome']), 'workspace': str(run_dir)}
    old_text, old_broker = target.read_bytes(), broker_bytes
    try:
        _replace_bytes(broker_path, new_broker)
        _replace_bytes(target, (json.dumps(relocated, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        policy = retrieval_runtime.load_policy(target)
        retrieval_runtime.controller_audit(
            policy, 'policy_relocated', previous_controller=str(old_root), controller=str(new_root),
            previous_workspace=str(old_workspace), workspace=str(run_dir),
            previous_policy_sha256=hashlib.sha256(old_text).hexdigest(), policy_sha256=policy['_sha256'])
    except (retrieval_runtime.RetrievalPolicyError, retrieval_runtime.IsolationCheckUnavailable, OSError) as exc:
        _replace_bytes(broker_path, old_broker)
        _replace_bytes(target, old_text)
        raise NeedsAttention(f'The moved historical controller folder {new_root} could not be adopted: '
                             f'{_cause(exc)}.') from exc
    # The isolated configs name the broker by path: map exactly those paths.
    replacements = [(str(old_root), str(new_root)), (str(old_workspace), str(run_dir)),
                    (broker.get('command', ''), new_command), (args[0] if args else '', new_script),
                    (pinned, relocated['broker']['args'][6])]
    for role in ('author', 'structured'):
        config = retrieval_runtime.role_home(policy, role) / 'config.toml'
        if not config.is_file() or config.is_symlink():
            continue
        text = config.read_text(encoding='utf-8')
        for old, new in replacements:
            if old:
                text = text.replace(json.dumps(str(old))[1:-1], json.dumps(str(new))[1:-1])
        try:
            import tomllib
            retrieval_runtime._validate_user_configuration(policy, role, tomllib.loads(text), refresh_environment=True)
        except (ValueError, retrieval_runtime.RetrievalPolicyError):
            continue  # Left as is: its own check names what differs.
        config.write_text(retrieval_runtime.config_text(policy, role), encoding='utf-8')
    return str(target)


def check_run(policy_path, run_dir, *, controller_root=None):
    """Recheck a historical run before any of its processes starts again.

    Adopts a moved bundle (relocate_policy), migrates the isolated configs and
    re-pins discovery after a Codex update. A problem no retry can fix raises
    NeedsAttention: the server holds the job with that message instead of
    relaunching it every minute. Returns the policy path to use.
    """
    try:
        path = relocate_policy(policy_path, run_dir, controller_root=controller_root)
        policy = retrieval_runtime.load_policy(path)
        if Path(policy['isolation']['workspace']).resolve() != Path(run_dir).resolve():
            raise retrieval_runtime.NeedsAttention('The historical policy belongs to another run folder.')
        for role in ('author', 'structured'):
            config = retrieval_runtime.role_home(policy, role) / 'config.toml'
            if config.exists() or config.is_symlink():
                retrieval_runtime._verify_saved_configuration(
                    policy, role, config, retrieval_runtime.config_text(policy, role))
        if policy.get('_discoverySettings'):
            retrieval_runtime._ensure_discovery_binary(policy, retrieval_runtime.codex_executable(policy))
        return path
    except NeedsAttention:
        raise
    except retrieval_runtime.IsolationCheckUnavailable as exc:
        raise PreflightRetry(f'Historical run check could not finish: {_cause(exc)}.') from exc
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise NeedsAttention(f'{_cause(exc)}.') from exc


def _empty_info(mode='live'):
    return {'mode': mode, 'cutoffExclusiveUtc': '-1' if mode == 'disabled' else '', 'provider': '', 'scope': 'author',
            'fixed': False, 'available': True,
            'counts': {key: 0 for key in ('searches', 'opens', 'finds', 'successes', 'errors', 'pending', 'verifiedCandidates', 'busy', 'cancelled', 'deadlines')},
            'requestFailures': [], 'lastCompleted': None,
            'lastSuccessAt': '', 'lastFailureAt': '',
            'lastEventAt': '', 'limitation': LIMITATION,
            'countBasis': 'Broker operations; pending means no recorded terminal event, not confirmed running. Verified candidates are not returned-source counts.'}


class _RequestFold:
    """_request_metadata() folded one record at a time.

    Explains terminal request failures, never individual retries or candidates.
    """

    def __init__(self, modern):
        self.modern = modern
        self.operations, self.terminal, self.failures = {}, set(), {}
        self.latest = {'any': None, 'success': None, 'error': None}
        self.result = {'requestFailures': [], 'lastCompleted': None,
                       'lastSuccessAt': '', 'lastFailureAt': ''}

    def _completed(self, tool, status, event, category=None):
        if category is not None:
            self.failures[category] = self.failures.get(category, 0) + 1
        try:
            at = historical_search.parse_cutoff(event.get('time'))
            stamp = historical_search.utc_text(at)
        except (historical_search.HistoricalSearchError, TypeError):
            at, stamp = None, ''
        latest, result = self.latest, self.result
        if result['lastCompleted'] is None or (at is not None and (latest['any'] is None or at >= latest['any'])):
            latest['any'] = at
            result['lastCompleted'] = {'tool': tool, 'status': status, 'time': stamp}
        if status in ('success', 'error') and at is not None and (latest[status] is None or at >= latest[status]):
            latest[status] = at
            result['lastSuccessAt' if status == 'success' else 'lastFailureAt'] = stamp

    def feed(self, event):
        name = event.get('event')
        operation = event.get('operation_id')
        operation = operation if isinstance(operation, str) and operation else None
        if operation is not None and operation in self.terminal:
            return
        details = self.operations.setdefault(operation, {'tool': 'unknown', 'code': None, 'oversized': False}) if operation is not None else {'tool': 'unknown', 'code': None, 'oversized': False}
        if name in REQUEST_TOOLS:
            details['tool'] = REQUEST_TOOLS[name]
        if name == 'http_failure' and event.get('code') == 'response_too_large':
            details['oversized'] = True
        if name in REQUEST_ERRORS:
            code = event.get('failure_code')
            details['code'] = code if code in REQUEST_FAILURE_LABELS or code == 'deadline_exceeded' else 'unknown'
        if self.modern:
            if name == 'mcp_busy':
                if operation is not None:
                    self.terminal.add(operation)
                    self.operations.pop(operation, None)
                self._completed(details['tool'], 'error', event, 'busy')
                return
            if name not in MCP_TERMINALS or operation is None:
                return
            failed = name == 'mcp_deadline' or event.get('is_error') is True
            status = 'cancelled' if name == 'mcp_cancelled' else 'error' if failed else 'success'
            self.terminal.add(operation)
        else:
            if name not in REQUEST_SUCCESSES | REQUEST_ERRORS:
                return
            status = 'error' if name in REQUEST_ERRORS else 'success'
            if operation is not None:
                self.terminal.add(operation)
        if operation is not None:
            self.operations.pop(operation, None)
        category = None
        if status == 'error':
            category = details['code'] or 'unknown'
            if name == 'mcp_deadline' or category == 'deadline_exceeded':
                category = 'deadline'
            elif category == 'no_capture' and details['oversized']:
                category = 'response_too_large'
        self._completed(details['tool'], status, event, category)

    def snapshot(self):
        result = copy.deepcopy(self.result)
        result['requestFailures'] = [{'code': code, 'label': label, 'count': self.failures[code]}
                                     for code, label in REQUEST_FAILURE_LABELS.items() if self.failures.get(code)]
        return result


def _request_metadata(records, modern):
    """Explain terminal request failures, never individual retries or candidates."""
    fold = _RequestFold(modern)
    for event in records:
        fold.feed(event)
    return fold.snapshot()


class _AuditTotals:
    """Dashboard totals of one audit log, folded from newly appended lines only."""

    def __init__(self):
        self.counts = {key: 0 for key in _empty_info()['counts']}
        self.accepted, self.terminal, self.legacy_terminal = set(), set(), set()
        self.modern = False
        self.legacy_success = self.legacy_error = 0
        self.last_event, self.skipped = '', 0
        self.requests = {True: _RequestFold(True), False: _RequestFold(False)}
        self.offset, self.identity, self.fingerprint = 0, None, b''
        self.partial = self.catching_up = False

    def feed(self, event):
        counts, name = self.counts, event.get('event')
        self.modern |= name in MCP_TERMINALS | {'mcp_accepted', 'mcp_busy'}
        counts['searches'] += name == 'search_requested'
        counts['opens'] += name == 'open_requested'
        counts['finds'] += name == 'find_requested'
        counts['verifiedCandidates'] += name == 'capture_verified'
        operation = event.get('operation_id')
        if name in REQUEST_SUCCESSES | REQUEST_ERRORS and (not isinstance(operation, str) or operation not in self.legacy_terminal):
            self.legacy_success += name in REQUEST_SUCCESSES
            self.legacy_error += name in REQUEST_ERRORS
            if isinstance(operation, str):
                self.legacy_terminal.add(operation)
        if name == 'mcp_accepted' and isinstance(operation, str):
            self.accepted.add(operation)
        if name in MCP_TERMINALS and isinstance(operation, str) and operation not in self.terminal:
            self.terminal.add(operation)
            counts['cancelled'] += name == 'mcp_cancelled'
            counts['deadlines'] += name == 'mcp_deadline'
            if name != 'mcp_cancelled':
                failed = name == 'mcp_deadline' or event.get('is_error') is True
                counts['errors' if failed else 'successes'] += 1
        if name == 'mcp_busy' and (not isinstance(operation, str) or operation not in self.terminal):
            if isinstance(operation, str):
                self.terminal.add(operation)
            counts['busy'] += 1
            counts['errors'] += 1
        event_time = event.get('time')
        if isinstance(event_time, str):
            try:
                self.last_event = historical_search.utc_text(historical_search.parse_cutoff(event_time))
            except historical_search.HistoricalSearchError:
                pass
        for fold in self.requests.values():
            fold.feed(event)

    def info(self):
        counts = dict(self.counts)
        if self.modern:
            counts['pending'] = len(self.accepted - self.terminal)
        else:
            counts.update(successes=self.legacy_success, errors=self.legacy_error,
                          pending=max(0, counts['searches'] + counts['opens'] + counts['finds']
                                      - self.legacy_success - self.legacy_error))
        return {'counts': counts, 'lastEventAt': self.last_event, 'partialTrailingLine': self.partial,
                'skippedRecords': self.skipped, 'catchingUp': self.catching_up,
                **self.requests[self.modern].snapshot()}


def _audit_fingerprint(stream, offset):
    """Hash the first bytes and the bytes before offset; a rewritten log keeps its inode."""
    stream.seek(0)
    head = stream.read(min(offset, FINGERPRINT_BYTES))
    start = max(0, offset - FINGERPRINT_BYTES)
    stream.seek(start)
    return hashlib.sha256(head + b'\n' + stream.read(offset - start)).digest()


_AUDIT_LOCK = threading.Lock()


def _audit_totals(audit, cutoff):
    """Fold only what was appended since the last refresh, so a large log keeps its totals.

    Malformed or foreign (other-cutoff) records are counted and skipped.
    """
    if not audit.exists():
        return _AuditTotals().info()
    if audit.is_symlink():
        raise ValueError('Audit cannot be read safely.')
    key = (str(audit), cutoff)
    with _AUDIT_LOCK:
        with audit.open('rb') as stream:
            status = os.fstat(stream.fileno())
            identity = (status.st_dev, status.st_ino)
            totals = _AUDIT_TOTALS.get(key)
            if (totals is None or totals.identity != identity or totals.offset > status.st_size
                    or _audit_fingerprint(stream, totals.offset) != totals.fingerprint):
                totals = _AuditTotals()
                totals.identity = identity
            stream.seek(totals.offset)
            data = stream.read(MAX_AUDIT_READ_BYTES)
            complete = data[:data.rfind(b'\n') + 1]
            if not complete and len(data) == MAX_AUDIT_READ_BYTES:
                complete, totals.skipped = data, totals.skipped + 1  # One oversized line.
            for line in complete.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    totals.skipped += 1
                    continue
                if not isinstance(record, dict) or record.get('cutoff') != cutoff:
                    totals.skipped += 1
                    continue
                totals.feed(record)
            totals.offset += len(complete)
            totals.catching_up = totals.offset + (len(data) - len(complete)) < status.st_size
            totals.partial = not totals.catching_up and len(data) != len(complete)
            totals.fingerprint = _audit_fingerprint(stream, totals.offset)
        _AUDIT_TOTALS[key] = totals
        _AUDIT_TOTALS.move_to_end(key)
        while len(_AUDIT_TOTALS) > 64:
            _AUDIT_TOTALS.popitem(last=False)
        return totals.info()


def _file_signature(path):
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def display_policy(path):
    """Load a policy for dashboard display without rehashing unchanged files.

    The memo is rechecked against the policy, broker files and pinned Codex
    binary on every call. Launch, resume and the broker never use it; they
    still run the full load_policy and discovery hash checks.
    """
    key = str(path)
    with _CACHE_LOCK:
        saved = _POLICY_MEMO.get(key)
    if saved is not None and all(_file_signature(item) == signature for item, signature in saved[0]):
        return copy.deepcopy(saved[1])
    policy = retrieval_runtime.load_policy(path)
    broker = policy['broker']
    # The files load_policy hashed, and a legacy broker path it mapped away
    # (which would be used again if it reappeared).
    files = [key, broker['policyFile'], broker['command'], broker['args'][0], *policy['_brokerModuleSha256'],
             *([policy['_legacyBrokerArgs0']] if policy.get('_legacyBrokerArgs0') else [])]
    if policy.get('_discoverySettings'):
        from retrieval import historical_codex_discovery as discovery
        settings = policy['_discoverySettings']
        # The binary discovery runs and its replacements (a re-pin, the kept link).
        files.extend((settings['codex_path'], str(discovery._controller(settings) / discovery.REPIN_RECORD),
                      str(discovery.kept_binary(settings))))
    with _CACHE_LOCK:
        if len(_POLICY_MEMO) > 256:
            _POLICY_MEMO.clear()
        _POLICY_MEMO[key] = (tuple((item, _file_signature(item)) for item in files), copy.deepcopy(policy))
    return policy


def public_retrieval_info(policy_path, *, run_dir=None, mode='live', saved_cutoff=''):
    """Return whitelisted aggregate metadata only; never source or query content.

    A five-second cache bounds repeated dashboard reads. Totals are folded
    incrementally from each audit log (see _audit_totals), however large.
    ``saved_cutoff`` is the job's recorded cutoff; it stays visible when the
    policy metadata cannot be loaded, and a loaded policy always overrides it.
    """
    if not policy_path:
        return _empty_info(mode if mode in ('live', 'disabled') else 'disabled')
    try:
        saved_cutoff = historical_search.utc_text(historical_search.parse_cutoff(saved_cutoff))
    except historical_search.HistoricalSearchError:
        saved_cutoff = ''
    key = (str(policy_path), str(run_dir or ''), saved_cutoff)
    with _CACHE_LOCK:
        saved = _CACHE.get(key)
        if saved and time.monotonic() - saved[0] < 5:
            return copy.deepcopy(saved[1])
    info = _empty_info()
    info.update(mode='historical', fixed=True, available=False, cutoffExclusiveUtc=saved_cutoff)
    try:
        path = Path(policy_path)
        if path.is_symlink() or not path.is_absolute():
            raise ValueError('Invalid controller policy path.')
        policy = display_policy(path)
        if run_dir is not None and Path(policy['isolation']['workspace']).resolve() != Path(run_dir).resolve():
            raise ValueError('Controller workspace mismatch.')
        info['cutoffExclusiveUtc'] = policy['cutoffExclusiveUtc']
        broker = json.loads(Path(policy['broker']['policyFile']).read_text(encoding='utf-8'))
        provider = broker.get('provider')
        info['provider'] = provider if provider in ('codex_wayback', 'scholarly_wayback', 'brave_wayback', 'duckduckgo_wayback', 'sealed_corpus') else 'unknown'
        info.update(_audit_totals(Path(policy['broker']['auditLog']), policy['cutoffExclusiveUtc']))
        if policy.get('_discoveryProblem'):
            info['attention'] = policy['_discoveryProblem']
        info['available'] = True
    except (OSError, ValueError, TypeError, KeyError, retrieval_runtime.RetrievalPolicyError):
        info['error'] = 'Historical search metadata is unavailable; the saved retrieval policy remains fixed.'
    with _CACHE_LOCK:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), copy.deepcopy(info))
    return info
