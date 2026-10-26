"""Check content receipts and cutoff/ownership boundaries for document navigation."""
from copy import deepcopy
import hashlib
import json
import unittest

from retrieval import audit_historical_trajectory as auditor
from retrieval.historical_search import canonical_json, LIMITATION
try:
    from tests import test_historical_trajectory_audit as fixtures
except ImportError:
    import test_historical_trajectory_audit as fixtures

DOCUMENT, URL, CUTOFF = fixtures.DOCUMENT, fixtures.URL, fixtures.CUTOFF
capture_event, tool_event, tool_item, digest = (
    fixtures.capture_event, fixtures.tool_event, fixtures.tool_item, fixtures.digest)


class NavigationAuditTests(fixtures.TrajectoryAuditFixture):
    def navigation(self, tool='open', *, empty=False):
        doc = {**deepcopy(DOCUMENT), 'offset': 25000, 'total_chars': 25003,
               'document_text_sha256': 'c' * 64, 'offset_unit': 'unicode_code_points',
               'has_more': False, 'truncated': False, 'next_offset': None}
        args = {'url': URL, 'offset': 25000, 'sha256': doc['sha256']}
        if tool == 'open':
            doc.update(text='' if empty else 'ABC', end_offset=25003)
            if empty:
                doc['offset'] = args['offset'] = 25003
            doc['text_sha256'] = digest(doc['text'])
        else:
            del doc['text']
            args['pattern'] = doc['pattern'] = 'B'
            doc['matches'] = [] if empty else [{'offset': 25001, 'end_offset': 25002,
                'excerpt_offset': 25000, 'excerpt_end_offset': 25003,
                'text': 'ABC', 'text_sha256': digest('ABC')}]
        record = tool_event(tool, documents=[doc])
        tool_item(record)['arguments'] = args
        self.trajectory_records = [record]
        event = capture_event(doc, event=tool + '_completed')
        event['result_sha256'] = hashlib.sha256(canonical_json(doc)).hexdigest()
        self.broker_records = [event]
        return doc

    def replace_reply(self, doc):
        tool_item(self.trajectory_records[0])['result']['content'][0]['text'] = json.dumps({'cutoff': CUTOFF, 'result': doc})

    def test_pages_and_find_matches_after_twenty_thousand_have_content_receipts(self):
        for tool in ('open', 'find'):
            for empty in (False, True):
                with self.subTest(tool=tool, empty=empty):
                    self.navigation(tool, empty=empty)
                    report = self.audit()
                    self.assertTrue(report['passed'], report['issues'])
                    self.assertEqual(report['counts']['matched_documents'], 1)
                    self.assertEqual(report['sources'][0]['receipt_broker_lines'], [1])

    def test_changed_text_with_correct_excerpt_digest_cannot_borrow_receipt(self):
        for tool in ('open', 'find'):
            with self.subTest(tool=tool):
                doc = self.navigation(tool)
                target = doc if tool == 'open' else doc['matches'][0]
                target.update(text='XBC', text_sha256=digest('XBC'))
                self.replace_reply(doc)
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertIn('response_receipt_missing', [i['code'] for i in report['issues']])

    def test_navigation_cannot_fall_back_to_metadata_only_evidence(self):
        for tool in ('open', 'find'):
            self.navigation(tool)
            self.broker_records[0].pop('result_sha256')
            self.assertFalse(self.audit()['passed'])

    def test_removing_offset_cannot_disguise_navigation_as_a_legacy_open(self):
        doc = self.navigation()
        doc.pop('offset')
        tool_item(self.trajectory_records[0])['arguments'] = {'url': URL}
        self.broker_records[0].pop('result_sha256')
        self.replace_reply(doc)
        report = self.audit()
        self.assertFalse(report['passed'])
        codes = {issue['code'] for issue in report['issues']}
        self.assertIn('invalid_navigation_result', codes)
        self.assertIn('response_receipt_missing', codes)

    def test_find_continuation_must_leave_room_for_another_literal_match(self):
        doc = self.navigation('find')
        doc['matches'] = [{'offset': 25002, 'end_offset': 25003,
            'excerpt_offset': 25000, 'excerpt_end_offset': 25003,
            'text': 'ABB', 'text_sha256': digest('ABB')}]
        doc.update(has_more=True, truncated=True, next_offset=25003)
        self.replace_reply(doc)
        self.broker_records[0]['result_sha256'] = hashlib.sha256(canonical_json(doc)).hexdigest()
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertIn('invalid_navigation_result', [issue['code'] for issue in report['issues']])
        self.assertNotIn('response_receipt_missing', [issue['code'] for issue in report['issues']])

    def test_whole_document_digest_is_checked_when_all_text_is_returned(self):
        doc = self.navigation()
        doc.update(offset=0, total_chars=3, end_offset=3,
                   document_text_sha256=digest('ABC'))
        tool_item(self.trajectory_records[0])['arguments']['offset'] = 0
        self.replace_reply(doc)
        self.broker_records[0]['result_sha256'] = hashlib.sha256(canonical_json(doc)).hexdigest()
        self.assertTrue(self.audit()['passed'])
        doc['document_text_sha256'] = 'c' * 64
        self.replace_reply(doc)
        self.broker_records[0]['result_sha256'] = hashlib.sha256(canonical_json(doc)).hexdigest()
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertIn('invalid_navigation_result', [issue['code'] for issue in report['issues']])
        self.assertNotIn('response_receipt_missing', [issue['code'] for issue in report['issues']])

    def test_unreceipted_envelope_text_cannot_accompany_a_valid_document_receipt(self):
        for location in ('payload_extra', 'limitation', 'mcp_metadata', 'mcp_extra'):
            with self.subTest(location=location):
                self.navigation()
                result = tool_item(self.trajectory_records[0])['result']
                if location in ('payload_extra', 'limitation'):
                    payload = json.loads(result['content'][0]['text'])
                    payload['extra' if location == 'payload_extra' else 'limitation'] = 'UNVERIFIED_SOURCE_TEXT'
                    result['content'][0]['text'] = json.dumps(payload)
                else:
                    result['_meta' if location == 'mcp_metadata' else 'extra'] = {'text': 'UNVERIFIED_SOURCE_TEXT'}
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertNotIn('response_receipt_missing', [issue['code'] for issue in report['issues']])

    def test_known_broker_limitation_is_allowed_outside_the_document_receipt(self):
        self.navigation()
        result = tool_item(self.trajectory_records[0])['result']
        payload = json.loads(result['content'][0]['text'])
        payload['limitation'] = LIMITATION
        result['content'][0]['text'] = json.dumps(payload)
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])

    def test_find_without_matches_still_enforces_capture_cutoff(self):
        doc = self.navigation('find', empty=True)
        doc.update(capture_time=CUTOFF, archive_url='https://web.archive.org/web/20261002141312id_/' + URL)
        self.replace_reply(doc)
        self.broker_records[0] = {**capture_event(doc, event='find_completed'),
            'result_sha256': hashlib.sha256(canonical_json(doc)).hexdigest()}
        report = self.audit()
        self.assertIn('source_after_cutoff', [i['code'] for i in report['issues']])

    def test_invalid_ranges_and_wrong_requested_hash_fail_even_with_receipt(self):
        for field, value in [('offset', True), ('end_offset', 999999), ('text_sha256', 'e' * 64),
                             ('next_offset', 25003), ('offset_unit', 'bytes')]:
            with self.subTest(field=field):
                doc = self.navigation()
                doc[field] = value
                self.replace_reply(doc)
                self.broker_records[0]['result_sha256'] = hashlib.sha256(canonical_json(doc)).hexdigest()
                self.assertFalse(self.audit()['passed'])
        self.navigation()
        tool_item(self.trajectory_records[0])['arguments']['sha256'] = 'd' * 64
        self.assertFalse(self.audit()['passed'])

    def test_unrecognized_fields_and_boolean_offsets_are_not_valid_navigation(self):
        for extra in ({'offset': True}, {'cutoff': CUTOFF}, {'length': 20001}, {'sha256': 'invalid'}):
            self.navigation()
            tool_item(self.trajectory_records[0])['arguments'].update(extra)
            self.assertFalse(self.audit()['passed'])


