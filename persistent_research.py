"""Existing critic/final archive adapter; goal sessions own their separate files."""
import hashlib
import json
from pathlib import Path

from research_journal import ResearchJournal

def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical(value):
    return ' '.join(str(value).casefold().split())


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ResearchMemory:
    """Adapter for the YAML memory action, backed by complete journal records."""
    def __init__(self, journal):
        self.journal = journal

    @property
    def data(self):
        return {'currentAttemptId': self.journal.get_state('currentAttemptId')}

    def record_candidate(self, solution, source, revision=0, critic_round=0,
                         status='awaiting_critic', persist=True):
        identity = 'p' + fingerprint(solution.strip())[:24]
        self.journal.commit('candidate', {
            'candidate_id': identity,
            'solution': solution, 'source': source, 'revision': revision,
            'critic_round': critic_round, 'status': status,
            'fingerprint': fingerprint(solution.strip())},
            {'candidate': solution, 'candidate_status': status, 'currentAttemptId': identity})
        return identity

    def mark_current(self, status):
        self.journal.commit('candidate_status', {
            'attempt_id': self.data['currentAttemptId'], 'status': status},
            {'candidate_status': status})

    def record_critic_report(self, report, critic_round, attempt_id=None,
                             result_attempt_id=None):
        issues = self.journal.get_state('open_issues', [])
        # An unchanged independently audited candidate can discharge some issues
        # even when other issues still require rejection. Repairs await re-audit.
        unchanged = not report['fixed'] and (not result_attempt_id or result_attempt_id == attempt_id)
        resolved = {item['id'] for item in report.get('resolved_obligations', [])
                    if item.get('evidence', '').strip()} if unchanged else set()
        issues = [item for item in issues if item['id'] not in resolved]
        for description in report.get('memory_update', {}).get('unresolved_obligations', []):
            identity = 'issue-' + fingerprint(canonical(description))[:16]
            if not any(item['id'] == identity for item in issues):
                issues.append({'id': identity, 'description': description,
                               'candidate_id': result_attempt_id or attempt_id})
        self.journal.commit('critic', {'attempt_id': attempt_id,
            'result_attempt_id': result_attempt_id, 'round': critic_round, 'report': report},
            {'open_issues': issues, 'feedback': report,
             'candidate_status': 'rejected' if report['verdict'] == 'reject' else
                                 ('awaiting_critic' if report['fixed'] else 'approved')})


def initialize(runtime, directory, statement, workflow, prompts, options):
    journal = ResearchJournal(directory, statement)
    specification = {'workflow': workflow, 'prompts': prompts,
                     'models': {k: v for k, v in options.items()
                                if k.endswith(('_model', '_effort')) or k in
                                ('model', 'effort', 'speed', 'summary', 'thinking_hours',
                                 'critic_rounds', 'elapsed_seconds')}}
    version = fingerprint(packed(specification))
    if journal.get_state('workflow_version') != version:
        journal.commit('workflow_version', specification, {'workflow_version': version})
    # Historical notebooks are evidence, never instructions. Import full contents
    # once; keeping only a digest here would repeat the old forgetting problem.
    names = ('author-memory.json', 'REGISTRY.md', 'FAILED.md', 'PROVED.md', 'LESSONS.md')
    paths = [Path(directory) / name for name in names]
    legacy = Path(directory) / 'continuation-memory'
    if legacy.is_dir():
        paths.extend(path for path in legacy.rglob('*') if path.name in names)
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_text(encoding='utf-8')
        key = 'import:' + fingerprint(path.name + '\n' + content)
        if not journal.get_state(key):
            original = journal.commit('legacy_import', {'file': str(path), 'content': content})
            for offset in range(0, len(content), 12000):
                journal.commit('legacy_excerpt', {'file': str(path), 'original_event': original,
                    'offset': offset, 'content': content[offset:offset + 12300],
                    'status': 'historical claims; independently verify before reuse'})
            journal.commit('legacy_import_complete', {'original_event': original}, {key: True})
    return journal
