"""Controller-owned historical search setup and content-free UI metadata."""
from __future__ import annotations

import copy
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
MAX_AUDIT_BYTES = 32 * 1024 * 1024
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


def capabilities():
    # Model roles also have a Windows MXC adapter, but every historical run
    # ends in the isolated compiler, which supports only macOS.
    available = isolated_compiler.supported()
    return {'historicalAvailable': available, 'defaultMode': 'live', 'scope': 'author',
            'reason': '' if available else PLATFORM_REASON,
            'requiresDirect': True, 'requiresCodexModels': True}


def validate_options(mode='live', cutoff='', *, models=(), research_audits=None,
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
    if research_audits and any(model != 'none' for model in research_audits.get('models', [])):
        raise ValueError('Disable optional research auditors when using historical search.')
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
        raise ValueError('Historical runs keep their original workspace and cutoff. Resume the original run; a new continuation cannot discard its retrieval policy.')
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
    except retrieval_runtime.RetrievalPolicyError as exc:
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
    from retrieval.historical_codex_discovery import build_settings
    discovery = build_settings(executable, controller / 'discovery-auth',
                               controller / 'discovery-state', runtime.AUTHOR_MODEL)
    _write_json(broker_path, {
        'schema_version': 1, 'provider': 'codex_wayback', 'max_results': 5,
        'max_candidates': 6, 'max_content_chars': 20000, 'request_timeout_seconds': 15,
        'operation_timeout_seconds': 300, 'solver_workspace': str(workspace),
        'rate_limit_directory': str(rate_directory.resolve()),
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
        'metadata': {'source': 'local-ui', 'navigationVersion': 'full-document-v1',
                     'discoveryProvider': 'codex_wayback',
                     'inputSha256': hashlib.sha256((workspace / 'input.md').read_bytes()).hexdigest()},
    })
    try:
        retrieval_runtime.load_policy(path)
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ValueError('Historical controller configuration is invalid: ' + str(exc)) from exc
    return str(path)


def preflight_policy(policy_path, *, auth_home=None):
    """Copy login only and exercise existing sandbox checks before model work."""
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
            retrieval_runtime.preflight(policy, role, executable, dict(os.environ))
    except (retrieval_runtime.RetrievalPolicyError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        raise ValueError('Historical search preflight failed; no model was started. Check Codex login and native sandbox support.') from exc


def preflight_compiler(policy_path):
    if not isolated_compiler.supported():
        raise ValueError(PLATFORM_REASON)
    try:
        isolated_compiler.preflight(retrieval_runtime.load_policy(policy_path))
    except (isolated_compiler.CompilerIsolationError, retrieval_runtime.RetrievalPolicyError, OSError,
            subprocess.SubprocessError) as exc:
        raise ValueError('Historical compiler preflight failed; no model was started. Check the protected TeX installation and sandbox support.') from exc


def _empty_info(mode='live'):
    return {'mode': mode, 'cutoffExclusiveUtc': '-1' if mode == 'disabled' else '', 'provider': '', 'scope': 'author',
            'fixed': False, 'available': True,
            'counts': {key: 0 for key in ('searches', 'opens', 'finds', 'successes', 'errors', 'pending', 'verifiedCandidates', 'busy', 'cancelled', 'deadlines')},
            'requestFailures': [], 'lastCompleted': None,
            'lastSuccessAt': '', 'lastFailureAt': '',
            'lastEventAt': '', 'limitation': LIMITATION,
            'countBasis': 'Broker operations; pending means no recorded terminal event, not confirmed running. Verified candidates are not returned-source counts.'}


def _request_metadata(records, modern):
    """Explain terminal request failures, never individual retries or candidates."""
    operations, terminal, failures = {}, set(), {}
    latest = {'any': None, 'success': None, 'error': None}
    result = {'requestFailures': [], 'lastCompleted': None,
              'lastSuccessAt': '', 'lastFailureAt': ''}

    def completed(tool, status, event, category=None):
        if category is not None:
            failures[category] = failures.get(category, 0) + 1
        try:
            at = historical_search.parse_cutoff(event.get('time'))
            stamp = historical_search.utc_text(at)
        except (historical_search.HistoricalSearchError, TypeError):
            at, stamp = None, ''
        if result['lastCompleted'] is None or (at is not None and (latest['any'] is None or at >= latest['any'])):
            latest['any'] = at
            result['lastCompleted'] = {'tool': tool, 'status': status, 'time': stamp}
        if status in ('success', 'error') and at is not None and (latest[status] is None or at >= latest[status]):
            latest[status] = at
            result['lastSuccessAt' if status == 'success' else 'lastFailureAt'] = stamp

    for event in records:
        name = event.get('event')
        operation = event.get('operation_id')
        operation = operation if isinstance(operation, str) and operation else None
        if operation is not None and operation in terminal:
            continue
        details = operations.setdefault(operation, {'tool': 'unknown', 'code': None, 'oversized': False}) if operation is not None else {'tool': 'unknown', 'code': None, 'oversized': False}
        if name in REQUEST_TOOLS:
            details['tool'] = REQUEST_TOOLS[name]
        if name == 'http_failure' and event.get('code') == 'response_too_large':
            details['oversized'] = True
        if name in REQUEST_ERRORS:
            code = event.get('failure_code')
            details['code'] = code if code in REQUEST_FAILURE_LABELS or code == 'deadline_exceeded' else 'unknown'
        if modern:
            if name == 'mcp_busy':
                if operation is not None:
                    terminal.add(operation)
                completed(details['tool'], 'error', event, 'busy')
                continue
            if name not in MCP_TERMINALS or operation is None:
                continue
            failed = name == 'mcp_deadline' or event.get('is_error') is True
            status = 'cancelled' if name == 'mcp_cancelled' else 'error' if failed else 'success'
            terminal.add(operation)
        else:
            if name not in REQUEST_SUCCESSES | REQUEST_ERRORS:
                continue
            status = 'error' if name in REQUEST_ERRORS else 'success'
            if operation is not None:
                terminal.add(operation)
        category = None
        if status == 'error':
            category = details['code'] or 'unknown'
            if name == 'mcp_deadline' or category == 'deadline_exceeded':
                category = 'deadline'
            elif category == 'no_capture' and details['oversized']:
                category = 'response_too_large'
        completed(details['tool'], status, event, category)
    result['requestFailures'] = [{'code': code, 'label': label, 'count': failures[code]}
                                 for code, label in REQUEST_FAILURE_LABELS.items() if failures.get(code)]
    return result


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
        files.append(policy['_discoverySettings']['codex_path'])
    with _CACHE_LOCK:
        if len(_POLICY_MEMO) > 256:
            _POLICY_MEMO.clear()
        _POLICY_MEMO[key] = (tuple((item, _file_signature(item)) for item in files), copy.deepcopy(policy))
    return policy


def public_retrieval_info(policy_path, *, run_dir=None, mode='live', saved_cutoff=''):
    """Return whitelisted aggregate metadata only; never source or query content.

    A five-second cache bounds repeated dashboard reads. Large audit files are
    reported unavailable rather than silently presenting tail-only totals.
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
        audit = Path(policy['broker']['auditLog'])
        records = []
        if audit.exists():
            if audit.is_symlink() or audit.stat().st_size > MAX_AUDIT_BYTES:
                raise ValueError('Audit cannot be read safely.')
            with audit.open('rb') as stream:
                raw = stream.read(MAX_AUDIT_BYTES + 1)
            if len(raw) > MAX_AUDIT_BYTES:
                raise ValueError('Audit exceeds dashboard limit.')
            complete = raw[:raw.rfind(b'\n') + 1]
            info['partialTrailingLine'] = len(complete) != len(raw)
            for line in complete.splitlines():
                record = json.loads(line)
                if not isinstance(record, dict) or record.get('cutoff') != policy['cutoffExclusiveUtc']:
                    raise ValueError('Audit cutoff mismatch.')
                records.append(record)
        counts = info['counts']
        accepted, terminal = set(), set()
        modern = any(event.get('event') in MCP_TERMINALS | {'mcp_accepted', 'mcp_busy'} for event in records)
        legacy_success = legacy_error = 0
        legacy_terminal = set()
        for event in records:
            name = event.get('event')
            counts['searches'] += name == 'search_requested'
            counts['opens'] += name == 'open_requested'
            counts['finds'] += name == 'find_requested'
            counts['verifiedCandidates'] += name == 'capture_verified'
            operation = event.get('operation_id')
            if name in REQUEST_SUCCESSES | REQUEST_ERRORS and (not isinstance(operation, str) or operation not in legacy_terminal):
                legacy_success += name in REQUEST_SUCCESSES
                legacy_error += name in REQUEST_ERRORS
                if isinstance(operation, str):
                    legacy_terminal.add(operation)
            if name == 'mcp_accepted' and isinstance(operation, str):
                modern = True
                accepted.add(operation)
            if name in MCP_TERMINALS and isinstance(operation, str) and operation not in terminal:
                terminal.add(operation)
                counts['cancelled'] += name == 'mcp_cancelled'
                counts['deadlines'] += name == 'mcp_deadline'
                if name != 'mcp_cancelled':
                    failed = name == 'mcp_deadline' or event.get('is_error') is True
                    counts['errors' if failed else 'successes'] += 1
            if name == 'mcp_busy' and (not isinstance(operation, str) or operation not in terminal):
                if isinstance(operation, str):
                    terminal.add(operation)
                counts['busy'] += 1
                counts['errors'] += 1
            event_time = event.get('time')
            if isinstance(event_time, str):
                try:
                    info['lastEventAt'] = historical_search.utc_text(historical_search.parse_cutoff(event_time))
                except historical_search.HistoricalSearchError:
                    pass
        if modern:
            counts['pending'] = len(accepted - terminal)
        else:
            counts.update(successes=legacy_success, errors=legacy_error,
                          pending=max(0, counts['searches'] + counts['opens'] + counts['finds'] - legacy_success - legacy_error))
        info.update(_request_metadata(records, modern))
        info['available'] = True
    except (OSError, ValueError, TypeError, KeyError, retrieval_runtime.RetrievalPolicyError):
        info['error'] = 'Historical search metadata is unavailable; the saved retrieval policy remains fixed.'
    with _CACHE_LOCK:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), copy.deepcopy(info))
    return info