class FullDocumentAuditTests(fixtures.TrajectoryAuditFixture):
    def record(self, tool, doc, args):
        self.trajectory_records = [tool_event(tool, documents=[doc])]
        tool_item(self.trajectory_records[0])['arguments'] = args
        self.broker_records = [{**capture_event(doc, event=tool + '_completed'),
            'result_sha256': hashlib.sha256(canonical_json(doc)).hexdigest()}]

    def open_reply(self, content, offset=0, length=None):
        end = len(content) if length is None else min(offset + length, len(content))
        page = content[offset:end]
        doc = {**deepcopy(DOCUMENT), 'text': page, 'offset': offset, 'end_offset': end,
            'total_chars': len(content), 'document_text_sha256': digest(content),
            'text_sha256': digest(page), 'offset_unit': 'unicode_code_points',
            'has_more': end < len(content), 'truncated': end < len(content),
            'next_offset': end if end < len(content) else None}
        args = {'url': URL, 'offset': offset}
        if length is not None:
            args['length'] = length
        self.record('open', doc, args)
        return doc, args

    def find_reply(self, content, pattern, positions, maximum=None):
        matches = []
        for position in positions:
            size = max(400, len(pattern))
            start = max(0, position - min(120, size - len(pattern)))
            text = content[start:start + size]
            matches.append({'offset': position, 'end_offset': position + len(pattern),
                'excerpt_offset': start, 'excerpt_end_offset': start + len(text),
                'text': text, 'text_sha256': digest(text)})
        doc = {**deepcopy(DOCUMENT), 'offset': 0, 'total_chars': len(content),
            'document_text_sha256': digest(content), 'offset_unit': 'unicode_code_points',
            'has_more': False, 'truncated': False, 'next_offset': None,
            'pattern': pattern, 'matches': matches}
        del doc['text']
        args = {'url': URL, 'pattern': pattern}
        if maximum is not None:
            args['max_matches'] = maximum
        self.record('find', doc, args)
        return doc, args

    def test_default_open_reads_full_remaining_unicode_document_above_legacy_budget(self):
        content = ('A\U0001f600\u03bb' * 20000) + 'END'
        for offset in (0, 25001, len(content)):
            with self.subTest(offset=offset):
                self.open_reply(content, offset=offset)
                report = self.audit(full_document_default=True)
                self.assertTrue(report['passed'], report['issues'])
                self.assertEqual(report['counts']['matched_documents'], 1)
        self.open_reply(content)
        self.assertFalse(self.audit()['passed'])

    def test_legacy_default_page_stays_valid_only_under_legacy_rules(self):
        doc, args = self.open_reply('A' * 40001, length=20000)
        args.pop('length')
        self.record('open', doc, args)
        self.assertTrue(self.audit()['passed'])
        self.assertFalse(self.audit(full_document_default=True)['passed'])
        args['length'] = 20000
        self.record('open', doc, args)
        self.assertTrue(self.audit(full_document_default=True)['passed'])

    def test_explicit_open_length_can_exceed_the_legacy_policy_limit(self):
        for length in (60000, 2 ** 53 - 1):
            with self.subTest(length=length):
                self.open_reply('A' * 80000, length=length)
                self.assertTrue(self.audit(full_document_default=True)['passed'])
                self.assertFalse(self.audit()['passed'])

    def test_explicit_lengths_and_match_counts_remain_positive_safe_integers(self):
        for value in (0, -1, True, 1.5, None, '10', 2 ** 53):
            for tool, field in (('open', 'length'), ('find', 'max_matches')):
                with self.subTest(tool=tool, value=value):
                    self.open_reply('ABC')
                    item = tool_item(self.trajectory_records[0])
                    item['tool'] = tool
                    item['arguments'][field] = value
                    if tool == 'find':
                        item['arguments']['pattern'] = 'A'
                    report = self.audit(full_document_default=True)
                    self.assertFalse(report['passed'])
                    self.assertIn('malformed_tool_call', [issue['code'] for issue in report['issues']])

    def test_find_can_return_more_than_twenty_matches_and_twenty_thousand_context_characters(self):
        content = ('MARK' + 'x' * 496) * 60
        self.find_reply(content, 'MARK', list(range(0, len(content), 500)), maximum=60)
        report = self.audit(full_document_default=True)
        self.assertTrue(report['passed'], report['issues'])
        self.assertFalse(self.audit()['passed'])

    def test_find_pattern_and_context_are_independent_of_legacy_policy_budget(self):
        pattern = 'P' * 500
        self.find_reply('x' * 1000 + pattern + 'x' * 1000, pattern, [1000], maximum=2 ** 53 - 1)
        report = self.audit(full_document_default=True, content_limit=100)
        self.assertTrue(report['passed'], report['issues'])
        self.assertFalse(self.audit(content_limit=100)['passed'])

    def test_find_context_ranges_remain_validated_even_with_matching_receipt(self):
        doc, args = self.find_reply('x' * 1000 + 'MARK' + 'x' * 1000, 'MARK', [1000])
        self.assertTrue(self.audit(full_document_default=True)['passed'])
        match = doc['matches'][0]
        match.update(excerpt_offset=1000, excerpt_end_offset=1004,
                     text='MARK', text_sha256=digest('MARK'))
        self.record('find', doc, args)
        report = self.audit(full_document_default=True)
        self.assertFalse(report['passed'])
        self.assertIn('invalid_navigation_result', [issue['code'] for issue in report['issues']])

    def test_find_can_continue_only_after_returning_the_requested_match_count(self):
        content = 'x' * 1000 + 'MARK' + 'x' * 1000 + 'MARK' + 'x' * 1000
        doc, args = self.find_reply(content, 'MARK', [1000], maximum=1)
        doc.update(has_more=True, truncated=True, next_offset=1001)
        self.record('find', doc, args)
        self.assertTrue(self.audit(full_document_default=True)['passed'])
        args['max_matches'] = 2
        self.record('find', doc, args)
        report = self.audit(full_document_default=True)
        self.assertFalse(report['passed'])
        self.assertIn('invalid_navigation_result', [issue['code'] for issue in report['issues']])


