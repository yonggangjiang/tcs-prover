#!/usr/bin/env python3
"""Reconcile recorded historical MCP replies with per-run broker evidence.

This is a read-only, offline checkpoint audit, not archive revalidation. Only
newline-terminated transcript records and actual completed MCP replies establish
returned documents; candidate verification events and prose citations do not.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

from historical_search import canonical_json, canonical_url, parse_cutoff, HistoricalSearchError, LIMITATION


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / 'experiments' / 'firstproof-batch3' / 'manifest.json'
FULL_DOCUMENT_NAVIGATION = 'full-document-v1'
CODEX_DISCOVERY_PROVIDER = 'codex_wayback'
MAX_SAFE_INTEGER = 2 ** 53 - 1
LIMITATIONS = [
    'This audit reconciles recorded URL, cutoff, capture time and raw-body digest metadata. It does not refetch or independently revalidate archive bytes, CDX or Memento headers.',
    'Raw-body SHA256 identifies archive bytes. Versioned open/find documents additionally match a broker receipt covering their complete returned content; older replies and search excerpts have metadata-only evidence.',
    'Broker capture_verified events include candidates never returned. Only actual completed MCP reply documents are counted as returned source occurrences.',
    'Matching receipts establish content presence in the selected broker log, not unique request attribution or cryptographic log authenticity. Older evidence has no response receipts.',
    'This checkpoint covers complete recorded lines only; pending calls, partial trailing lines and later activity are not completed evidence.',
    'Pending means a recorded start lacks a matching terminal event, not that the call remains active. Code-cell termination has no reliable inner-MCP call-ID mapping and is reported separately.',
    'The audit checks recorded native web-search and MCP execution events, not every possible shell network action, hidden event or model training knowledge. Runtime isolation must be verified separately.',
    'Failed MCP calls are counted separately; error text is not authenticated historical content. A recorded MCP reply can be transformed or suppressed by code-mode output before model display.',
    'A native rollout may abbreviate a completed reply. Such a duplicate is only reconciled against a full receipt-matched transcript reply; the warning does not establish what text the model received.',
    'Current discovery and archive coverage do not reproduce the historical search index. Mathematical correctness and prose citation support are not assessed.',
]
COUNT_KEYS = ('completed_calls', 'successful_calls', 'error_calls', 'cancelled_calls',
              'pending_calls', 'returned_documents', 'matched_documents',
              'native_web_calls', 'unexpected_mcp_calls', 'incomplete_responses')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _issue(issues, code, detail, line=None, source=None):
    value = {'code': code, 'detail': detail}
    if line is not None:
        value['line'] = line
    if source is not None:
        value['source'] = source
    issues.append(value)


def _strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('Duplicate JSON object key: ' + key)
            result[key] = item
        return result

    def constant(_value):
        raise ValueError('Non-finite JSON number')

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def _read_jsonl(path, issues, source):
    """Snapshot a bounded prefix; never accept an unterminated final line."""
    path = Path(path)
    try:
        with path.open('rb') as stream:
            import os
            size = os.fstat(stream.fileno()).st_size
            raw = stream.read(size)
    except OSError as exc:
        _issue(issues, 'unreadable_evidence', str(exc), source=source)
        return [], {'path': str(path), 'complete_lines': 0, 'bytes_read': 0,
                    'complete_bytes': 0, 'trailing_bytes': 0, 'sha256': None}
    end = raw.rfind(b'\n') + 1
    complete = raw[:end]
    checkpoint = {'path': str(path), 'complete_lines': complete.count(b'\n'),
                  'bytes_read': len(raw), 'complete_bytes': end,
                  'trailing_bytes': len(raw) - end, 'sha256': _sha(complete),
                  'hash_scope': 'complete_newline_terminated_prefix'}
    if len(raw) != size:
        _issue(issues, 'evidence_truncated_during_read', 'File shortened while its checkpoint was read.', source=source)
    records = []
    for number, line in enumerate(complete.splitlines(), 1):
        try:
            value = _strict_json(line.decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError('JSONL record is not an object')
        except (UnicodeError, ValueError) as exc:
            _issue(issues, 'malformed_jsonl', str(exc), number, source)
            continue
        records.append((number, value))
    return records, checkpoint


def _timestamp(value):
    return parse_cutoff(value)


def _hash_string(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _same_time(value, expected):
    try:
        return _timestamp(value) == expected
    except (HistoricalSearchError, ValueError, TypeError):
        return False


def _arguments_valid(tool, arguments, limit, full_document_default=False):
    if not isinstance(arguments, dict):
        return False
    if tool == 'search':
        return set(arguments) == {'query'} and isinstance(arguments['query'], str)
    required = {'url', 'pattern'} if tool == 'find' else {'url'}
    optional = {'offset', 'sha256', 'max_matches' if tool == 'find' else 'length'}
    if not required <= arguments.keys() or set(arguments) - required - optional:
        return False
    if not isinstance(arguments['url'], str):
        return False
    offset = arguments.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= MAX_SAFE_INTEGER:
        return False
    if 'sha256' in arguments and not _hash_string(arguments['sha256']):
        return False
    if tool == 'open':
        if full_document_default and 'length' not in arguments:
            return True
        length = arguments.get('length', limit)
        return type(length) is int and 1 <= length <= (MAX_SAFE_INTEGER if full_document_default else limit)
    pattern, count = arguments['pattern'], arguments.get('max_matches', 10)
    return (isinstance(pattern, str) and 1 <= len(pattern) <= (500 if full_document_default else min(500, limit))
            and not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', pattern)
            and type(count) is int and 1 <= count <= (MAX_SAFE_INTEGER if full_document_default else 20))


def _check_navigation(tool, arguments, doc, limit, full_document_default=False):
    """Independently check returned ranges/digests; receipts also bind the text."""
    offset, total = doc.get('offset'), doc.get('total_chars')
    if (type(offset) is not int or type(total) is not int or not 0 <= offset <= total
            or offset != arguments.get('offset', 0)
            or not _hash_string(doc.get('document_text_sha256'))
            or doc.get('offset_unit') != 'unicode_code_points'):
        raise ValueError('Invalid document navigation metadata.')
    more, next_offset = doc.get('has_more'), doc.get('next_offset')
    if type(more) is not bool or doc.get('truncated') is not more:
        raise ValueError('Invalid navigation continuation flags.')
    if 'sha256' in arguments and arguments['sha256'] != doc['sha256']:
        raise ValueError('Returned document differs from the requested body digest.')
    if tool == 'open':
        text, end = doc.get('text'), doc.get('end_offset')
        requested_length = arguments.get('length', total - offset if full_document_default else limit)
        if (not isinstance(text, str) or type(end) is not int
                or end != offset + len(text) or end > total
                or len(text) != min(requested_length, total - offset)
                or doc.get('text_sha256') != _sha(text.encode('utf-8'))
                or more != (end < total)
                or ((type(next_offset) is not int or next_offset != end) if more else next_offset is not None)):
            raise ValueError('Invalid returned page length, digest or continuation.')
        if offset == 0 and end == total and doc['document_text_sha256'] != doc['text_sha256']:
            raise ValueError('Whole-document text digest differs from the complete page digest.')
    else:
        pattern, matches = arguments['pattern'], doc.get('matches')
        if doc.get('pattern') != pattern or not isinstance(matches, list) or len(matches) > arguments.get('max_matches', 10):
            raise ValueError('Invalid literal find results.')
        previous, budget = offset - 1, 0
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError('Find match is not an object.')
            start, end = match.get('offset'), match.get('end_offset')
            excerpt_start, excerpt_end = match.get('excerpt_offset'), match.get('excerpt_end_offset')
            text = match.get('text')
            if (any(type(n) is not int for n in (start, end, excerpt_start, excerpt_end))
                    or not offset <= start < end <= total or start <= previous
                    or end != start + len(pattern) or not 0 <= excerpt_start <= start
                    or not end <= excerpt_end <= total or not isinstance(text, str)
                    or len(text) != excerpt_end - excerpt_start
                    or text[start - excerpt_start:end - excerpt_start] != pattern
                    or match.get('text_sha256') != _sha(text.encode('utf-8'))):
                raise ValueError('Invalid find match range, literal text or digest.')
            if full_document_default:
                context_size = max(400, len(pattern))
                expected_start = max(0, start - min(120, context_size - len(pattern)))
                if excerpt_start != expected_start or excerpt_end != min(total, expected_start + context_size):
                    raise ValueError('Invalid find context range.')
            previous, budget = start, budget + len(text)
        if ((not full_document_default and budget > limit)
                or (full_document_default and more and len(matches) != arguments.get('max_matches', 10))
                or (more and (not matches or type(next_offset) is not int
                              or next_offset != previous + 1 or next_offset + len(pattern) > total))
                or (not more and next_offset is not None)):
            raise ValueError('Invalid find budget or continuation.')


def _payload(result):
    if not isinstance(result, dict):
        raise ValueError('Completed MCP call has no result object.')
    if 'isError' in result and not isinstance(result['isError'], bool):
        raise ValueError('MCP error flag is not a boolean.')
    blocks = result.get('content')
    structured = result.get('structuredContent')
    text_value = None
    if blocks is not None:
        if not isinstance(blocks, list) or len(blocks) != 1:
            raise ValueError('Expected exactly one complete JSON text content block.')
        block = blocks[0]
        if not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str):
            raise ValueError('MCP reply is not a complete JSON text content block.')
        text_value = _strict_json(block['text'])
    if structured is not None and not isinstance(structured, dict):
        raise ValueError('Structured MCP content is not an object.')
    if structured is not None and text_value is not None and structured != text_value:
        raise ValueError('Text and structured MCP payloads disagree.')
    value = structured if structured is not None else text_value
    if not isinstance(value, dict):
        raise ValueError('MCP content does not contain an object payload.')
    return value


def _semantic_call(item):
    """Normalize app-server/native serialization differences, not source data."""
    result = item.get('result')
    if isinstance(result, dict):
        result = {key: value for key, value in result.items()
                  if key not in ('_meta',) and not (key == 'structuredContent' and value is None)}
        # App-server omits isError even for failed MCP replies; native rollouts
        # retain it. Explicit mismatching values still remain distinguishable.
        result.setdefault('isError', item.get('status') in ('failed', 'error'))
    return json.dumps({'server': item.get('server'), 'tool': item.get('tool'),
                       'arguments': item.get('arguments'), 'status': item.get('status'),
                       'result': result, 'error': item.get('error')}, sort_keys=True)


def _native_truncated_duplicate(complete, abbreviated):
    """Recognize an observed native JSON-wrapper abbreviation, never a source.

    Native logging abbreviates the serialized nested MCP reply, sometimes in
    the middle of a JSON escape. Reconstruct that exact serialization from the
    receipt-verified transcript and require the omitted interval to be wholly
    inside the document text. This does not establish model-visible completeness.
    """
    try:
        for key in ('server', 'tool', 'arguments', 'status', 'error'):
            if complete.get(key) != abbreviated.get(key):
                return False
        if complete.get('tool') != 'open' or complete.get('status') != 'completed':
            return False

        def text_block(result):
            if (not isinstance(result, dict)
                    or set(result) - {'content', 'isError', 'structuredContent', '_meta'}
                    or result.get('isError', False) is not False
                    or result.get('structuredContent') is not None or result.get('_meta') is not None):
                raise ValueError('Unrecognized native envelope.')
            blocks = result.get('content')
            if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
                    or set(blocks[0]) != {'type', 'text'} or blocks[0]['type'] != 'text'
                    or not isinstance(blocks[0]['text'], str)):
                raise ValueError('Unrecognized native content block.')
            return blocks[0]['text']

        full_payload = text_block(complete.get('result'))
        short_text = text_block(abbreviated.get('result'))
        payload = _strict_json(full_payload)
        if not isinstance(payload, dict) or not isinstance(payload.get('result'), dict):
            return False
        if not isinstance(payload['result'].get('text'), str):
            return False

        def value_span(raw, path, start=0):
            # Full JSON has already passed strict parsing. Walk object members
            # to locate the actual result.text token without substring guesses.
            decoder = json.JSONDecoder()
            pos = start
            while raw[pos].isspace():
                pos += 1
            if raw[pos] != '{':
                raise ValueError('Expected object.')
            pos += 1
            while True:
                while raw[pos].isspace():
                    pos += 1
                key, pos = decoder.raw_decode(raw, pos)
                while raw[pos].isspace():
                    pos += 1
                if raw[pos] != ':':
                    raise ValueError('Expected member.')
                pos += 1
                while raw[pos].isspace():
                    pos += 1
                value_start = pos
                _, pos = decoder.raw_decode(raw, pos)
                if key == path[0]:
                    return (value_start, pos) if len(path) == 1 else value_span(raw, path[1:], value_start)
                while raw[pos].isspace():
                    pos += 1
                if raw[pos] != ',':
                    raise ValueError('Missing member.')
                pos += 1

        doc_start, doc_end = value_span(full_payload, ('result', 'text'))
        nested_prefix = '{"content":[{"type":"text","text":'
        full_text = json.dumps({'content': [{'type': 'text', 'text': full_payload}], 'isError': False},
                               ensure_ascii=False, separators=(',', ':'))
        # Exclude both quote delimiters of result.text. Each offset accounts
        # for the additional escaping of the entire payload in the MCP wrapper.
        content_start = len(nested_prefix) + 1
        text_start = content_start + len(json.dumps(full_payload[:doc_start + 1], ensure_ascii=False)[1:-1])
        text_end = content_start + len(json.dumps(full_payload[:doc_end - 1], ensure_ascii=False)[1:-1])
        markers = list(re.finditer(r'…([1-9][0-9]*) chars truncated…', short_text))
        if len(markers) != 1:
            return False
        marker = markers[0]
        prefix, suffix = short_text[:marker.start()], short_text[marker.end():]
        omitted_end = len(full_text) - len(suffix)
        if (not prefix or not suffix or not text_start <= len(prefix) < omitted_end <= text_end
                or not full_text.startswith(prefix) or not full_text.endswith(suffix)):
            return False
        return omitted_end - len(prefix) == int(marker.group(1))
    except (ValueError, TypeError, KeyError, IndexError):
        return False


def _native_wrappers(records, issues, expected_thread_id):
    wrappers = []
    for line, record in records:
        payload = record.get('payload')
        if record.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'item_completed':
            continue
        item = payload.get('item')
        if not isinstance(item, dict):
            continue
        native_type = item.get('type')
        if native_type not in ('McpToolCall', 'WebSearch', 'WebSearchCall'):
            continue
        if expected_thread_id and payload.get('thread_id') != expected_thread_id:
            _issue(issues, 'thread_mismatch', 'Native terminal tool event belongs to another thread.', line, 'native_session')
        item = dict(item, type='mcpToolCall' if native_type == 'McpToolCall' else 'webSearch')
        wrappers.append((line, {'kind': 'codex_event', 'stage': 'solve', 'node': 'author',
                               '_evidence_source': 'native_session', 'event': {
                                   'method': 'item/completed', 'params': {'item': item,
                                       'threadId': payload.get('thread_id'), 'turnId': payload.get('turn_id')}}}))
    return wrappers


def _native_terminated_cells(records):
    requested = {}
    result = []
    for line, record in records:
        payload = record.get('payload')
        if record.get('type') != 'response_item' or not isinstance(payload, dict):
            continue
        if payload.get('type') == 'function_call' and payload.get('name') == 'wait':
            try:
                args = _strict_json(payload.get('arguments', ''))
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict) and args.get('terminate') is True and isinstance(args.get('cell_id'), str):
                requested[payload.get('call_id')] = args['cell_id']
        elif payload.get('type') == 'function_call_output' and payload.get('call_id') in requested:
            output = payload.get('output')
            if isinstance(output, str) and output.startswith('Script terminated\n'):
                result.append({'cell_id': requested[payload['call_id']], 'call_id': payload['call_id'],
                               'line': line, 'source': 'native_session'})
    return result


def audit_problem(problem_id, cutoff, transcript_path, broker_log_path, expected_thread_id=None,
                  native_session_path=None, content_limit=20000, require_navigation_receipts=False,
                  full_document_default=False):
    """Audit one transcript/log pair; callers must bind paths to their controller.

    ``passed`` means no recorded evidence violations, not that all work completed.
    ``status`` distinguishes an otherwise clean pending checkpoint. The batch CLI
    additionally checks manifest, per-problem policy, workspace and session binding.
    ``full_document_default`` selects the versioned interface; the default retains
    the earlier policy-bounded page and find rules for historical records.
    """
    issues = []
    counts = dict.fromkeys(COUNT_KEYS, 0)
    report = {'problem_id': problem_id, 'cutoff': cutoff, 'passed': False,
              'navigation_version': FULL_DOCUMENT_NAVIGATION if full_document_default else 'legacy-paged',
              'issues': issues, 'counts': counts, 'sources': [], 'checkpoint': {},
              'warnings': [],
              'limitations': list(LIMITATIONS),
              'ownership': 'caller_supplied_paths; use audit_batch for controller binding'}
    try:
        cutoff_time = _timestamp(cutoff)
    except (HistoricalSearchError, ValueError, TypeError) as exc:
        _issue(issues, 'invalid_cutoff', str(exc))
        report['status'] = 'issues'
        return report
    # Broker writes evidence before emitting replies. Read transcript first so an
    # independently growing broker log is at least as recent as returned replies.
    transcript, report['checkpoint']['transcript'] = _read_jsonl(transcript_path, issues, 'transcript')
    if native_session_path is not None:
        native, report['checkpoint']['native_session'] = _read_jsonl(native_session_path, issues, 'native_session')
        transcript.extend(_native_wrappers(native, issues, expected_thread_id))
        report['terminated_code_cells'] = _native_terminated_cells(native)
    broker, report['checkpoint']['broker'] = _read_jsonl(broker_log_path, issues, 'broker')
    evidence = {}
    broker_counts = Counter()
    for line, event in broker:
        event_name = event.get('event')
        broker_counts[str(event_name)] += 1
        if not _same_time(event.get('cutoff'), cutoff_time):
            _issue(issues, 'cutoff_mismatch', 'Broker event does not have the expected fixed cutoff.', line, 'broker')
            continue
        if event_name not in ('capture_verified', 'open_completed', 'find_completed'):
            continue
        try:
            capture = _timestamp(event.get('capture'))
        except (HistoricalSearchError, ValueError, TypeError):
            continue
        if not (_hash_string(event.get('url_sha256')) and _hash_string(event.get('body_sha256'))):
            continue
        key = (event_name, event['url_sha256'], capture, event['body_sha256'])
        evidence.setdefault(key, []).append((line, event))
    report['broker_event_counts'] = dict(broker_counts)

    pending = {}
    terminal = {}
    receipt_verified_transcript = {}
    forbidden_seen = set()
    unexpected = {}
    unexpected_terminal = {}
    threads = set()
    for line, record in transcript:
        record_source = record.get('_evidence_source', 'transcript')
        if record.get('kind') != 'codex_event':
            if (record.get('kind') == 'request' and record.get('node') == 'author'
                    and expected_thread_id and record.get('threadId') != expected_thread_id):
                _issue(issues, 'thread_mismatch', 'Author request belongs to another thread.', line, record_source)
            continue
        event = record.get('event')
        if not isinstance(event, dict):
            _issue(issues, 'malformed_tool_call', 'Codex event is not an object.', line, record_source)
            continue
        method = event.get('method')
        params = event.get('params', {})
        if not isinstance(params, dict):
            continue
        thread = params.get('threadId')
        if isinstance(thread, str):
            threads.add(thread)
        if (expected_thread_id and record.get('stage') == 'solve' and record.get('node') == 'author'
                and thread is not None and thread != expected_thread_id):
            _issue(issues, 'thread_mismatch', 'Author event belongs to another thread.', line, record_source)
        item = params.get('item')
        # Legacy native-web events are also violations, but never sources.
        if method in ('codex/event/web_search_begin', 'codex/event/web_search_end'):
            identity = ('web', params.get('call_id', params.get('id', line)))
            if identity not in forbidden_seen:
                forbidden_seen.add(identity)
                counts['native_web_calls'] += 1
                _issue(issues, 'native_web_search', 'Recorded native web search execution.', line, record_source)
        if method not in ('item/started', 'item/completed') or not isinstance(item, dict):
            continue
        item_type = item.get('type')
        identity = (thread, params.get('turnId'), item.get('id'))
        if item_type in ('webSearch', 'webSearchCall', 'web_search_call'):
            key = ('web', identity)
            if key not in forbidden_seen:
                forbidden_seen.add(key)
                counts['native_web_calls'] += 1
                _issue(issues, 'native_web_search', 'Recorded native web search execution.', line, record_source)
            continue
        if item_type != 'mcpToolCall':
            continue
        server, tool = item.get('server'), item.get('tool')
        if server != 'historical_search' or tool not in ('search', 'open', 'find'):
            key = ('mcp', identity)
            if key not in forbidden_seen:
                forbidden_seen.add(key)
                counts['unexpected_mcp_calls'] += 1
            detail = unexpected.setdefault(identity, {'call_id': item.get('id'), 'server': server, 'tool': tool,
                                                     'started': False, 'completed': False,
                                                     'source': record_source, 'line': line})
            detail['started'] |= method == 'item/started'
            if method == 'item/completed':
                signature = _semantic_call(item)
                if identity in unexpected_terminal and unexpected_terminal[identity] != signature:
                    _issue(issues, 'conflicting_tool_completion', 'Unexpected call ID has inconsistent completed content.', line, record_source)
                unexpected_terminal[identity] = signature
                detail.update(completed=True, status=item.get('status'), source=record_source, line=line)
                try:
                    value = _payload(item.get('result'))
                    result = item.get('result')
                    field = {'list_mcp_resources': 'resources', 'list_mcp_resource_templates': 'resourceTemplates'}.get(tool)
                    clean_result = (isinstance(result, dict) and set(result) <= {'content', 'structuredContent', '_meta', 'isError'}
                                    and result.get('_meta') is None and result.get('isError', False) is False)
                    if (server == 'codex' and field and value == {field: []} and clean_result
                            and item.get('status') == 'completed' and item.get('error') is None):
                        detail['result_summary'] = 'empty_resources' if field == 'resources' else 'empty_resource_templates'
                    else:
                        detail['result_summary'] = 'unvalidated_content'
                except (TypeError, ValueError):
                    detail['result_summary'] = 'error_or_unparseable'
            continue
        if not isinstance(item.get('id'), str) or not item['id']:
            _issue(issues, 'malformed_tool_call', 'MCP execution has no call ID.', line, record_source)
            continue
        if expected_thread_id and record.get('stage') == 'solve' and record.get('node') == 'author' and thread is None:
            _issue(issues, 'thread_mismatch', 'Author MCP execution has no thread identity.', line, record_source)
        if method == 'item/started':
            if identity not in terminal:
                pending[identity] = {'call_id': item['id'], 'tool': tool, 'line': line, 'source': record_source}
            continue
        serialized = _semantic_call(item)
        if identity in terminal:
            if terminal[identity] != serialized:
                if (record_source == 'native_session' and identity in receipt_verified_transcript
                        and _native_truncated_duplicate(receipt_verified_transcript[identity], item)):
                    _issue(report['warnings'], 'native_reply_abbreviated',
                           'Native rollout abbreviated a duplicate reply. Exact metadata, prefix/suffix and escaped omission count match the full receipt-verified transcript; model-visible completeness is not established.',
                           line, record_source)
                else:
                    _issue(issues, 'conflicting_tool_completion', 'Repeated call ID has inconsistent completed content.', line, record_source)
            continue
        terminal[identity] = serialized
        pending.pop(identity, None)
        counts['completed_calls'] += 1
        status = item.get('status')
        if status in ('cancelled', 'canceled'):
            counts['cancelled_calls'] += 1
            continue
        result = item.get('result')
        if (status in ('failed', 'error') or item.get('error') is not None
                or isinstance(result, dict) and result.get('isError') is True):
            counts['error_calls'] += 1
            continue
        if status != 'completed':
            _issue(issues, 'malformed_tool_call', 'Completed event has an unknown or nonterminal status.', line, record_source)
            continue
        arguments = item.get('arguments')
        if not _arguments_valid(tool, arguments, content_limit, full_document_default):
            _issue(issues, 'malformed_tool_call', 'Historical tool arguments do not match the fixed interface.', line, record_source)
            continue
        try:
            payload = _payload(result)
        except (ValueError, TypeError) as exc:
            _issue(issues, 'malformed_tool_result', str(exc), line, record_source)
            continue
        if not _same_time(payload.get('cutoff'), cutoff_time):
            _issue(issues, 'cutoff_mismatch', 'MCP reply does not have the expected fixed cutoff.', line, record_source)
        documents = payload.get('results') if tool == 'search' else [payload.get('result')]
        if not isinstance(documents, list) or any(not isinstance(doc, dict) for doc in documents):
            _issue(issues, 'malformed_tool_result', 'MCP reply lacks the expected document collection.', line, record_source)
            continue
        counts['successful_calls'] += 1
        counts['incomplete_responses'] += int(payload.get('incomplete') is True)
        for index, doc in enumerate(documents):
            counts['returned_documents'] += 1
            source = {'call_id': item['id'], 'tool': tool, 'line': line,
                      'evidence_source': record_source,
                      'document_index': index, 'url': doc.get('url'),
                      'capture_time': doc.get('capture_time'), 'sha256': doc.get('sha256'),
                      'archive_url': doc.get('archive_url'), 'matched': False}
            report['sources'].append(source)
            before = len(issues)
            try:
                url = canonical_url(doc.get('url'))
                if url != doc['url']:
                    raise ValueError('Returned URL is not its exact canonical source identity.')
                capture = _timestamp(doc.get('capture_time'))
                if not _hash_string(doc.get('sha256')):
                    raise ValueError('Returned raw-body SHA256 is invalid.')
                navigation_fields = {'offset', 'end_offset', 'total_chars', 'next_offset', 'has_more',
                                     'text_sha256', 'document_text_sha256', 'offset_unit'}
                navigation = tool == 'find' or tool == 'open' and (require_navigation_receipts or full_document_default
                    or navigation_fields.intersection(doc) or set(arguments) != {'url'})
                if (not isinstance(doc.get('title'), str) or doc.get('untrusted_source_content') is not True
                        or not navigation and (not isinstance(doc.get('text'), str) or not doc['text'].strip())):
                    raise ValueError('Returned source lacks title/text or untrusted-content marker.')
            except (HistoricalSearchError, ValueError, TypeError) as exc:
                _issue(issues, 'invalid_source', str(exc), line, record_source)
                continue
            if capture >= cutoff_time:
                _issue(issues, 'source_after_cutoff', 'Returned capture is at or after the exclusive cutoff.', line, record_source)
            archive_url = 'https://web.archive.org/web/' + capture.strftime('%Y%m%d%H%M%S') + 'id_/' + url
            if capture.microsecond or doc.get('archive_url') != archive_url:
                _issue(issues, 'archive_identity_mismatch', 'Archive URL does not bind the exact source and capture.', line, record_source)
            if tool in ('open', 'find'):
                try:
                    requested = canonical_url(arguments['url'])
                except (HistoricalSearchError, ValueError, TypeError):
                    requested = None
                if requested != url:
                    _issue(issues, 'open_identity_mismatch', 'Returned source does not match requested URL.', line, record_source)
            url_hash = _sha(url.encode('utf-8'))
            kind = 'capture_verified' if tool == 'search' else tool + '_completed'
            events = evidence.get((kind, url_hash, capture, doc['sha256']), [])
            if not events:
                _issue(issues, 'source_evidence_missing', 'No matching URL/capture/body SHA in this run\'s ' + kind + ' events.', line, record_source)
            receipt_required = navigation or any('result_sha256' in event for _, event in events)
            if receipt_required:
                if (set(payload) - {'cutoff', 'result', 'limitation'}
                        or 'limitation' in payload and payload['limitation'] != LIMITATION
                        or set(result) - {'content', 'structuredContent', 'isError', '_meta'}
                        or result.get('_meta') is not None):
                    _issue(issues, 'unvalidated_navigation_envelope', 'Navigation reply contains unvalidated content outside the receipted document.', line, record_source)
                try:
                    _check_navigation(tool, arguments, doc, content_limit, full_document_default)
                except (ValueError, TypeError, KeyError) as exc:
                    _issue(issues, 'invalid_navigation_result', str(exc), line, record_source)
                digest = _sha(canonical_json(doc))
                receipt_lines = [n for n, event in events if event.get('result_sha256') == digest]
                if not receipt_lines:
                    _issue(issues, 'response_receipt_missing', 'No matching broker receipt for the complete returned content.', line, record_source)
                source.update(result_sha256=digest, receipt_broker_lines=receipt_lines)
            source.update(url_sha256=url_hash, broker_event=kind, broker_lines=[n for n, _ in events])
            source['matched'] = len(issues) == before and _same_time(payload.get('cutoff'), cutoff_time)
            counts['matched_documents'] += int(source['matched'])
            if source['matched'] and receipt_required and record_source == 'transcript' and tool == 'open':
                receipt_verified_transcript[identity] = item
    counts['pending_calls'] = len(pending)
    report['pending_calls'] = list(pending.values())
    report['unexpected_calls'] = list(unexpected.values())
    for call in unexpected.values():
        if call.get('result_summary') in ('empty_resources', 'empty_resource_templates'):
            _issue(report['warnings'], 'empty_builtin_enumeration',
                   'Unexpected built-in metadata enumeration returned an exactly empty collection; no source content was returned by this call.',
                   call['line'], call['source'])
        else:
            _issue(issues, 'unexpected_mcp_tool', 'Unexpected MCP attempt: ' + str(call['server']) + '.' + str(call['tool']),
                   call['line'], call['source'])
    report['thread_ids'] = sorted(threads)
    report['checkpoint_pending'] = bool(pending) or any(c.get('trailing_bytes') for c in report['checkpoint'].values())
    report['passed'] = not issues
    report['status'] = 'issues' if issues else 'pending' if report['checkpoint_pending'] else 'passed'
    return report


def _load_json(path):
    value = _strict_json(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object: ' + str(path))
    return value


def _session_identity(codex_home, expected_thread, folder):
    # Native session payloads can contain private system text. Read only their
    # metadata record; report just IDs, workspace and the path, never instructions.
    candidates = list((codex_home / 'author' / 'sessions').rglob('*' + expected_thread + '.jsonl'))
    if len(candidates) != 1:
        raise ValueError('Expected exactly one author native session for the pinned thread.')
    with candidates[0].open('rb') as stream:
        line = stream.readline()
    if not line.endswith(b'\n'):
        raise ValueError('Author native session metadata is not a completed JSONL line.')
    metadata = _strict_json(line.decode('utf-8'))
    payload = metadata.get('payload', {})
    if metadata.get('type') != 'session_meta' or payload.get('id') != expected_thread:
        raise ValueError('Author native session does not identify the pinned thread.')
    if Path(payload.get('cwd', '')).resolve() != folder:
        raise ValueError('Author native session workspace differs from the prepared workspace.')
    return {'path': str(candidates[0]), 'thread_id': expected_thread, 'workspace': str(folder),
            'metadata_sha256': _sha(line)}


def audit_batch(control_root, manifest_path=DEFAULT_MANIFEST, problems=None):
    """Audit each manifest run, including failures/missing runs as separate rows."""
    control_root = Path(control_root).resolve()
    manifest = _load_json(manifest_path)
    prepared = _load_json(control_root / 'prepared-runs.json')
    runs = manifest.get('runs', [])
    if [item.get('id') for item in runs] != ['P%02d' % number for number in range(1, 11)]:
        raise ValueError('Manifest must contain exactly P01 through P10 in order.')
    plans = prepared.get('runs', [])
    if not isinstance(plans, list):
        raise ValueError('Prepared run list is invalid.')
    selected = prepared.get('selectedProblemIds')
    navigation_version = prepared.get('navigationVersion')
    discovery_provider = prepared.get('discoveryProvider')
    if navigation_version not in (None, FULL_DOCUMENT_NAVIGATION):
        raise ValueError('Prepared navigation interface version is unknown.')
    if navigation_version and (prepared.get('schemaVersion') != 2 or selected is None):
        raise ValueError('Versioned navigation requires a bound schema-two preparation record.')
    if discovery_provider not in (None, CODEX_DISCOVERY_PROVIDER):
        raise ValueError('Prepared discovery provider is unknown.')
    if discovery_provider and (prepared.get('schemaVersion') != 2 or selected is None):
        raise ValueError('Versioned discovery requires a bound schema-two preparation record.')
    if selected is not None or problems is not None:
        wanted = selected if problems is None else problems
        all_ids = [item['id'] for item in runs]
        if (not isinstance(wanted, list) or not wanted or any(not isinstance(p, str) or p not in all_ids for p in wanted)
                or len(set(wanted)) != len(wanted)):
            raise ValueError('Invalid audit problem selection.')
        wanted = [p for p in all_ids if p in wanted]
        if selected != wanted or [p.get('id') for p in plans] != wanted:
            raise ValueError('Audit selection does not match the prepared batch.')
        if prepared.get('manifest') != manifest or prepared.get('manifestSha256') != _sha(Path(manifest_path).read_bytes()):
            raise ValueError('Prepared subset does not bind the full official manifest.')
        if (prepared.get('schemaVersion') != 2 or prepared.get('controlRoot') != str(control_root)
                or not isinstance(prepared.get('runsRoot'), str)):
            raise ValueError('Prepared subset roots or schema do not match this controller.')
        runs_root = Path(prepared['runsRoot']).resolve()
        if any(Path(p.get('folder', '')).resolve() != runs_root / ('firstproof-b3-' + p['id'].lower()) for p in plans):
            raise ValueError('Prepared subset workspace is outside the recorded runs root.')
        runs = [item for item in runs if item['id'] in wanted]
    reports = []
    for item in runs:
        problem = item['id']
        cutoff = item['cutoffExclusiveUtc']
        controller = control_root / problem
        binding = []
        report = None
        try:
            matching = [p for p in plans if p.get('id') == problem]
            if len(matching) != 1:
                raise ValueError('Missing or duplicated prepared run for ' + problem)
            plan = matching[0]
            folder = Path(plan['folder']).resolve()
            policy_path = controller / 'retrieval-policy.json'
            if Path(plan['policy']).resolve() != policy_path.resolve():
                raise ValueError('Prepared policy is outside the expected problem controller.')
            policy = _load_json(policy_path)
            if policy.get('metadata', {}).get('navigationVersion') != navigation_version:
                raise ValueError('Policy navigation interface differs from the prepared batch.')
            broker = policy['broker']
            log_path = controller / 'historical-search.jsonl'
            if Path(broker['auditLog']).resolve() != log_path.resolve():
                raise ValueError('Broker audit path belongs to another problem.')
            if (policy.get('mode') != 'historical' or policy.get('runId') != plan.get('runId')
                    or plan.get('runId') != 'firstproof-b3-' + problem.lower()):
                raise ValueError('Run identity or historical retrieval mode does not match.')
            if policy.get('metadata', {}).get('problemId') != item.get('problemId'):
                raise ValueError('Retrieval policy belongs to another problem.')
            for value in (policy.get('cutoffExclusiveUtc'), plan.get('cutoffExclusiveUtc')):
                if not _same_time(value, _timestamp(cutoff)):
                    raise ValueError('Prepared run or policy cutoff differs from the manifest.')
            if Path(policy['isolation']['workspace']).resolve() != folder:
                raise ValueError('Policy workspace differs from prepared run workspace.')
            input_sha = _sha((folder / 'input.md').read_bytes())
            if any(value != input_sha for value in (item['statementUtf8Sha256'], plan.get('inputSha256'), policy['metadata'].get('inputSha256'))):
                raise ValueError('Official input bytes or problem identity do not match.')
            broker_path = controller / 'broker-policy.json'
            if Path(broker['policyFile']).resolve() != broker_path.resolve():
                raise ValueError('Broker policy belongs to another problem.')
            broker_policy = _load_json(broker_path)
            policy_discovery_provider = policy.get('metadata', {}).get('discoveryProvider')
            if (discovery_provider is not None or policy_discovery_provider is not None
                    or broker_policy.get('provider') == CODEX_DISCOVERY_PROVIDER):
                if (discovery_provider != CODEX_DISCOVERY_PROVIDER
                        or policy_discovery_provider != discovery_provider
                        or broker_policy.get('provider') != discovery_provider):
                    raise ValueError('Discovery provider differs from the prepared batch.')
                discovery = broker_policy.get('discovery')
                if not isinstance(discovery, dict):
                    raise ValueError('Prepared discovery settings are missing.')
                for key, name in (('auth_home', 'discovery-auth'), ('state_root', 'discovery-state')):
                    expected = controller / name
                    if discovery.get(key) != str(expected) or Path(discovery[key]).resolve() != expected:
                        raise ValueError('Discovery ' + key + ' belongs to another problem controller.')
            if Path(broker_policy['solver_workspace']).resolve() != folder:
                raise ValueError('Broker policy workspace belongs to another problem.')
            args = broker['args']
            for flag, wanted in (('--cutoff', cutoff), ('--policy-file', str(broker_path)),
                                 ('--policy-sha256', _sha(broker_path.read_bytes())), ('--audit-log', str(log_path))):
                if args.count(flag) != 1 or args.index(flag) + 1 >= len(args) or args[args.index(flag) + 1] != wanted:
                    raise ValueError('Pinned broker argument mismatch: ' + flag)
            settings = _load_json(folder / 'job-settings.json')
            if discovery_provider and (not isinstance(settings.get('authorModel'), str)
                    or discovery.get('model') != settings['authorModel']):
                raise ValueError('Discovery model differs from the prepared author model.')
            if Path(settings.get('retrievalPolicyPath', '')).resolve() != policy_path.resolve():
                raise ValueError('Job settings refer to another retrieval policy.')
            thread = settings.get('goalThreadId')
            if not isinstance(thread, str) or not thread:
                raise ValueError('Author thread is not recorded in job settings.')
            home = Path(policy['isolation']['codexHome']).resolve()
            if home != (controller / 'codex-home').resolve():
                raise ValueError('Native author session root belongs to another problem.')
            session = _session_identity(home, thread, folder)
            limit = broker_policy.get('max_content_chars', 20000)
            if type(limit) is not int or not 100 <= limit <= 50000:
                raise ValueError('Invalid pinned document page budget.')
            report = audit_problem(problem, cutoff, folder / 'transcript.jsonl', log_path, thread, session['path'],
                                   content_limit=limit, require_navigation_receipts=prepared.get('schemaVersion') == 2,
                                   full_document_default=navigation_version == FULL_DOCUMENT_NAVIGATION)
            report['ownership'] = 'manifest_prepared_policy_workspace_input_thread_and_native_session_bound'
            report['session'] = session
            report['binding'] = {'run_id': plan['runId'], 'workspace': str(folder),
                                 'navigation_version': navigation_version or 'legacy-paged',
                                 'discovery_provider': broker_policy.get('provider'),
                                 'policy': str(policy_path), 'policy_sha256': _sha(policy_path.read_bytes()),
                                 'broker_policy_sha256': _sha(broker_path.read_bytes()), 'input_sha256': input_sha}
            for source in report['sources']:
                if not isinstance(source.get('url'), str):
                    continue
                parts = urlsplit(source['url'])
                for prefix in broker_policy.get('deny_url_prefixes', []):
                    denied = urlsplit(prefix)
                    if parts.hostname == denied.hostname and unquote(parts.path).startswith(unquote(denied.path)):
                        _issue(report['issues'], 'denied_source', 'Returned source is excluded by the pinned broker policy.', source['line'], 'transcript')
        except (OSError, ValueError, TypeError, KeyError, HistoricalSearchError) as exc:
            _issue(binding, 'run_binding_failed', str(exc))
        if report is None:
            report = {'problem_id': problem, 'cutoff': cutoff, 'counts': dict.fromkeys(COUNT_KEYS, 0),
                      'issues': [], 'sources': [], 'checkpoint': {}, 'checkpoint_pending': False}
        report['issues'].extend(binding)
        report['passed'] = not report['issues']
        report['status'] = 'issues' if report['issues'] else 'pending' if report['checkpoint_pending'] else 'passed'
        reports.append(report)
    totals = {key: sum(row['counts'][key] for row in reports) for key in COUNT_KEYS}
    return {'schema_version': 1, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'control_root': str(control_root), 'manifest': str(Path(manifest_path).resolve()),
            'manifest_sha256': _sha(Path(manifest_path).read_bytes()),
            'selected_problem_ids': [item['id'] for item in runs],
            'navigation_version': navigation_version or 'legacy-paged',
            'discovery_provider': discovery_provider,
            'passed': all(row['passed'] for row in reports),
            'checkpoint_pending': any(row['checkpoint_pending'] for row in reports),
            'counts': totals, 'runs': reports, 'limitations': list(LIMITATIONS)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--problems', nargs='+', help='Require the exact prepared problem subset, e.g. P09 P10.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = audit_batch(args.control_root, args.manifest, problems=args.problems)
        output = args.output.resolve()
        # Keep reports outside solver workspaces and never overwrite evidence.
        plans = _load_json(args.control_root / 'prepared-runs.json').get('runs', [])
        if any(output == Path(plan['folder']).resolve() or output.is_relative_to(Path(plan['folder']).resolve()) for plan in plans):
            raise ValueError('Audit output must be outside all solver workspaces.')
        evidence_paths = {args.manifest.resolve(), (args.control_root / 'prepared-runs.json').resolve()}
        for plan in plans:
            controller = (args.control_root / plan['id']).resolve()
            evidence_paths.update(controller / name for name in ('retrieval-policy.json', 'broker-policy.json', 'historical-search.jsonl'))
        if output in evidence_paths or output.is_relative_to((args.control_root / 'retrieval-state').resolve()):
            raise ValueError('Audit output must not overwrite controller evidence or cache.')
        if any(output.is_relative_to((args.control_root / plan['id'] / 'codex-home').resolve()) for plan in plans):
            raise ValueError('Audit output must not overwrite native session state.')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({'passed': report['passed'], 'checkpoint_pending': report['checkpoint_pending'],
                      'counts': report['counts'], 'output': str(output)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
