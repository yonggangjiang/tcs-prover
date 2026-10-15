#!/usr/bin/env python3
"""Prepare ten held First Proof jobs. This command never starts model work."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from ui.server import empty_state, default_prompts

ROOT = Path(__file__).resolve().parent
EXPERIMENT = ROOT / 'experiments' / 'firstproof-batch3'
HOLD = 'Prepared for user launch on MacBook; compatible isolation and compiler adapters plus host preflight are required.'
SETTING_KEYS = (
    'reviewModel', 'authorModel', 'criticModel', 'writerModel', 'reasoningEffort',
    'reviewEffort', 'authorEffort', 'criticEffort', 'writerEffort', 'criticRounds',
    'thinkingHours', 'speedMode', 'reasoningSummary', 'researchAudits',
    'problemMode', 'skipStatementReview', 'statementReviewOnly', 'fileManagement',
    'preparedRun', 'sourceFile', 'goalThreadId', 'goalWorkspace', 'goalResume',
    'launchHold', 'retrievalPolicyPath',
)

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare(runs_root=None, control_root=None):
    runs_root = Path(runs_root or ROOT / 'runs').resolve()
    control_root = Path(control_root or ROOT / '.experiment-control' / 'firstproof-batch3').resolve()
    if control_root == runs_root or control_root.is_relative_to(runs_root):
        raise ValueError('Controller policies must be outside the solver runs directory.')
    manifest = json.loads((EXPERIMENT / 'manifest.json').read_text(encoding='utf-8'))
    expected_ids = [f'P{n:02}' for n in range(1, 11)]
    if [r['id'] for r in manifest['runs']] != expected_ids:
        raise ValueError('Expected exactly P01 through P10 in public problem order.')
    plans = []
    # Validate the entire batch and every target before writing any run.
    for item in manifest['runs']:
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
    result = []
    for item, source, run_id, folder, controller in plans:
        folder.mkdir(parents=True)
        controller.mkdir(parents=True)
        broker_policy = controller / 'broker-policy.json'
        write_json(broker_policy, {
            'schema_version': 1, 'provider': 'duckduckgo_wayback',
            'max_results': 5,
            'max_candidates': 10, 'max_content_chars': 20000,
            'request_timeout_seconds': 15,
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
                'args': [str(ROOT / 'historical_search.py'), '--cutoff', cutoff,
                         '--policy-file', str(broker_policy), '--policy-sha256', sha(broker_policy),
                         '--audit-log', str(audit_log)],
                'policyFile': str(broker_policy), 'auditLog': str(audit_log),
            },
            'isolation': {
                'status': 'requires-runtime-preflight',
                'adapter': 'codex-windows-mxc-v1',
                'codexHome': str(controller / 'codex-home'), 'workspace': str(folder),
            },
            'metadata': {'problemId': item['problemId'], 'inputSha256': item['statementUtf8Sha256'],
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
                       'inputSha256': sha(folder / 'input.md'), 'phase': 'prepared',
                       'launchHold': HOLD, 'modelSessionStarted': False})
    output = {'schemaVersion': 1, 'branch': 'web-search-cut', 'baseCommit': manifest['baseTcsCommit'],
              'status': 'PREPARED_HELD', 'launchAuthorized': manifest['confirmedUserChoices']['authorizesLaunch'], 'runs': result}
    write_json(control_root / 'prepared-runs.json', output)
    return output

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs-root', type=Path)
    p.add_argument('--control-root', type=Path)
    args = p.parse_args(argv)
    try:
        result = prepare(args.runs_root, args.control_root)
    except (OSError, ValueError) as exc:
        p.exit(1, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