class NativeAbbreviationAuditTests(fixtures.TrajectoryAuditFixture):
    def abbreviated_reply(self):
        prefix, suffix = 'Original prefix ', ' original suffix'
        omitted = ('Greek \u03bb and emoji \U0001f600 with \\frac{"x"}{2}\n' * 80)
        text = prefix + omitted + suffix
        doc = {**DOCUMENT, 'text': text, 'offset': 0, 'end_offset': len(text),
            'total_chars': len(text), 'document_text_sha256': digest(text),
            'text_sha256': digest(text), 'offset_unit': 'unicode_code_points',
            'has_more': False, 'truncated': False, 'next_offset': None}
        complete = tool_event('open', documents=[doc])
        complete['event']['params']['item']['result']['content'][0]['text'] = json.dumps(
            {'cutoff': CUTOFF, 'result': doc}, ensure_ascii=False)
        self.trajectory_records = [complete]
        self.broker_records = [{**capture_event(doc, event='open_completed'),
            'result_sha256': hashlib.sha256(canonical_json(doc)).hexdigest()}]
        escaped = omitted
        for _ in range(2):
            escaped = json.dumps(escaped, ensure_ascii=False)[1:-1]
        short = {**doc, 'text': prefix + f'…{len(escaped)} chars truncated…' + suffix}
        native = fixtures.native_tool_event(complete)
        self.wrap(native, {'cutoff': CUTOFF, 'result': short})
        return native, short

    @staticmethod
    def wrap(native, payload):
        nested = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}], 'isError': False}
        native['payload']['item']['result'] = {'isError': False, 'content': [
            {'type': 'text', 'text': json.dumps(nested, ensure_ascii=False, separators=(',', ':'))}]}

    def test_native_abbreviation_matches_only_full_receipted_transcript_and_warns(self):
        native, _ = self.abbreviated_reply()
        report = self.audit(native_records=[native], full_document_default=True)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['completed_calls'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)
        self.assertEqual([warning['code'] for warning in report['warnings']], ['native_reply_abbreviated'])

    def test_native_abbreviation_without_a_complete_transcript_is_not_a_source(self):
        native, _ = self.abbreviated_reply()
        self.trajectory_records = []
        report = self.audit(native_records=[native], full_document_default=True)
        self.assertFalse(report['passed'])
        self.assertEqual(report['counts']['matched_documents'], 0)
        self.assertNotIn('native_reply_abbreviated', [warning['code'] for warning in report['warnings']])

    def test_unreceipted_full_transcript_cannot_authenticate_native_abbreviation(self):
        native, _ = self.abbreviated_reply()
        self.broker_records[0].pop('result_sha256')
        report = self.audit(native_records=[native], full_document_default=True)
        codes = {issue['code'] for issue in report['issues']}
        self.assertIn('response_receipt_missing', codes)
        self.assertIn('conflicting_tool_completion', codes)
        self.assertFalse(report['warnings'])

    def test_wrong_count_prefix_suffix_or_metadata_remain_conflicts(self):
        for mutation in ('count', 'prefix', 'suffix', 'metadata', 'extra_payload', 'arguments'):
            with self.subTest(mutation=mutation):
                native, short = self.abbreviated_reply()
                payload = {'cutoff': CUTOFF, 'result': short}
                if mutation == 'count':
                    short['text'] = short['text'].replace(' chars truncated', '0 chars truncated')
                elif mutation == 'prefix':
                    short['text'] = 'X' + short['text'][1:]
                elif mutation == 'suffix':
                    short['text'] += 'X'
                elif mutation == 'metadata':
                    short['title'] += ' changed'
                elif mutation == 'extra_payload':
                    payload['unverified'] = 'extra source text'
                else:
                    native['payload']['item']['arguments']['url'] = 'https://example.org/other'
                self.wrap(native, payload)
                report = self.audit(native_records=[native], full_document_default=True)
                self.assertFalse(report['passed'])
                self.assertIn('conflicting_tool_completion', [issue['code'] for issue in report['issues']])
                self.assertFalse(report['warnings'])

    def test_abbreviation_may_split_either_json_escape_layer(self):
        for layer in ('outer', 'inner'):
            with self.subTest(layer=layer):
                native, _ = self.abbreviated_reply()
                complete = self.trajectory_records[0]['event']['params']['item']
                raw = json.dumps({'content': complete['result']['content'], 'isError': False},
                                 ensure_ascii=False, separators=(',', ':'))
                # A document backslash becomes four backslashes in this wrapper.
                escape_start = raw.index('\\' * 4, raw.index('Original prefix'))
                start = escape_start + (1 if layer == 'outer' else 2)
                end = raw.index(' original suffix')
                native['payload']['item']['result']['content'][0]['text'] = (
                    raw[:start] + f'…{end - start} chars truncated…' + raw[end:])
                report = self.audit(native_records=[native], full_document_default=True)
                self.assertTrue(report['passed'], report['issues'])
                self.assertEqual(report['warnings'][0]['code'], 'native_reply_abbreviated')

    def test_abbreviation_cannot_hide_metadata_or_remove_document_delimiters(self):
        for region in ('metadata', 'delimiter'):
            with self.subTest(region=region):
                native, _ = self.abbreviated_reply()
                complete = self.trajectory_records[0]['event']['params']['item']
                raw = json.dumps({'content': complete['result']['content'], 'isError': False},
                                 ensure_ascii=False, separators=(',', ':'))
                start = raw.index(CUTOFF) if region == 'metadata' else raw.index('Original prefix') - 1
                end = raw.index(' original suffix')
                native['payload']['item']['result']['content'][0]['text'] = (
                    raw[:start] + f'…{end - start} chars truncated…' + raw[end:])
                report = self.audit(native_records=[native], full_document_default=True)
                self.assertFalse(report['passed'])
                self.assertIn('conflicting_tool_completion', [issue['code'] for issue in report['issues']])
                self.assertFalse(report['warnings'])

    def test_unapproved_single_wrapper_and_conflicting_full_reply_remain_conflicts(self):
        for variant in ('single_wrapper', 'full_reply'):
            with self.subTest(variant=variant):
                native, short = self.abbreviated_reply()
                if variant == 'single_wrapper':
                    native['payload']['item']['result']['content'][0]['text'] = json.dumps(
                        {'cutoff': CUTOFF, 'result': short}, ensure_ascii=False)
                else:
                    complete = self.trajectory_records[0]['event']['params']['item']
                    payload = json.loads(complete['result']['content'][0]['text'])
                    payload['result']['text'] += ' different full reply'
                    self.wrap(native, payload)
                report = self.audit(native_records=[native], full_document_default=True)
                self.assertFalse(report['passed'])
                self.assertIn('conflicting_tool_completion', [issue['code'] for issue in report['issues']])
                self.assertFalse(report['warnings'])

    def test_extra_native_content_or_structured_content_is_not_ignored(self):
        for mutation in ('extra_block', 'structured', 'metadata'):
            with self.subTest(mutation=mutation):
                native, _ = self.abbreviated_reply()
                result = native['payload']['item']['result']
                if mutation == 'extra_block':
                    result['content'].append({'type': 'text', 'text': 'additional source'})
                elif mutation == 'structured':
                    result['structuredContent'] = {'text': 'additional source'}
                else:
                    result['_meta'] = {'text': 'additional source'}
                report = self.audit(native_records=[native], full_document_default=True)
                self.assertFalse(report['passed'])
                self.assertIn('conflicting_tool_completion', [issue['code'] for issue in report['issues']])


