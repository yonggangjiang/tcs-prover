#!/usr/bin/env python3
"""Prepare held First Proof jobs (all ten by default), without model work."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from ui.server import empty_state, default_prompts
from retrieval import runtime as retrieval

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / 'experiments' / 'firstproof-batch3'
NAVIGATION_VERSION = 'full-document-v1'
DISCOVERY_PROVIDER = 'codex_wayback'
HOLD = 'Prepared for user launch on MacBook; compatible isolation and compiler adapters plus host preflight are required.'
SETTING_KEYS = (
    'reviewModel', 'authorModel', 'criticModel', 'writerModel', 'reasoningEffort',
    'reviewEffort', 'authorEffort', 'criticEffort', 'writerEffort', 'criticRounds',
    'thinkingHours', 'speedMode', 'reasoningSummary', 'researchAudits',
    'problemMode', 'skipStatementReview', 'statementReviewOnly', 'fileManagement',
    'preparedRun', 'sourceFile', 'goalThreadId', 'goalWorkspace', 'goalResume',
    'launchHold', 'retrievalPolicyPath',
)

def write_json(path, value, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x' if exclusive else 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def selected_problems(manifest, problems=None):
    """Validate an explicit subset and return it in pinned manifest order."""
    expected_ids = [f'P{n:02}' for n in range(1, 11)]
    if [r['id'] for r in manifest['runs']] != expected_ids:
        raise ValueError('Expected exactly P01 through P10 in public problem order.')
    if problems is None:
        return expected_ids
    if not isinstance(problems, (list, tuple)) or not problems:
        raise ValueError('Select at least one problem ID from P01 through P10.')
    if any(not isinstance(item, str) or item not in expected_ids for item in problems):
        raise ValueError('Unknown problem ID; expected P01 through P10.')
    if len(set(problems)) != len(problems):
        raise ValueError('Selected problem IDs must be unique.')
    return [item for item in expected_ids if item in problems]

def prepare(runs_root=None, control_root=None, problems=None):
    runs_root = Path(runs_root or ROOT / 'runs').resolve()
    control_root = Path(control_root or ROOT / '.experiment-control' / 'firstproof-batch3').resolve()
    if control_root == runs_root or control_root.is_relative_to(runs_root):
        raise ValueError('Controller policies must be outside the solver runs directory.')
    manifest = json.loads((EXPERIMENT / 'manifest.json').read_text(encoding='utf-8'))
    selected_ids = selected_problems(manifest, problems)
    if control_root.exists() and any(control_root.iterdir()):
        raise ValueError('Prepared controller destination already exists; use a fresh control root: ' + str(control_root))
    if runs_root.exists() and any(path.is_dir() or path.is_symlink() for path in runs_root.iterdir()):
        raise ValueError('Prepared run destination already exists; use a fresh runs root: ' + str(runs_root))
    plans = []
    # Validate every selected input and target before writing any run.
    for item in manifest['runs']:
        if item['id'] not in selected_ids:
            continue
        source = EXPERIMENT / 'inputs' / (item['id'] + '.md')
        if sha(source) != item['statementUtf8Sha256']:
            raise ValueError('Official input hash mismatch: ' + item['id'])
        cutoff = datetime.fromisoformat(item['cutoffExclusiveUtc'])
        if cutoff >= datetime.fromisoformat(manifest['publicReleaseUtc']):
            raise ValueError('Historical cutoff must precede publication.')
        run_id = 'firstproof-b3-' + item['id'].lower()
        folder = runs_root / run_id
        controller = control_root / item['id']
        if folder.exists() or controller.exists():
            raise ValueError('Prepared destination already exists; refusing to replace it: ' + str(folder))
        plans.append((item, source, run_id, folder, controller))
    defaults = empty_state()
    prompts = default_prompts()
    adapter = retrieval.MACOS_ADAPTER if sys.platform == 'darwin' else retrieval.WINDOWS_ADAPTER
    # Locate only; preparation must not execute Codex or copy credentials.
    discovery_codex = retrieval.codex_executable({'isolation': {
        'workspace': str(runs_root), 'adapter': adapter}})
    discovery_codex_sha = sha(Path(discovery_codex))
    result = []
    for item, source, run_id, folder, controller in plans:
        folder.mkdir(parents=True)
        controller.mkdir(parents=True)
        broker_policy = controller / 'broker-policy.json'
        write_json(broker_policy, {
            'schema_version': 1, 'provider': DISCOVERY_PROVIDER,
            'discovery': {'codex_path': discovery_codex,
                          'codex_sha256': discovery_codex_sha,
                          'auth_home': str(controller / 'discovery-auth'),
                          'state_root': str(controller / 'discovery-state'),
                          'model': defaults['authorModel'], 'timeout_seconds': 120},
            'max_results': 5,
            'max_candidates': 6, 'max_content_chars': 20000,
            'request_timeout_seconds': 15,
            'operation_timeout_seconds': 300,
            'rate_limit_directory': str(control_root / 'retrieval-state'),
            'solver_workspace': str(folder),
            'deny_url_prefixes': [
                'https://github.com/1stproof/batch-3',
                'https://raw.githubusercontent.com/1stproof/batch-3',
                'https://hexagonmath.org/2610.00244',
                'https://hexagonmath.org/pdf/2610.00244v1',
                'https://1stproof.org/third-batch-press-release.html',
            ],
        })
        policy_path = controller / 'retrieval-policy.json'
        audit_log = controller / 'historical-search.jsonl'
        cutoff = item['cutoffExclusiveUtc']
        write_json(policy_path, {
            'schemaVersion': 1, 'mode': 'historical', 'runId': run_id,
            'cutoffExclusiveUtc': cutoff,
            'broker': {
                'command': sys.executable,
                'args': [str(ROOT / 'retrieval' / 'historical_search.py'), '--cutoff', cutoff,
                         '--policy-file', str(broker_policy), '--policy-sha256', sha(broker_policy),
                         '--audit-log', str(audit_log)],
                'policyFile': str(broker_policy), 'auditLog': str(audit_log),
            },
            'isolation': {
                'status': 'requires-runtime-preflight',
                'adapter': adapter,
                'codexHome': str(controller / 'codex-home'), 'workspace': str(folder),
            },
            'metadata': {'problemId': item['problemId'], 'inputSha256': item['statementUtf8Sha256'],
                         'navigationVersion': NAVIGATION_VERSION,
                         'discoveryProvider': DISCOVERY_PROVIDER,
                         'userApprovedLaunch': manifest['confirmedUserChoices']['authorizesLaunch'], 'originalSource': item['originalInputSource']},
        })
        raw = source.read_bytes()
        statement = raw.decode('utf-8')
        (folder / 'input.md').write_bytes(raw)
        (folder / 'draft.md').write_text('# Draft problem\n\n' + statement, encoding='utf-8')
        # Both the original adapter and normal TCS direct input strip only
        # surrounding whitespace. Preserve the original bytes in input.md.
        (folder / 'checked-statement.md').write_text('# Statement sent directly to the proof author\n\n' + statement.strip(), encoding='utf-8')
        settings = {key: defaults[key] for key in SETTING_KEYS}
        settings.update(preparedRun=True, skipStatementReview=True, fileManagement=False,
                        speedMode='fast', sourceFile=item['id'] + '.md', launchHold=HOLD,
                        retrievalPolicyPath=str(policy_path), goalWorkspace=str(folder),
                        thinkingHours=manifest['executionOverrides']['thinkingHours'])
        write_json(folder / 'job-settings.json', settings)
        for role in ('review', 'author', 'critic', 'final'):
            prompt = prompts['author_simple' if role == 'author' else role]
            p = folder / 'prompts' / (role + '.txt')
            p.parent.mkdir(exist_ok=True)
            p.write_text(prompt, encoding='utf-8')
        result.append({'runId': run_id, 'id': item['id'], 'folder': str(folder),
                       'policy': str(policy_path), 'cutoffExclusiveUtc': cutoff,
                       'inputSha256': sha(folder / 'input.md'),
                       'settingsSha256': sha(folder / 'job-settings.json'),
                       'policySha256': sha(policy_path), 'phase': 'prepared',
                       'launchHold': HOLD, 'modelSessionStarted': False})
    output = {'schemaVersion': 2, 'branch': 'web-search-cut', 'baseCommit': manifest['baseTcsCommit'],
              'navigationVersion': NAVIGATION_VERSION,
              'discoveryProvider': DISCOVERY_PROVIDER,
              'manifest': manifest, 'manifestSha256': sha(EXPERIMENT / 'manifest.json'),
              'selectedProblemIds': selected_ids, 'runsRoot': str(runs_root), 'controlRoot': str(control_root),
              'status': 'PREPARED_HELD', 'launchAuthorized': manifest['confirmedUserChoices']['authorizesLaunch'], 'runs': result}
    write_json(control_root / 'prepared-runs.json', output, exclusive=True)
    return output

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs-root', type=Path)
    p.add_argument('--control-root', type=Path)
    p.add_argument('--problems', nargs='+', metavar='PXX',
                   help='Prepare only these unique problem IDs, in manifest order (default: all ten).')
    args = p.parse_args(argv)
    try:
        result = prepare(args.runs_root, args.control_root, args.problems)
    except (OSError, ValueError, retrieval.RetrievalPolicyError) as exc:
        p.exit(1, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
