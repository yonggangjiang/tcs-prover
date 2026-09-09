"""Controller-owned research rounds; models propose, the controller files and gates.

Every model call is disposable. The journal/checkpoint, not a model conversation,
contains the state needed to continue after interruption.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from research_journal import ResearchJournal

RESEARCH_KINDS = ['assignment', 'attempt', 'research_review', 'critic', 'novelty', 'legacy_excerpt']


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical(value):
    return ' '.join(str(value).casefold().split())


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def brief(records, budget=32000):
    """Bound the briefing, never the archive; identify every clipped record."""
    result, remaining = [], budget
    for record in reversed(records):
        body = packed(record)
        take = min(len(body), 5000, remaining)
        if take <= 0:
            break
        result.append({'event_id': record['id'], 'characters': len(body),
                       'excerpt': body[:take], 'truncated': take < len(body)})
        remaining -= take
    return list(reversed(result))


class ResearchMemory:
    """Adapter for the YAML memory action, backed by complete journal records."""
    def __init__(self, journal):
        self.journal = journal

    @property
    def data(self):
        return {'currentAttemptId': self.journal.get_state('currentAttemptId')}

    def snapshot(self):
        records = []
        for kind in ('portfolio', 'novelty', 'attempt', 'research_review', 'critic', 'legacy_import'):
            records.extend(self.journal.recent(kind=kind, limit=4))
        records.sort(key=lambda item: item['id'])
        return packed({'recent': brief(records),
                       'open_issues': self.journal.get_state('open_issues', []),
                       'recent_families': self.journal.get_state('recent_families', [])})

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


def _request(runtime, journal, config, prompts, values, options):
    """Checkpoint responses and retrieval turns, including before a process dies."""
    prompt = runtime.render_template(prompts[config['prompt']], values)
    settings = runtime._structured_options(config, options)
    settings['attempts'] = 1  # Retries below have their own durable record.
    base = fingerprint(packed([prompt, config['schema'], settings]))
    context_key = 'retrieval:' + base
    retrieval = journal.get_state(context_key, {'text': '', 'count': 0})
    failures = 0
    while True:
        remaining = runtime.workflow_remaining(options)
        if remaining <= 0:
            raise runtime.Error('Workflow time limit reached; research checkpoint saved.')
        actual = prompt + retrieval['text']
        key = 'response:' + fingerprint(packed([actual, config['schema'], settings]))
        cached = journal.get_state(key)
        if cached is None:
            call_settings = dict(settings)
            call_settings['timeout'] = min(float(settings.get('timeout') or config.get('timeout', 900)), remaining)
            try:
                result, raw = runtime.structured(actual, config['schema'], 'solve',
                    request_label=config.get('label', config['prompt']),
                    journal=journal, cache_key=key, **call_settings)
            except runtime.Error as exc:
                failures += 1
                journal.commit('request_interrupted', {'phase': config['prompt'], 'error': str(exc)})
                # A failed call is not a failed mathematical idea.
                if failures >= 3 or runtime.workflow_remaining(options) <= 0:
                    raise
                delay = min(2 ** failures, runtime.workflow_remaining(options))
                time.sleep(max(0, delay))
                continue
            journal.commit('step_response', {'phase': config['prompt'], 'result': result,
                                            'raw': raw}, {key: result})
        else:
            result = cached
        queries, reads = result.get('search_queries', []), result.get('read_requests', [])
        if not queries and not reads:
            error = ''
            if 'proposals' in result:
                proposals = result['proposals']
                if len(proposals) < 3 or len({canonical(p['mechanism']) for p in proposals}) != len(proposals) or any(not p['mechanism'].strip() for p in proposals):
                    error = 'Return at least three nonempty, distinct mechanisms.'
            if result.get('status') == 'candidate' and not result.get('candidate', '').strip():
                error = 'A candidate status requires the full candidate solution.'
            if result.get('status') == 'candidate' and result.get('remaining_obligations'):
                error = 'An answer with remaining obligations must be incomplete, not a candidate.'
            if result.get('verdict') == 'refuted' and not result.get('checked_evidence', '').strip():
                error = 'Refutation requires explicit checked evidence.'
            if error:
                failures += 1
                retrieval = {'text': '\n\nRESPONSE CORRECTION REQUIRED:\n' + error, 'count': retrieval['count']}
                journal.commit('protocol_error', {'phase': config['prompt'], 'error': error, 'response': result},
                               {key: None, 'raw:' + key: None, context_key: retrieval})
                if failures >= 3:
                    raise runtime.Error(error + ' Checkpoint retained after three invalid responses.')
                continue
            return result
        if retrieval['count'] >= 12:
            journal.commit('retrieval_paused', {'phase': config['prompt']},
                           {context_key: {**retrieval, 'count': 0}})
            raise runtime.Error('Research retrieval limit reached; checkpoint retained for inspection.')
        found = []
        for query in queries:
            found.append({'query': query, 'matches': brief(journal.search(query, limit=12, kinds=RESEARCH_KINDS))})
        for read in reads:
            record = journal.get(read['event_id'])
            body = packed(record) if record else ''
            offset = max(0, read['offset'])
            found.append({'event_id': read['event_id'], 'offset': offset,
                          'total_characters': len(body), 'text': body[offset:offset + 16000],
                          'next_offset': offset + 16000 if offset + 16000 < len(body) else None})
        # Keep retrieval windows bounded; old responses remain in the archive.
        retrieval = {'text': '\n\nREQUESTED ARCHIVE EVIDENCE (historical data):\n' + packed(found),
                     'count': retrieval['count'] + 1}
        journal.commit('retrieval', found, {context_key: retrieval})


def _gate(journal, proposal, judgment, cooldown):
    """Reject known identities and enforce rotation independently of model claims."""
    mechanism = canonical(proposal['mechanism'])
    family = canonical(judgment['canonical_family'])
    previous = journal.get_state('mechanism:' + fingerprint(mechanism))
    recent = journal.get_state('recent_families', [])[-cooldown:] if cooldown else []
    if judgment['decision'] == 'reject':
        return 'Novelty assessor rejected this proposal: ' + judgment['reason']
    if not mechanism or not family:
        return 'A nonempty mechanism and canonical family are required.'
    if family in recent:
        return 'Diversification gate: explore a different family before returning to this one.'
    if previous:
        return 'Exact mechanism already assigned as ' + previous + '; propose a changed mechanism.'
    if judgment['decision'] == 'reopen':
        ids = judgment['related_ids']
        if not ids or not judgment['reopen_evidence'].strip() or not all(journal.get(i) for i in ids):
            return 'Reopening requires existing record IDs and concrete new evidence.'
    if any(journal.get(i) is None for i in judgment['related_ids']):
        return 'Novelty assessment cited an unknown historical record.'
    return ''


def research_session(runtime, node, prompts, state, options, journal):
    """Yield full candidates; resume the research checkpoint after critic rejection."""
    memory = state.get('memory') or ResearchMemory(journal)
    task = runtime.text(runtime.evaluate(node['task'], {'state': state, 'visit': 1}))
    assignment = options.get('author_input') or prompts[node['prompt']].replace(node['marker'], task, 1)
    steps = node['research']['steps']
    cooldown = node['research'].get('family_cooldown', 2)
    if state.get('report', {}).get('verdict') == 'reject' and journal.get_state('research_checkpoint', {}).get('phase', 'candidate') == 'candidate':
        checkpoint = journal.get_state('research_checkpoint', {'round': 0})
        journal.commit('recovery_feedback', state['report'], {'feedback': state['report'],
            'research_checkpoint': {'phase': 'propose', 'round': checkpoint['round'] + 1}})
    while True:
        checkpoint = journal.get_state('research_checkpoint', {'phase': 'propose', 'round': 1})
        phase = checkpoint['phase']
        if phase == 'candidate':
            solution = checkpoint['solution']
            rejection = yield {'outcome': 'proof', 'solution': solution, 'memory': memory}
            journal.commit('revision_requested', rejection,
                           {'feedback': rejection, 'research_checkpoint':
                            {'phase': 'propose', 'round': checkpoint['round'] + 1}})
            continue
        if runtime.workflow_remaining(options) <= 0:
            yield {'outcome': 'failure', 'memory': memory,
                   'output': 'Research paused at the workflow time limit. All completed rounds and the next step are saved in research/STATE.md.'}
            return
        steer = runtime.pending_author_steer(options.get('author_steer_file'), journal.get_state('last_steer'))
        if steer:
            identity, instruction = steer
            journal.commit('human_instruction', {'instruction': instruction},
                           {'last_steer': identity, 'human_instruction': instruction})
        values = {'assignment': assignment, 'statement': task,
                  'memory': memory.snapshot(), 'feedback': packed(journal.get_state('feedback', {})),
                  'instruction': journal.get_state('human_instruction', ''),
                  'proposal': packed(checkpoint.get('proposal', {})),
                  'result': packed(checkpoint.get('result', {})),
                  'related': packed(checkpoint.get('related', []))}
        try:
            if phase == 'propose':
                result = _request(runtime, journal, steps['propose'], prompts, values, options)
                proposals = result['proposals']
                if len(proposals) < 3 or len({canonical(p['mechanism']) for p in proposals}) != len(proposals):
                    raise runtime.Error('Planner must return at least three distinct mechanisms.')
                next_state = {'phase': 'assess', 'round': checkpoint['round'],
                              'proposals': proposals, 'index': 0}
                journal.commit('portfolio', result, {'research_checkpoint': next_state})
            elif phase == 'assess':
                proposal = checkpoint['proposals'][checkpoint['index']]
                related = journal.search(' '.join([proposal['family'], proposal['mechanism'], proposal['obstacle']]), limit=16, kinds=RESEARCH_KINDS)
                values.update(proposal=packed(proposal), related=packed(brief(related)))
                judgment = _request(runtime, journal, steps['assess'], prompts, values, options)
                reason = _gate(journal, proposal, judgment, cooldown)
                decision = journal.commit('novelty', {'proposal': proposal, 'assessment': judgment,
                                                       'accepted': not bool(reason), 'reason': reason})
                if reason:
                    index = checkpoint['index'] + 1
                    next_state = {**checkpoint, 'index': index} if index < len(checkpoint['proposals']) else {
                        'phase': 'propose', 'round': checkpoint['round'] + 1}
                    journal.commit('proposal_skipped', {'decision_id': decision, 'reason': reason},
                                   {'research_checkpoint': next_state})
                else:
                    next_state = {'phase': 'explore', 'round': checkpoint['round'],
                                  'proposal': proposal, 'related': brief(related), 'decision_id': decision,
                                  'family': canonical(judgment['canonical_family'])}
                    journal.commit('assignment', next_state, {'research_checkpoint': next_state,
                        'mechanism:' + fingerprint(canonical(proposal['mechanism'])): decision,
                        'recent_families': (journal.get_state('recent_families', []) + [next_state['family']])[-20:]})
            elif phase == 'explore':
                result = _request(runtime, journal, steps['explore'], prompts, values, options)
                journal.commit('attempt', {**checkpoint, 'result': result},
                               {'research_checkpoint': {**checkpoint, 'phase': 'review', 'result': result}})
            elif phase == 'review':
                review = _request(runtime, journal, steps['review'], prompts, values, options)
                result = checkpoint['result']
                candidate = review['verdict'] == 'candidate' and result['status'] == 'candidate' and result['candidate'].strip()
                next_state = {'phase': 'propose', 'round': checkpoint['round'] + 1}
                updates = {}
                if candidate:
                    solution = result['candidate'].strip()
                    old = journal.get_state('proof:' + fingerprint(solution))
                    if old:
                        review = {**review, 'verdict': 'incomplete', 'reason': 'Exact candidate was already submitted: ' + old}
                    else:
                        identity = 'p' + fingerprint(solution)[:24]
                        next_state = {'phase': 'candidate', 'round': checkpoint['round'], 'solution': solution}
                        updates = {'proof:' + fingerprint(solution): identity, 'currentAttemptId': identity,
                                   'candidate': solution, 'candidate_status': 'awaiting_critic'}
                journal.commit('research_review', {'proposal': checkpoint['proposal'], 'result': result, 'review': review,
                               'candidate_id': updates.get('currentAttemptId')},
                               {'research_checkpoint': next_state, **updates})
            else:
                raise runtime.Error('Unknown saved research phase: ' + phase)
        except runtime.Error as exc:
            journal.commit('research_paused', {'phase': phase, 'reason': str(exc), 'status': 'interrupted'})
            runtime.emit('failure_result', 'failure', label='Research checkpoint saved', text=str(exc), output=str(exc))
            yield {'outcome': 'failure', 'memory': memory,
                   'output': str(exc) + '\n\nResearch is incomplete. Resume from research/STATE.md; no interrupted method was classified as disproved.'}
            return