class SubsetAuditTests(fixtures.BatchAuditFixture):
    def select_subset(self):
        self.prepared.update(schemaVersion=2, selectedProblemIds=['P09', 'P10'],
            runsRoot=str(self.root / 'runs'), controlRoot=str(self.control), manifest=self.manifest,
            manifestSha256=hashlib.sha256(self.manifest_path.read_bytes()).hexdigest())
        self.prepared['runs'] = self.prepared['runs'][-2:]
        self.write_json(self.control / 'prepared-runs.json', self.prepared)

    def test_recorded_subset_binds_full_manifest_and_only_selected_runs(self):
        self.select_subset()
        report = self.batch()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['selected_problem_ids'], ['P09', 'P10'])
        self.assertEqual(report['counts']['matched_documents'], 2)

    def test_subset_cannot_silently_drop_a_requested_job(self):
        self.select_subset()
        with self.assertRaises(ValueError):
            auditor.audit_batch(self.control, self.manifest_path, problems=['P09'])
        self.prepared['runs'].pop()
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        with self.assertRaises(ValueError):
            self.batch()

    def test_subset_must_bind_the_actual_runs_and_controller_roots(self):
        self.select_subset()
        original = deepcopy(self.prepared)
        for key, value in [('runsRoot', str(self.root / 'other-runs')),
                           ('controlRoot', str(self.root / 'other-controls')),
                           ('schemaVersion', 1)]:
            with self.subTest(key=key):
                self.prepared = {**deepcopy(original), key: value}
                self.write_json(self.control / 'prepared-runs.json', self.prepared)
                with self.assertRaises(ValueError):
                    self.batch()

    def test_schema_two_open_cannot_strip_all_navigation_fields_to_use_legacy_evidence(self):
        self.select_subset()
        fixture = self.fixtures['P09']
        # This is valid legacy evidence, with every navigation field removed.
        # A schema-two batch must still require the new document interface.
        self.write_records(fixture['folder'] / 'transcript.jsonl',
                           [tool_event('open', thread=fixture['thread'])])
        for include_receipt in (False, True):
            with self.subTest(receipt=include_receipt):
                event = capture_event(event='open_completed')
                if include_receipt:
                    event['result_sha256'] = hashlib.sha256(canonical_json(DOCUMENT)).hexdigest()
                self.write_records(fixture['log_path'], [event])
                report = self.batch()
                self.assertFalse(report['passed'])
                self.assertEqual([row['problem_id'] for row in report['runs']], ['P09', 'P10'])
                self.assertFalse(report['runs'][0]['passed'])
                self.assertTrue(report['runs'][1]['passed'])
                codes = {issue['code'] for issue in report['runs'][0]['issues']}
                self.assertIn('invalid_navigation_result', codes)
                if not include_receipt:
                    self.assertIn('response_receipt_missing', codes)

    def test_full_document_rules_are_bound_to_preparation_and_each_policy(self):
        self.select_subset()
        self.prepared['navigationVersion'] = 'full-document-v1'
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        for problem in ('P09', 'P10'):
            fixture = self.fixtures[problem]
            fixture['policy']['metadata']['navigationVersion'] = 'full-document-v1'
            self.write_json(fixture['policy_path'], fixture['policy'])
        fixture = self.fixtures['P09']
        text = 'A' * 30001
        doc = {**DOCUMENT, 'text': text, 'offset': 0, 'end_offset': len(text),
            'total_chars': len(text), 'text_sha256': digest(text),
            'document_text_sha256': digest(text), 'offset_unit': 'unicode_code_points',
            'has_more': False, 'truncated': False, 'next_offset': None}
        self.write_records(fixture['folder'] / 'transcript.jsonl',
                           [tool_event('open', documents=[doc], thread=fixture['thread'])])
        self.write_records(fixture['log_path'], [{**capture_event(doc, event='open_completed'),
            'result_sha256': hashlib.sha256(canonical_json(doc)).hexdigest()}])
        report = self.batch()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['navigation_version'], 'full-document-v1')
        fixture['policy']['metadata'].pop('navigationVersion')
        self.write_json(fixture['policy_path'], fixture['policy'])
        self.assertFalse(self.batch()['passed'])

    def test_navigation_marker_cannot_be_dropped_or_changed_in_preparation(self):
        self.select_subset()
        fixture = self.fixtures['P09']
        fixture['policy']['metadata']['navigationVersion'] = 'full-document-v1'
        self.write_json(fixture['policy_path'], fixture['policy'])
        self.assertFalse(self.batch()['passed'])
        self.prepared['navigationVersion'] = 'unrecognized-version'
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        with self.assertRaisesRegex(ValueError, 'version is unknown'):
            self.batch()

    def configure_codex_discovery(self):
        self.select_subset()
        self.prepared['discoveryProvider'] = 'codex_wayback'
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        for problem in ('P09', 'P10'):
            fixture = self.fixtures[problem]
            fixture['broker_policy'].update(provider='codex_wayback', discovery={
                'auth_home': str(fixture['controller'] / 'discovery-auth'),
                'state_root': str(fixture['controller'] / 'discovery-state'),
                'model': 'gpt-6.1-sol'})
            fixture['policy']['metadata']['discoveryProvider'] = 'codex_wayback'
            self.save_discovery_policy(fixture)
            settings_path = fixture['folder'] / 'job-settings.json'
            settings = json.loads(settings_path.read_text())
            settings['authorModel'] = 'gpt-6.1-sol'
            self.write_json(settings_path, settings)

    def save_discovery_policy(self, fixture):
        self.write_json(fixture['broker_path'], fixture['broker_policy'])
        args = fixture['policy']['broker']['args']
        args[args.index('--policy-sha256') + 1] = hashlib.sha256(fixture['broker_path'].read_bytes()).hexdigest()
        self.write_json(fixture['policy_path'], fixture['policy'])

    def test_codex_discovery_provider_is_bound_without_changing_source_receipts(self):
        self.configure_codex_discovery()
        report = self.batch()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['discovery_provider'], 'codex_wayback')
        self.assertEqual(report['counts']['matched_documents'], 2)
        self.assertEqual(report['runs'][0]['binding']['discovery_provider'], 'codex_wayback')

    def test_discovery_provider_cannot_be_changed_or_dropped_from_binding(self):
        self.configure_codex_discovery()
        fixture = self.fixtures['P09']
        for provider in ('scholarly_wayback', None):
            with self.subTest(provider=provider):
                if provider is None:
                    fixture['policy']['metadata'].pop('discoveryProvider')
                else:
                    fixture['policy']['metadata']['discoveryProvider'] = provider
                self.save_discovery_policy(fixture)
                report = self.batch()
                self.assertFalse(report['passed'])
                self.assertEqual(report['runs'][0]['issues'][0]['code'], 'run_binding_failed')
        fixture['policy']['metadata']['discoveryProvider'] = 'codex_wayback'
        self.save_discovery_policy(fixture)
        self.prepared.pop('discoveryProvider')
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        self.assertFalse(self.batch()['passed'])

    def test_discovery_cannot_borrow_another_problems_protected_home_or_state(self):
        self.configure_codex_discovery()
        fixture, other = self.fixtures['P09'], self.fixtures['P10']
        original = deepcopy(fixture['broker_policy']['discovery'])
        for key in ('auth_home', 'state_root'):
            with self.subTest(key=key):
                fixture['broker_policy']['discovery'] = {**original,
                    key: other['broker_policy']['discovery'][key]}
                self.save_discovery_policy(fixture)
                report = self.batch()
                self.assertFalse(report['runs'][0]['passed'])
                self.assertTrue(report['runs'][1]['passed'])
                self.assertIn('another problem controller', report['runs'][0]['issues'][0]['detail'])
        fixture['broker_policy']['discovery'] = {**original, 'model': 'different-model'}
        self.save_discovery_policy(fixture)
        self.assertFalse(self.batch()['runs'][0]['passed'])


if __name__ == '__main__':
    unittest.main()
