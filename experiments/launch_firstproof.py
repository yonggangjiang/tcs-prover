#!/usr/bin/env python3
"""Preflight the pinned First Proof batch, or start it with a persistent local UI."""

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

from experiments import prepare_firstproof as preparation
from retrieval import runtime as retrieval
from ui.server import HOST, Server, saved_statement


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def batch_plan(runs_root, control_root, problems=None):
    """Verify exactly the prepared subset before credentials or model calls."""
    runs_root, control_root = Path(runs_root).resolve(), Path(control_root).resolve()
    manifest_path = preparation.EXPERIMENT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    prepared = json.loads((control_root / 'prepared-runs.json').read_text())
    if (prepared.get('schemaVersion') != 2 or prepared.get('manifest') != manifest
            or prepared.get('navigationVersion') != preparation.NAVIGATION_VERSION
            or prepared.get('discoveryProvider') != preparation.DISCOVERY_PROVIDER
            or prepared.get('manifestSha256') != preparation.sha(manifest_path)
            or prepared.get('runsRoot') != str(runs_root)
            or prepared.get('controlRoot') != str(control_root)
            or prepared.get('status') != 'PREPARED_HELD'):
        raise ValueError('Preparation record does not match the pinned manifest and batch roots.')
    selected_ids = prepared.get('selectedProblemIds')
    if not selected_ids or preparation.selected_problems(manifest, selected_ids) != selected_ids:
        raise ValueError('Preparation selection must contain unique problem IDs in manifest order.')
    if problems is not None and preparation.selected_problems(manifest, problems) != selected_ids:
        raise ValueError('Requested problem selection does not match the prepared batch.')
    recorded_runs = prepared.get('runs')
    if not isinstance(recorded_runs, list) or [row.get('id') for row in recorded_runs] != selected_ids:
        raise ValueError('Prepared jobs do not match the recorded problem selection.')
    expected_folders = {'firstproof-b3-' + item.lower() for item in selected_ids}
    if any((path.is_dir() or path.is_symlink()) and path.name not in expected_folders
           for path in runs_root.iterdir()):
        raise ValueError('Unselected jobs exist in this runs root; use a fresh batch directory.')
    plans = []
    for item in manifest['runs']:
        if item['id'] not in selected_ids:
            continue
        recorded = recorded_runs[len(plans)]
        run_id = 'firstproof-b3-' + item['id'].lower()
        folder = runs_root / run_id
        if folder.is_symlink():
            raise ValueError(f'{run_id}: prepared job cannot be a symbolic link.')
        settings_path = folder / 'job-settings.json'
        settings = json.loads(settings_path.read_text())
        expected_policy = control_root / item['id'] / 'retrieval-policy.json'
        if (recorded.get('runId') != run_id or recorded.get('folder') != str(folder)
                or recorded.get('policy') != str(expected_policy)
                or recorded.get('cutoffExclusiveUtc') != item['cutoffExclusiveUtc']
                or recorded.get('inputSha256') != item['statementUtf8Sha256']
                or recorded.get('phase') != 'prepared' or recorded.get('modelSessionStarted') is not False):
            raise ValueError(f'{run_id}: prepared job identity or input binding changed.')
        if settings.get('retrievalPolicyPath') != str(expected_policy):
            raise ValueError(f'{run_id}: unexpected controller policy.')
        policy = retrieval.load_policy(expected_policy)
        if policy.get('metadata', {}).get('navigationVersion') != prepared['navigationVersion']:
            raise ValueError(f'{run_id}: prepared navigation interface changed.')
        broker_policy = json.loads(Path(policy['broker']['policyFile']).read_text())
        discovery = policy.get('_discoverySettings', {})
        if (policy.get('metadata', {}).get('discoveryProvider') != prepared['discoveryProvider']
                or broker_policy.get('provider') != prepared['discoveryProvider']
                or discovery.get('auth_home') != str(control_root / item['id'] / 'discovery-auth')
                or discovery.get('state_root') != str(control_root / item['id'] / 'discovery-state')
                or discovery.get('model') != settings.get('authorModel')
                or discovery.get('timeout_seconds') != 120):
            raise ValueError(f'{run_id}: prepared discovery settings changed.')
        if (policy['runId'] != run_id or policy['cutoffExclusiveUtc'] != item['cutoffExclusiveUtc']
                or Path(policy['isolation']['workspace']).resolve() != folder):
            raise ValueError(f'{run_id}: policy identity, workspace or cutoff mismatch.')
        if (hashlib.sha256((folder / 'input.md').read_bytes()).hexdigest() != item['statementUtf8Sha256']
                or saved_statement(folder) != (folder / 'input.md').read_text().strip()):
            raise ValueError(f'{run_id}: official statement changed.')
        defaults = preparation.empty_state()
        required = {key: defaults[key] for key in (
            'authorModel', 'criticModel', 'writerModel', 'authorEffort',
            'criticEffort', 'writerEffort', 'criticRounds', 'researchAudits',
        )}
        required.update(thinkingHours=0, speedMode='fast', fileManagement=False,
                        skipStatementReview=True, preparedRun=True)
        if any(settings.get(key) != value for key, value in required.items()):
            raise ValueError(f'{run_id}: prepared run settings changed or run already started.')
        prompts = preparation.default_prompts()
        for role in ('review', 'author', 'critic', 'final'):
            expected = prompts['author_simple' if role == 'author' else role]
            if (folder / 'prompts' / (role + '.txt')).read_text().rstrip() != expected.rstrip():
                raise ValueError(f'{run_id}: prepared {role} prompt changed.')
        if settings.get('goalThreadId') or (folder / 'transcript.jsonl').exists():
            raise ValueError(f'{run_id}: existing model work; use the UI to resume it.')
        expected_entries = {'input.md', 'draft.md', 'checked-statement.md', 'job-settings.json',
                            'prompts', *retrieval.PROTECTED_DIRECTORIES}
        if any(path.name not in expected_entries for path in folder.iterdir()):
            raise ValueError(f'{run_id}: existing work in prepared directory; use a fresh batch.')
        if not settings.get('launchHold') or settings['launchHold'] != recorded.get('launchHold'):
            raise ValueError(f'{run_id}: launch hold was released or changed.')
        if preparation.sha(settings_path) != recorded.get('settingsSha256'):
            raise ValueError(f'{run_id}: prepared run settings changed.')
        if preparation.sha(expected_policy) != recorded.get('policySha256'):
            raise ValueError(f'{run_id}: prepared retrieval policy changed.')
        plans.append({'id': item['id'], 'runId': run_id, 'folder': folder,
                      'settingsPath': settings_path, 'settings': settings, 'policy': policy})
    return plans


def provision_auth(policy, auth_home):
    """Copy only authentication; never import ambient config, skills or sessions."""
    source = Path(auth_home).expanduser().resolve() / 'auth.json'
    if not source.is_file() or source.is_symlink():
        raise ValueError(f'Codex login is missing from {source.parent}. Run codex login first.')
    raw = source.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not (data.get('tokens') or data.get('OPENAI_API_KEY')):
        raise ValueError('Codex authentication has neither login tokens nor an API key.')
    homes = [retrieval.role_home(policy, role) for role in ('author', 'structured')]
    # Recheck the exact broker policy before following its credential destination.
    broker = policy['broker']
    policy_raw = Path(broker['policyFile']).read_bytes()
    args = broker['args']
    if hashlib.sha256(policy_raw).hexdigest() != args[args.index('--policy-sha256') + 1]:
        raise ValueError('Historical broker policy changed before credential provisioning.')
    broker_policy = json.loads(policy_raw)
    if broker_policy.get('provider') == preparation.DISCOVERY_PROVIDER:
        discovery = policy.get('_discoverySettings')
        if not isinstance(discovery, dict) or discovery != broker_policy.get('discovery'):
            raise ValueError('Historical discovery settings were not validated before credential provisioning.')
        discovery_home = Path(discovery['auth_home'])
        if discovery_home != Path(policy['_path']).parent / 'discovery-auth':
            raise ValueError('Historical discovery authentication must use its protected controller home.')
        homes.append(discovery_home)
    for home in homes:
        if home.is_symlink() or home.resolve() != home.absolute():
            raise ValueError('Isolated authentication home cannot be a symbolic link.')
        if (home / 'auth.json').is_symlink():
            raise ValueError('Isolated authentication cannot be a symbolic link.')
    for home in homes:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        target = home / 'auth.json'
        if target.is_symlink():
            raise ValueError('Isolated authentication cannot be a symbolic link.')
        if not target.exists():
            # Keep refreshed isolated credentials when rechecking a prepared batch.
            with target.open('xb') as stream:
                stream.write(raw)
        target.chmod(0o600)


def preflight_one(plan, auth_home):
    policy = plan['policy']
    provision_auth(policy, auth_home)
    broker = policy['broker']
    result = subprocess.run([broker['command'], *broker['args'], '--preflight'],
                            capture_output=True, text=True, timeout=30, check=True)
    broker_report = json.loads(result.stdout)
    if (broker_report.get('status') != 'ready' or not broker_report.get('pdf_supported')
            or broker_report.get('provider') != preparation.DISCOVERY_PROVIDER
            or broker_report.get('discovery_preflight') != 'ready'):
        raise ValueError(f"{plan['runId']}: broker preflight failed.")
    executable = retrieval.codex_executable(policy)
    for role in ('author', 'structured'):
        retrieval.preflight(policy, role, executable, dict(os.environ))
    return {'id': plan['id'], 'runId': plan['runId'], 'inputSha256': preparation.sha(plan['folder'] / 'input.md'),
            'cutoffExclusiveUtc': policy['cutoffExclusiveUtc'], 'codex': executable,
            'rolesVerified': ['author', 'structured'], 'broker': broker_report}


def preflight_batch(plans, auth_home, control_root):
    from retrieval.isolated_compiler import preflight as compiler_preflight
    report = {'checkedAtUtc': datetime.now(timezone.utc).isoformat(),
              'platform': 'macOS', 'modelsStarted': 0,
              'selectedProblemIds': [plan['id'] for plan in plans], 'runs': []}
    # Limit simultaneous probes to avoid local startup/network spikes.
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(preflight_one, plan, auth_home): plan for plan in plans}
        for future in as_completed(pending):
            row = future.result()
            report['runs'].append(row)
            print(f"{row['id']}: input, broker and both role sandboxes verified", flush=True)
    report['runs'].sort(key=lambda row: row['id'])
    report['compiler'] = compiler_preflight(plans[0]['policy'])
    if report['compiler'].get('status') != 'pass':
        raise ValueError('Isolated compiler preflight failed.')
    report['status'] = 'PASSED'
    write_json(Path(control_root) / 'macos-preflight.json', report)
    return report


def serve_batch(plans, control_root, port):
    """Bind before releasing holds; keep UI, retries and status monitoring alive."""
    control_root = Path(control_root)
    if not plans:
        raise ValueError('No prepared problems selected.')
    verified = batch_plan(plans[0]['folder'].parent, control_root,
                          problems=[plan['id'] for plan in plans])
    if [(plan['runId'], plan['folder']) for plan in plans] != [
            (plan['runId'], plan['folder']) for plan in verified]:
        raise ValueError('Launch plans do not match the prepared batch.')
    plans = verified
    server = Server((HOST, port), runs=plans[0]['folder'].parent)
    url = f'{server.origin}/#{server.token}'
    status_path = control_root / 'launch-status.json'
    stopped = threading.Event()
    started = []

    def save_status():
        rows = []
        for plan in plans:
            app = server.jobs.get(plan['runId'])
            if app is None:
                rows.append({'runId': plan['runId'], 'phase': 'unavailable', 'pid': None})
                continue
            state = app.snapshot()
            rows.append({key: state.get(key) for key in (
                'runId', 'phase', 'activeNode', 'startedAt', 'finishedAt',
                'error', 'goalThreadId', 'autoResumeAt',
            )})
            rows[-1]['pid'] = app.process.pid if app.process and app.process.poll() is None else None
        write_json(status_path, {'controllerPid': os.getpid(), 'url': url,
                                'selectedProblemIds': [plan['id'] for plan in plans],
                                'updatedAtUtc': datetime.now(timezone.utc).isoformat(), 'runs': rows})

    try:
        if set(server.jobs) != {plan['runId'] for plan in plans}:
            raise ValueError('Restored jobs do not match the prepared problem selection.')
        for plan in plans:
            app = server.jobs.get(plan['runId'])
            if app is None or app.state['phase'] != 'prepared' or app.has_active_worker():
                raise ValueError(f"{plan['runId']}: no longer a prepared idle job.")
        for plan in plans:
            # The --start invocation records current authorization separately
            # from the preserved Windows handoff's historical choices.
            policy = json.loads(Path(plan['policy']['_path']).read_text())
            original_policy = json.loads(json.dumps(policy))
            policy['metadata']['userApprovedLaunch'] = True
            write_json(plan['policy']['_path'], policy)
            app = server.jobs[plan['runId']]
            old_hold = app.state['launchHold']
            app.state['launchHold'] = ''
            app._save_job_settings(app.state)
            try:
                app.start_prepared_run()
            except BaseException:
                app.state['launchHold'] = old_hold
                app._save_job_settings(app.state)
                write_json(plan['settingsPath'], plan['settings'])
                write_json(plan['policy']['_path'], original_policy)
                raise
            started.append(plan['runId'])
            print(f"{plan['id']}: started", flush=True)
        save_status()
        print(f'TCS Prover batch UI: {url}', flush=True)

        def monitor():
            while not stopped.wait(5):
                save_status()
        threading.Thread(target=monitor, daemon=True).start()
        server.serve_forever()
    except BaseException:
        if started:
            server.stop_all()
        raise
    finally:
        stopped.set()
        save_status()
        server.server_close()


@contextmanager
def batch_lock(control_root):
    """Prevent two launchers from releasing/starting the same prepared jobs."""
    import fcntl
    path = Path(control_root) / 'launcher.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another launcher already controls this batch.') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true', help='After all preflights pass, release holds and start the prepared jobs.')
    parser.add_argument('--runs-root', type=Path, default=preparation.ROOT / 'runs')
    parser.add_argument('--control-root', type=Path, default=preparation.ROOT / '.experiment-control/firstproof-batch3')
    parser.add_argument('--auth-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--problems', nargs='+', metavar='PXX',
                        help='Require this exact prepared subset (default: use the preparation record).')
    args = parser.parse_args(argv)
    with batch_lock(args.control_root):
        plans = batch_plan(args.runs_root, args.control_root, args.problems)
        preflight_batch(plans, args.auth_home, args.control_root)
        if args.start:
            serve_batch(plans, args.control_root, args.port)
        else:
            print('All preflights passed; launch holds remain in place. Use --start to launch.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
