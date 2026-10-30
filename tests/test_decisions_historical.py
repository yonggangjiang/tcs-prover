"""Owner decisions for historical (web-cutoff) retrieval and runs.

Offline: stand-in Codex binaries and app-server doubles, fake archive clients
and patched clocks. No model, CLI, broker process or network request runs.
"""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import audit_historical_trajectory as trajectory
from retrieval import historical_cache as hc
from retrieval import historical_codex_discovery as discovery
from retrieval import historical_rate_limit as hr
from retrieval import historical_search as hs
from retrieval import historical_transport as ht
from retrieval import runtime as retrieval_runtime
from ui import retrieval, server
import workflow_runner as runtime

import test_historical_search as fixtures
from test_audit_fixes_retrieval import RuntimePolicyFixture, UIRetrievalFixture, UI_CUTOFF
from test_goal_runtime import PROMPT, PROMPTS, SETTINGS, Runtime, success
from test_historical_mcp_concurrency import FakeEngine, Harness, request, search
from test_model_retry import RetryCase


def cached_record(item):
    return {**item.__dict__, 'body': base64.b64encode(item.body).decode('ascii')}


class SharedStateEngineFixture(unittest.TestCase):
    """An archive engine with the shared capture cache and pacing of a controller."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.policy_path = self.root / 'policy.json'
        self.audit = self.root / 'audit.jsonl'

    def engine(self, client=None, **changes):
        policy = {'schema_version': 1, 'provider': 'scholarly_wayback',
                  'rate_limit_directory': str(self.root / 'pacing'),
                  'solver_workspace': str(self.root / 'solver'), **changes}
        self.policy_path.write_text(json.dumps(policy), encoding='utf-8')
        return hs.HistoricalSearchEngine(self.policy_path, fixtures.CUTOFF, self.audit,
                                         hs.sha256(self.policy_path.read_bytes()),
                                         client=client or fixtures.FakeClient())

    def events(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def quarantined(self):
        folder = self.root / 'pacing' / 'captures' / hc.QUARANTINE
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


class SharedStateRobustnessTests(SharedStateEngineFixture):
    """I-086: one stray file, mode change or I/O error no longer disables retrieval."""

    def cache(self, **options):
        return hc.CaptureCache(self.root / 'captures', workspace_root=self.root / 'solver', **options)

    def test_stray_file_is_moved_aside_with_a_log_line_and_writes_continue(self):
        reasons = []
        cache = self.cache(on_quarantine=reasons.append)
        cache.put(fixtures.SOURCE, cached_record(fixtures.snapshot()))
        (self.root / 'captures' / '.DS_Store').write_bytes(b'Finder state')
        other = fixtures.SOURCE + '/other'
        cache.put(other, cached_record(fixtures.snapshot(url=other)))
        self.assertIsNotNone(cache.get(other))
        self.assertFalse((self.root / 'captures' / '.DS_Store').exists())
        moved = list((self.root / 'captures' / hc.QUARANTINE).iterdir())
        self.assertEqual([path.name.endswith('.DS_Store') for path in moved], [True])
        self.assertEqual(moved[0].read_bytes(), b'Finder state')
        self.assertEqual(reasons, ['unexpected entry'])

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_loosened_modes_are_tightened_but_writable_entries_are_never_trusted(self):
        cache = self.cache()
        cache.put(fixtures.SOURCE, cached_record(fixtures.snapshot()))
        entry = next((self.root / 'captures').glob('*.json'))
        (self.root / 'captures').chmod(0o755)
        entry.chmod(0o644)
        self.assertIsNotNone(cache.get(fixtures.SOURCE))
        self.assertEqual(stat.S_IMODE((self.root / 'captures').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        entry.chmod(0o666)  # Others could have forged it.
        with self.assertRaises(hc.CacheError) as caught:
            cache.get(fixtures.SOURCE)
        self.assertEqual(caught.exception.kind, 'invalid')
        cache.put(fixtures.SOURCE, cached_record(fixtures.snapshot()))
        self.assertEqual(len(list((self.root / 'captures' / hc.QUARANTINE).iterdir())), 1)
        self.assertIsNotNone(cache.get(fixtures.SOURCE))

    def test_invalid_cached_capture_is_moved_aside_and_fetched_again(self):
        engine = self.engine()
        forged = {**cached_record(fixtures.snapshot()), 'memento_datetime': 'Fri, 02 Oct 2026 12:00:00 GMT'}
        engine.provider.persistent_cache.put(fixtures.SOURCE, forged)
        result = engine.open(fixtures.SOURCE)['result']
        self.assertEqual(result['sha256'], fixtures.snapshot().body_sha256)
        self.assertEqual(len(self.quarantined()), 1)
        self.assertTrue(engine.provider.client.calls)
        reasons = [event.get('reason') for event in self.events() if event['event'] == 'archive_cache_unavailable']
        self.assertEqual(reasons, ['invalid_entry'])
        # The fresh, verified capture replaced the entry that was moved aside.
        self.assertEqual(engine.provider.persistent_cache.get(fixtures.SOURCE)['memento_datetime'],
                         fixtures.snapshot().memento_datetime)

    def test_cache_lock_timeout_and_write_failure_still_return_the_verified_capture(self):
        engine = self.engine()
        with mock.patch.object(engine.provider.persistent_cache, 'get',
                               side_effect=hc.CacheError('busy', kind='busy')), \
                mock.patch.object(engine.provider.persistent_cache, 'put',
                                  side_effect=hc.CacheError('disk full')):
            result = engine.open(fixtures.SOURCE)['result']
        self.assertIn('Graph lemma', result['text'])
        names = [(event['event'], event.get('reason')) for event in self.events()
                 if event['event'].startswith('archive_cache')]
        self.assertEqual(names, [('archive_cache_unavailable', 'busy'), ('archive_cache_write_failed', 'unavailable')])

    def test_unusable_cache_folder_disables_only_the_cache(self):
        (self.root / 'pacing').mkdir(mode=0o700)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        (self.root / 'pacing' / 'captures').symlink_to(elsewhere, target_is_directory=True)
        engine = self.engine()
        self.assertIsNone(engine.provider.persistent_cache)
        self.assertIn('Graph lemma', engine.open(fixtures.SOURCE)['result']['text'])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_failed_audit_writes_are_kept_in_order_and_retried_instead_of_disabling_retrieval(self):
        real = self.audit

        class FlakyPath:
            failing = True

            def open(self, *args, **kwargs):
                if self.failing:
                    raise OSError('No space left on device')
                return real.open(*args, **kwargs)

            def __str__(self):
                return str(real) + '#flaky'

        path = FlakyPath()
        with mock.patch.object(hs.threading, 'Thread') as worker:  # No real 60-second retry thread.
            log = hs.AuditLog(path, hs.parse_cutoff(fixtures.CUTOFF), 'test')
            log.write('search_requested', sequence=1)
            log.write('search_completed', sequence=2)
        worker.return_value.start.assert_called_once()
        self.assertFalse(real.exists())
        path.failing = False
        log.write('search_requested', sequence=3)  # Queued behind the earlier records, never ahead.
        self.assertFalse(real.exists())
        self.assertTrue(hs.AuditLog.flush_pending())
        records = [json.loads(line) for line in real.read_text().splitlines()]
        self.assertEqual([record['event'] for record in records],
                         ['startup', 'search_requested', 'search_completed', 'search_requested'])
        self.assertEqual([record.get('sequence') for record in records], [None, 1, 2, 3])
        hs.AuditLog._flusher = None

    def test_audit_backlog_limit_is_explicit(self):
        class Broken:
            def open(self, *args, **kwargs):
                raise OSError('read-only file system')

            def __str__(self):
                return 'broken-audit-path'

        with mock.patch.object(hs.threading, 'Thread'), mock.patch.object(hs.AuditLog, 'MAX_PENDING', 2):
            log = hs.AuditLog(Broken(), hs.parse_cutoff(fixtures.CUTOFF), 'test')
            log.write('first')
            with self.assertRaises(hs.HistoricalSearchError) as caught:
                log.write('second')
        self.assertEqual(caught.exception.code, 'audit_failure')
        hs.AuditLog._pending.pop('broken-audit-path', None)
        hs.AuditLog._flusher = None

    def test_corrupt_pacing_state_is_moved_aside_and_pacing_restarts_from_one_interval(self):
        clock = {'now': 100.0}
        limiter = hr.SharedHostLimiter(self.root / 'rates', workspace_root=self.root / 'solver',
                                       clock=lambda: clock['now'], wall_clock=lambda: 1000.0 + clock['now'],
                                       sleep=lambda seconds: clock.__setitem__('now', clock['now'] + seconds))
        state = self.root / 'rates' / 'wayback.json'
        state.write_text('{"version": 1, "query": "not timing state"}')
        state.chmod(0o600)
        with limiter.slot('web.archive.org', clock['now'] + 60):
            started = clock['now']
        self.assertEqual(started, 103.0)
        self.assertEqual(len(list((self.root / 'rates' / hr.QUARANTINE).iterdir())), 1)
        self.assertEqual(set(json.loads(state.read_text())), {'version', 'next_wall', 'next_monotonic',
                                                              'written_monotonic'})


class ArchiveRetryTests(unittest.TestCase):
    """I-028: every 5xx is retried, cooldowns are capped and fail at once with a time."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.clock = {'now': 100.0, 'wall': 1700000000.0}

    def limiter(self):
        def sleep(seconds):
            self.clock['now'] += seconds
            self.clock['wall'] += seconds

        return hr.SharedHostLimiter(self.root / 'rates', workspace_root=self.root / 'solver',
                                    clock=lambda: self.clock['now'], wall_clock=lambda: self.clock['wall'],
                                    sleep=sleep)

    def test_every_server_error_status_is_retried(self):
        self.assertTrue({500, 501, 505, 520, 522, 524, 530, 599} <= ht.RETRY_STATUSES)
        self.assertTrue({408, 429} <= ht.RETRY_STATUSES)
        self.assertFalse({400, 403, 404, 202} & ht.RETRY_STATUSES)

    def test_stored_retry_after_is_capped(self):
        limiter = self.limiter()
        limiter.defer('web.archive.org', 86400)
        state = json.loads((self.root / 'rates' / 'wayback.json').read_text())
        self.assertEqual(state['next_monotonic'] - self.clock['now'], hr.MAX_COOLDOWN_SECONDS)
        # A longer pause an older version stored is shortened too.
        state.update(next_wall=self.clock['wall'] + 86400, next_monotonic=self.clock['now'] + 86400)
        (self.root / 'rates' / 'wayback.json').write_text(json.dumps(state))
        with limiter.slot('web.archive.org', self.clock['now'] + 3600):
            started = self.clock['now']
        self.assertLessEqual(started - 100.0, hr.MAX_COOLDOWN_SECONDS + 3.0)

    def test_cooldown_beyond_the_deadline_fails_at_once_and_says_when(self):
        limiter = self.limiter()
        limiter.defer('web.archive.org', 300)
        with self.assertRaises(hr.RateLimitError) as caught:
            with limiter.slot('web.archive.org', self.clock['now'] + 30):
                self.fail('A request started inside the provider cooldown.')
        self.assertEqual(caught.exception.code, 'provider_cooldown')
        self.assertAlmostEqual(caught.exception.retry_at, self.clock['wall'] + 300, delta=1)
        self.assertEqual(self.clock['now'], 100.0)  # Not one second slept.

    def test_transport_tells_the_model_when_to_try_again(self):
        retry_at = 1700000420.0

        class Cooling:
            def slot(self, host, deadline):
                raise hr.RateLimitError('cooling', 'provider_cooldown', retry_at=retry_at)

        client = ht.RestrictedHttpClient(coordinator=Cooling(), retries=0)
        with mock.patch.object(ht.socket, 'getaddrinfo'), self.assertRaises(ht.HistoricalSearchError) as caught:
            client.get('https://web.archive.org/cdx/search/cdx?url=x')
        self.assertEqual(caught.exception.code, 'provider_cooldown')
        self.assertEqual(caught.exception.retry_at, retry_at)
        self.assertIn(ht.clock_text(retry_at), str(caught.exception))
        self.assertIn('try again', str(caught.exception))

    def test_failed_archive_request_is_retried_in_the_background_and_cached(self):
        fixture = SharedStateEngineFixture('setUp')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        outage = {'on': True}
        client = fixtures.FakeClient()
        original = client.get

        def archive(url, headers=None):
            if outage['on'] and '/cdx/' in url:
                raise hs.HistoricalSearchError('Archive unavailable.', code='http_error', status=503,
                                               operational=True)
            return original(url, headers)

        client.get = archive
        engine = fixture.engine(client)
        created = []

        def factory():
            other = fixture.engine(client)
            other.provider.share_memory_caches(engine.provider)
            created.append(other)
            return other

        engine.provider.retrier = hs.BackgroundRetries(factory)
        with mock.patch.object(engine.provider.retrier, '_run'):
            with self.assertRaises(hs.HistoricalSearchError) as caught:
                engine.open(fixtures.SOURCE)
        self.assertIn('retries this archive request in the background; try again after', str(caught.exception))
        self.assertIsNotNone(caught.exception.retry_at)
        (task,) = engine.provider.retrier.tasks.values()
        self.assertEqual((task['kind'], task['value']), ('archive', fixtures.SOURCE))
        outage['on'] = False
        self.assertTrue(engine.provider.retrier._attempt(task))
        calls = len(client.calls)
        self.assertIn('Graph lemma', engine.open(fixtures.SOURCE)['result']['text'])
        self.assertEqual(len(client.calls), calls)  # Served from the cache the retry filled.
        self.assertTrue(created)


class FairPacingTests(unittest.TestCase):
    """I-054: pacing waits do not use up a call's budget, and waiters start in arrival order."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_pacing_waits_are_credited_up_to_the_hard_limit(self):
        clock = {'now': 0.0}
        url = 'https://web.archive.org/web/20200101000000id_/https://example.org/'

        class SlowSlot:
            @contextmanager
            def slot(self, host, deadline):
                self.deadline = deadline
                clock['now'] += 30.0  # Other jobs' requests ahead in line.
                yield

        for limit, expected in ((120.0, None), (None, 'deadline_exceeded')):
            with self.subTest(limit=limit):
                clock['now'] = 0.0
                coordinator = SlowSlot()
                client = ht.RestrictedHttpClient(coordinator=coordinator, retries=0, clock=lambda: clock['now'])
                with mock.patch.object(client, '_request', return_value=ht.HttpResponse(b'x', {}, url)):
                    if expected is None:
                        with client.operation(timeout_seconds=10, limit_seconds=limit):
                            self.assertEqual(client.get(url).body, b'x')
                        self.assertEqual(coordinator.deadline, 120.0)
                    else:
                        with self.assertRaises(ht.HistoricalSearchError) as caught:
                            with client.operation(timeout_seconds=10, limit_seconds=limit):
                                client.get(url)
                        self.assertEqual(caught.exception.code, expected)

    def test_waiting_moves_the_deadline_of_the_enclosing_call_too(self):
        clock = {'now': 0.0}
        call = ht.Budget(300.0, 540.0, clock=lambda: clock['now'])
        nested = ht.Budget(300.0, 540.0, clock=lambda: clock['now'], parent=call)
        with nested.waiting():
            clock['now'] = 100.0
            self.assertEqual((nested.current(), call.current()), (400.0, 400.0))  # While waiting.
        self.assertEqual((nested.current(), call.current()), (400.0, 400.0))  # Credited afterwards.
        with nested.waiting():
            clock['now'] = 700.0
        self.assertEqual((nested.current(), call.current(), call.hard()), (540.0, 540.0, 540.0))

    def test_waiting_requests_start_in_arrival_order(self):
        directory, solver = self.root / 'rates', self.root / 'solver'
        hr.SharedHostLimiter(directory, workspace_root=solver)
        holder_ready, release, order = threading.Event(), threading.Event(), []

        def holder():
            with hr.SharedHostLimiter(directory, workspace_root=solver).slot('api.crossref.org', time.monotonic() + 20):
                holder_ready.set()
                release.wait(10)

        def waiter(name):
            with hr.SharedHostLimiter(directory, workspace_root=solver).slot('api.crossref.org', time.monotonic() + 20):
                order.append(name)

        def tickets():
            return [path for path in directory.iterdir() if path.name.endswith('.ticket')]

        threads = [threading.Thread(target=holder, daemon=True)]
        threads[0].start()
        self.assertTrue(holder_ready.wait(5))
        for name in ('first', 'second', 'third'):
            before = len(tickets())
            threads.append(threading.Thread(target=waiter, args=(name,), daemon=True))
            threads[-1].start()
            deadline = time.monotonic() + 5
            while len(tickets()) <= before and time.monotonic() < deadline:
                time.sleep(0.01)
        release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order, ['first', 'second', 'third'])
        self.assertEqual(tickets(), [])

    def test_ticket_of_a_crashed_waiter_does_not_block_the_queue(self):
        directory = self.root / 'rates'
        limiter = hr.SharedHostLimiter(directory, workspace_root=self.root / 'solver')
        stale = directory / f'crossref-{0:020d}-1-deadbeef.ticket'
        stale.write_bytes(b'\0')  # Its owner died: nothing holds its lock.
        started = time.monotonic()
        with limiter.slot('api.crossref.org', started + 5):
            pass
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(stale.exists())



def pdf_with_forms(invocations):
    """One page whose content invokes the same small text form ``invocations`` times."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica')}))
    form = DecodedStreamObject()
    form.set_data(b'BT /F1 12 Tf 10 10 Td (Lemma) Tj ET')
    form.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                 NameObject('/BBox'): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(100)]),
                 NameObject('/Resources'): DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject(
        {NameObject('/Fm0'): writer._add_object(form)})})
    content = DecodedStreamObject()
    content.set_data(b'/Fm0 Do\n' * invocations)
    page[NameObject('/Contents')] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class ExplicitLimitTests(unittest.TestCase):
    """I-052: a document over a limit is named as such, never reported as a missing capture."""

    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine

    def test_oversized_capture_is_reported_and_its_answer_cached(self):
        client = fixtures.FakeClient()
        original, requests = client.get, []

        def replay(url, headers=None):
            requests.append(url)
            if '/web/' in url:
                raise hs.HistoricalSearchError('Historical retrieval response exceeds the size limit.',
                                               code='response_too_large')
            return original(url, headers)

        client.get = replay
        engine = self.engine(client)
        for _ in range(2):
            with self.assertRaises(hs.HistoricalSearchError) as caught:
                engine.open(fixtures.SOURCE)
            self.assertEqual(caught.exception.code, 'document_too_large')
            self.assertIn('4 MiB', str(caught.exception))
        self.assertEqual(len(requests), 2)  # Index and capture once; the repeat downloads nothing.

    def test_pdf_page_limit_and_form_limit_are_errors(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(251):
            writer.add_blank_page(72, 72)
        long_pdf = io.BytesIO()
        writer.write(long_pdf)
        for body in (long_pdf.getvalue(), pdf_with_forms(150)):
            with self.subTest(size=len(body)), self.assertRaises(hs.HistoricalSearchError) as caught:
                hs.extract_pdf(body)
            self.assertEqual(caught.exception.code, 'pdf_limit')
        self.assertIn('Lemma', hs.extract_pdf(pdf_with_forms(20))[1])

    def test_search_names_limited_candidates_and_omitted_results(self):
        other = 'https://example.org/second-paper'
        large = 'https://example.org/large-paper'
        client = fixtures.FakeClient(captures=[fixtures.snapshot(), fixtures.snapshot(url=other),
                                               fixtures.snapshot(url=large)],
                                     discoveries=[{'url': fixtures.SOURCE}, {'url': other}, {'url': large}])
        original = client.get

        def replay(url, headers=None):
            if '/web/' in url and url.endswith(large):
                raise hs.HistoricalSearchError('Too large.', code='response_too_large')
            return original(url, headers)

        client.get = replay
        result = self.engine(client, policy={'schema_version': 1, 'provider': 'brave_wayback',
                                             'max_results': 1}).search('graph lemma')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual((result['omitted_results'], result['limited_candidates']), (1, 1))
        self.assertIn('1 more verified result matched the query less closely', result['warning'])
        self.assertIn('1 candidate document exceeded the archive size or PDF extraction limits', result['warning'])
        self.assertNotIn('incomplete', result)


class PagedOpenTests(unittest.TestCase):
    """I-060: open() without a length returns one bounded page with its continuation."""

    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine

    BODY = ('<html><head><title>Long paper</title></head><body>'
            + ' '.join(f'word{index}' for index in range(1500)) + '</body></html>').encode()

    def paged_engine(self, **changes):
        capture = fixtures.snapshot(body=self.BODY)
        return self.engine(fixtures.FakeClient(captures=[capture]),
                           policy={'schema_version': 1, 'provider': 'brave_wayback', **changes})

    def test_default_page_is_bounded_and_says_how_to_continue(self):
        engine = self.paged_engine(open_default_chars=1000)
        page = engine.open(fixtures.SOURCE)['result']
        self.assertEqual(len(page['text']), 1000)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_offset'], 1000)
        self.assertGreater(page['total_chars'], 1000)
        rest = engine.open(fixtures.SOURCE, offset=page['next_offset'], length=page['total_chars'])['result']
        self.assertFalse(rest['has_more'])
        self.assertEqual(page['text'] + rest['text'], engine._document(fixtures.SOURCE, None)[1])

    def test_policies_without_a_page_keep_the_whole_document(self):
        page = self.paged_engine().open(fixtures.SOURCE)['result']
        self.assertFalse(page['has_more'])
        self.assertEqual(page['end_offset'], page['total_chars'])

    def test_tools_and_instructions_describe_the_page(self):
        description = next(tool for tool in hs.tool_definitions(30000) if tool['name'] == 'open')['description']
        self.assertIn('at most 30,000 characters', description)
        self.assertIn('next_offset', description)
        self.assertNotIn('full remaining document', description)
        self.assertIn('no policy character cap', next(tool for tool in hs.tool_definitions()
                                                      if tool['name'] == 'open')['description'])
        engine = self.paged_engine(open_default_chars=30000)
        server_ = hs.StdioMCPServer(engine)
        reply = server_.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertIn('at most 30,000 characters per call', reply['result']['instructions'])
        listed = server_.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        self.assertEqual(listed['result']['tools'], hs.tool_definitions(30000))

    def test_trajectory_audit_checks_paged_replies(self):
        content = 'x' * 2500
        page = content[:1000]
        doc = {'offset': 0, 'total_chars': 2500, 'document_text_sha256': hs.sha256(content.encode()),
               'offset_unit': 'unicode_code_points', 'has_more': True, 'truncated': True,
               'text': page, 'end_offset': 1000, 'next_offset': 1000, 'text_sha256': hs.sha256(page.encode()),
               'sha256': 'a' * 64}
        trajectory._check_navigation('open', {'url': fixtures.SOURCE}, doc, 20000, True, 1000)
        with self.assertRaises(ValueError):
            trajectory._check_navigation('open', {'url': fixtures.SOURCE}, doc, 20000, True)
        self.assertEqual(trajectory.BOUNDED_PAGE_NAVIGATION, retrieval.NAVIGATION_VERSION)


class PreparedPagingTests(UIRetrievalFixture):
    def test_new_policies_pin_the_paged_interface(self):
        policy = retrieval_runtime.load_policy(self.prepare())
        broker = json.loads(Path(policy['broker']['policyFile']).read_text())
        self.assertEqual(broker['open_default_chars'], retrieval.OPEN_DEFAULT_CHARS)
        self.assertEqual(policy['metadata']['navigationVersion'], 'bounded-page-v2')


class QueuedCallTests(unittest.TestCase):
    """I-053: a fifth concurrent call waits its turn instead of an immediate 'busy'."""

    def blocked(self, release, entered=None):
        def behavior(engine, value):
            if entered is not None:
                entered.put(value)
            release.wait(5)
            return {'results': [value]}
        return behavior

    def test_fifth_call_waits_and_is_answered(self):
        release, entered = threading.Event(), queue.Queue()
        harness = Harness(self, lambda: FakeEngine(self.blocked(release, entered)))
        harness.release_on_close(release)
        for number in range(5):
            harness.input.send(search(number, f'query {number}'))
        for _ in range(4):
            entered.get(timeout=2)
        with self.assertRaises(queue.Empty):
            harness.receive(timeout=0.2)  # No 'busy' reply.
        with harness.server._state_lock:
            self.assertEqual(list(harness.server._queued), [4])
        release.set()
        records = [harness.receive() for _ in range(5)]
        self.assertEqual({record['id'] for record in records}, {0, 1, 2, 3, 4})
        self.assertTrue(all(not record['result']['isError'] for record in records))
        self.assertEqual(entered.get(timeout=2), 'query 4')
        self.assertIn('further calls wait their turn', harness.initialization['result']['instructions'])

    def test_queued_call_is_answered_by_its_own_deadline(self):
        release = threading.Event()
        base = FakeEngine()
        base.operation_timeout = 0.2
        with mock.patch.object(hs.StdioMCPServer, 'RESPONSE_GRACE_SECONDS', 0.05):
            harness = Harness(self, lambda: FakeEngine(self.blocked(release)), base=base, max_active=1)
            harness.release_on_close(release)
            harness.input.send(search(1))
            time.sleep(0.05)
            started = time.monotonic()
            harness.input.send(search(2))
            replies = {}
            while len(replies) < 2:
                record = harness.receive(timeout=2)
                replies[record['id']] = (record, time.monotonic() - started)
        self.assertIn('deadline', replies[2][0]['result']['content'][0]['text'])
        self.assertLess(replies[2][1], 1.0)
        release.set()

    def test_cancelled_queued_call_never_runs(self):
        release, entered = threading.Event(), queue.Queue()
        harness = Harness(self, lambda: FakeEngine(self.blocked(release, entered)), max_active=1)
        harness.release_on_close(release)
        harness.input.send(search(1, 'first'))
        entered.get(timeout=2)
        harness.input.send(search(2, 'second'))
        harness.input.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 2}})
        harness.input.send(request(3, 'ping'))
        self.assertEqual(harness.receive()['id'], 3)
        release.set()
        self.assertEqual(harness.receive()['id'], 1)
        with self.assertRaises(queue.Empty):
            entered.get(timeout=0.3)

    def test_an_overfull_queue_answers_explicitly_and_names_the_tool(self):
        release, audit = threading.Event(), []
        base = FakeEngine(audit=type('Audit', (), {'write': lambda self, event, **fields: audit.append((event, fields))})())
        with mock.patch.object(hs.StdioMCPServer, 'MAX_QUEUED', 1):
            harness = Harness(self, lambda: FakeEngine(self.blocked(release)), base=base, max_active=1)
            harness.release_on_close(release)
            for number in range(3):
                harness.input.send(search(number))
            rejected = harness.receive()
        self.assertEqual(rejected['id'], 2)
        self.assertIn('calls waiting', rejected['result']['content'][0]['text'])
        busy = [fields for event, fields in audit if event == 'mcp_busy']
        self.assertEqual(busy[0]['tool'], 'search')
        release.set()

    def test_pacing_credit_moves_the_reply_deadline(self):
        engine_started = threading.Event()

        def behavior(engine, value):
            engine_started.set()
            with engine.request_budget.waiting():
                time.sleep(0.4)  # Longer than the 0.1 s budget: a pacing wait, credited.
            return {'results': ['after pacing']}

        base = FakeEngine()
        base.operation_timeout = 0.1
        with mock.patch.object(hs.StdioMCPServer, 'RESPONSE_GRACE_SECONDS', 0.05):
            harness = Harness(self, lambda: FakeEngine(behavior), base=base)
            harness.input.send(search(1))
            reply = harness.receive(timeout=3)
        self.assertFalse(reply['result']['isError'])
        self.assertTrue(engine_started.is_set())



class FakeDiscoveryError(RuntimeError):
    def __init__(self, code='discovery_unavailable'):
        super().__init__('SECRET CURRENT ANSWER')
        self.code = code


class DiscoveryFixture(unittest.TestCase):
    """A codex_wayback engine whose discovery module is a stand-in (no Codex process)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'solver'
        self.workspace.mkdir()
        self.audit_path = self.root / 'audit.jsonl'
        self.settings = {'codex_path': str(self.root / 'codex'), 'codex_sha256': 'a' * 64,
                         'auth_home': str(self.root / 'auth'), 'state_root': str(self.root / 'discovery'),
                         'model': 'pinned-model', 'timeout_seconds': 40}
        self.discover = mock.Mock(return_value=[fixtures.SOURCE])
        module = type(sys)('historical_codex_discovery')
        module.discover, module.validate_settings = self.discover, mock.Mock()
        module.preflight, module.DiscoveryError = mock.Mock(), FakeDiscoveryError
        patcher = mock.patch.dict(sys.modules, {'retrieval.historical_codex_discovery': module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, timeout=300):
        policy = {'schema_version': 1, 'provider': 'codex_wayback', 'solver_workspace': str(self.workspace),
                  'rate_limit_directory': str(self.root / 'state'), 'discovery': self.settings,
                  'max_candidates': 6, 'operation_timeout_seconds': timeout}
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy))
        return hs.HistoricalSearchEngine(path, fixtures.CUTOFF, self.audit_path, hs.sha256(path.read_bytes()),
                                         client=fixtures.FakeClient())


class DiscoveryRetryTests(DiscoveryFixture):
    """I-014: one retry inside the call, then background retries and a short-lived result cache."""

    def test_a_failed_discovery_is_tried_again_within_the_call(self):
        self.discover.side_effect = [FakeDiscoveryError(), [fixtures.SOURCE]]
        result = self.engine().search('graph lemma')
        self.assertEqual(result['results'][0]['url'], fixtures.SOURCE)
        self.assertEqual(self.discover.call_count, 2)

    def test_after_two_failures_the_broker_retries_and_says_when_to_search_again(self):
        self.discover.side_effect = FakeDiscoveryError('discovery_timeout')
        engine = self.engine()
        engine.provider.retrier = mock.Mock(schedule=mock.Mock(return_value=1700000460.0))
        with self.assertRaises(hs.HistoricalSearchError) as caught:
            engine.search('graph lemma')
        self.assertEqual(caught.exception.code, 'discovery_timeout')
        self.assertIn('retries this search in the background; repeat the same search after '
                      + ht.clock_text(1700000460.0), str(caught.exception))
        self.assertNotIn('SECRET', str(caught.exception))
        engine.provider.retrier.schedule.assert_called_once_with('discovery', 'graph lemma')
        self.assertEqual(self.discover.call_count, 2)

    def test_identical_searches_share_one_discovery_for_a_while(self):
        engine = self.engine()
        engine.search('graph lemma')
        engine.search('graph lemma')
        self.assertEqual(self.discover.call_count, 1)
        hits = [json.loads(line) for line in self.audit_path.read_text().splitlines()
                if json.loads(line)['event'] == 'codex_discovery_cache_hit']
        self.assertEqual(len(hits), 1)
        with mock.patch.object(hs.time, 'monotonic', return_value=time.monotonic() + hs.DISCOVERY_CACHE_SECONDS + 1):
            engine.provider.candidates('graph lemma')
        self.assertEqual(self.discover.call_count, 2)

    def test_a_background_retry_fills_the_cache_for_the_next_identical_search(self):
        self.discover.side_effect = FakeDiscoveryError()
        engine = self.engine()
        retrier = engine.provider.retrier = hs.BackgroundRetries(lambda: self.engine())

        def factory():
            other = self.engine()
            other.provider.share_memory_caches(engine.provider)
            return other

        retrier.engine_factory = factory
        with mock.patch.object(retrier, '_run'), self.assertRaises(hs.HistoricalSearchError):
            engine.search('graph lemma')
        (task,) = retrier.tasks.values()
        self.discover.side_effect = None
        self.assertTrue(retrier._attempt(task))
        calls = self.discover.call_count
        self.assertEqual(engine.search('graph lemma')['results'][0]['url'], fixtures.SOURCE)
        self.assertEqual(self.discover.call_count, calls)

    def test_background_tasks_are_bounded_and_expire(self):
        retrier = hs.BackgroundRetries(lambda: None)
        with mock.patch.object(threading.Thread, 'start'):
            for index in range(hs.BackgroundRetries.MAX_TASKS + 2):
                retrier.schedule('archive', f'https://example.org/{index}')
        self.assertEqual(len(retrier.tasks), hs.BackgroundRetries.MAX_TASKS)
        self.assertEqual(next(iter(retrier.tasks.values()))['value'], 'https://example.org/2')


class DiscoveryModelTests(DiscoveryFixture):
    """I-085: the model is chosen per call and recorded, with fallbacks."""

    def write_settings(self, model):
        (self.workspace / 'job-settings.json').write_text(json.dumps({'authorModel': model}))

    def models(self):
        return [call.kwargs['model'] for call in self.discover.call_args_list]

    def test_the_run_author_model_comes_first_then_pinned_then_fallbacks(self):
        self.write_settings('gpt-6-astra')
        self.engine().search('graph lemma')
        self.assertEqual(self.models(), ['gpt-6-astra'])
        for model in ('claude-opus-5-5', 'api:custom', 'bad model'):
            self.write_settings(model)
            self.engine().search('graph lemma')
            self.assertEqual(self.models()[-1], 'pinned-model')

    def test_an_unavailable_model_is_skipped_for_the_next_attempts(self):
        self.write_settings('gpt-6-astra')
        self.discover.side_effect = [FakeDiscoveryError('discovery_model_unavailable'), [fixtures.SOURCE]]
        engine = self.engine()
        engine.search('graph lemma')
        self.assertEqual(self.models(), ['gpt-6-astra', 'pinned-model'])
        events = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertIn({'model': 'gpt-6-astra'}, [{'model': event.get('model')} for event in events
                                                if event['event'] == 'codex_discovery_model_unavailable'])
        self.discover.side_effect = None
        engine.provider.candidates('another query')
        self.assertEqual(self.models()[-1], 'pinned-model')

    def test_fallbacks_follow_the_offered_codex_models(self):
        # The broker may import only its pinned sibling modules, so it keeps its own copy.
        from runner import providers
        offered = [row['id'] for row in providers.BUILTIN_MODELS if row['provider'] == providers.CODEX]
        self.assertEqual(list(hs.DISCOVERY_FALLBACK_MODELS), offered)
        self.assertEqual(hs.DISCOVERY_FALLBACK_MODELS[0], runtime.MODEL)


class DiscoveryTraceTests(unittest.TestCase):
    """I-085 and I-098: the real trace parser classifies model refusals and new notices."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def trace(self, *extra):
        events = [{'type': 'thread.started', 'thread_id': 't1'}, {'type': 'turn.started'}, *extra,
                  {'type': 'item.completed', 'item': {'id': 'w1', 'type': 'web_search', 'status': 'completed',
                                                      'action': {'type': 'search', 'query': 'q'},
                                                      'results': [{'url': fixtures.SOURCE}]}},
                  {'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message',
                                                      'text': json.dumps({'urls': [fixtures.SOURCE]})}},
                  {'type': 'turn.completed', 'usage': {}}]
        trace, final = self.root / 'events.jsonl', self.root / 'urls.json'
        trace.write_text(''.join(json.dumps(event) + '\n' for event in events))
        final.write_text(json.dumps({'urls': [fixtures.SOURCE]}))
        return trace, final

    def test_unknown_notices_are_tolerated_but_unknown_items_still_fail(self):
        self.assertEqual(discovery._trace(*self.trace({'type': 'turn.progress', 'percent': 50}), 3)[0],
                         [fixtures.SOURCE])
        for event in ({'type': 'item.created', 'item': {'type': 'command_execution'}},
                      {'type': 'turn.partially_failed'}, {'type': 'stream_error'}, {'type': 7}):
            with self.subTest(event=event), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(event), 3)

    def test_a_refused_model_is_named_as_unavailable(self):
        refusal = {'type': 'error', 'message': "unexpected status 400: The 'gpt-old' model is not supported "
                                               'when using Codex with a ChatGPT account.'}
        with self.assertRaises(discovery.DiscoveryError) as caught:
            discovery._trace(*self.trace(refusal), 3, model='gpt-old')
        self.assertEqual(caught.exception.code, 'discovery_model_unavailable')
        with self.assertRaises(discovery.DiscoveryError) as caught:
            discovery._trace(*self.trace({'type': 'error', 'message': 'stream disconnected'}), 3, model='gpt-old')
        self.assertEqual(caught.exception.code, 'discovery_unavailable')


class DiscoveryBinaryTests(unittest.TestCase):
    """I-097: a Codex update no longer breaks discovery or the rest of the run."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / 'releases' / 'old'
        self.release.mkdir(parents=True)
        self.binary = self.native(self.release / 'codex', b'old release')
        self.native(self.release / 'codex-code-mode-host', b'old host')
        platform = mock.patch.object(discovery.sys, 'platform', 'darwin')
        platform.start()
        self.addCleanup(platform.stop)
        self.controller = self.root / 'controller'
        self.controller.mkdir()
        self.settings = discovery.build_settings(self.binary, self.controller / 'discovery-auth',
                                                 self.controller / 'discovery-state', 'gpt-6.1-sol')

    @staticmethod
    def native(path, payload):
        path.write_bytes(b'\xcf\xfa\xed\xfe' + payload)
        path.chmod(0o755)
        return path

    def replace(self, path, payload):
        """An update writes a new file and renames it over the old one."""
        staged = path.with_name(path.name + '.new')
        self.native(staged, payload)
        staged.replace(path)

    def test_the_kept_link_serves_after_an_update_replaces_the_pinned_binary(self):
        kept = discovery.keep_binary(self.settings)
        self.assertEqual(kept, discovery.kept_binary(self.settings))
        self.assertTrue((kept.parent / 'codex-code-mode-host').is_file())
        self.replace(self.binary, b'new release')
        resolved = discovery.validate_settings(self.settings)
        self.assertEqual((resolved['codex_path'], resolved['codex_sha256']), (str(kept), self.settings['codex_sha256']))
        self.assertTrue(discovery.needs_repin(self.settings))

    def test_without_a_replacement_the_error_names_the_binary_and_hash(self):
        self.binary.unlink()
        with self.assertRaises(discovery.DiscoveryError) as caught:
            discovery.validate_settings(self.settings)
        self.assertEqual(caught.exception.code, 'discovery_binary')
        self.assertIn(str(self.binary), str(caught.exception))
        self.assertIn(self.settings['codex_sha256'][:16], str(caught.exception))

    def test_repin_runs_a_no_model_preflight_and_is_then_used(self):
        self.binary.unlink()
        installed = self.native(self.root / 'installed-codex', b'new release')
        with mock.patch.object(discovery, 'preflight') as preflight:
            record = discovery.repin(self.settings, installed)
        checked = preflight.call_args.args[0]
        self.assertEqual(checked['codex_sha256'], hashlib.sha256(installed.read_bytes()).hexdigest())
        self.assertEqual(record['pinnedSha256'], self.settings['codex_sha256'])
        resolved = discovery.validate_settings(self.settings)
        self.assertEqual(resolved['codex_sha256'], record['codex_sha256'])
        self.assertEqual(stat.S_IMODE((self.controller / discovery.REPIN_RECORD).stat().st_mode), 0o600)
        with mock.patch.object(discovery, 'preflight', side_effect=discovery.DiscoveryError()):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.repin({**self.settings, 'codex_sha256': 'b' * 64}, installed)


class PolicyBinaryTests(UIRetrievalFixture):
    """I-097 through the controller: other roles continue; the author's start re-pins, audited."""

    def setUp(self):
        super().setUp()
        platform = mock.patch.object(discovery.sys, 'platform', 'darwin')
        platform.start()
        self.addCleanup(platform.stop)

    def audit_events(self, policy):
        path = Path(policy['broker']['auditLog'])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_changed_binary_no_longer_fails_every_role_and_the_author_re_pins(self):
        policy_path = self.prepare()
        self.update_codex()  # In place: the kept link changes too, so nothing serves.
        policy = retrieval_runtime.load_policy(policy_path)
        self.assertIn('needs attention', policy['_discoveryProblem'])
        installed = self.root / 'installed-codex'
        installed.write_bytes(b'\xcf\xfa\xed\xfe' + b'newer native release')
        installed.chmod(0o755)
        with mock.patch.object(discovery, 'preflight') as preflight:
            retrieval_runtime._ensure_discovery_binary(policy, str(installed))
        preflight.assert_called_once()
        self.assertNotIn('_discoveryProblem', policy)
        repinned = [event for event in self.audit_events(policy) if event['event'] == 'codex_discovery_repinned']
        self.assertEqual(len(repinned), 1)
        self.assertEqual(repinned[0]['codex_sha256'], hashlib.sha256(installed.read_bytes()).hexdigest())
        self.assertNotIn('_discoveryProblem', retrieval_runtime.load_policy(policy_path))

    def test_a_failed_re_pin_without_fallback_needs_attention(self):
        policy_path = self.prepare()
        self.update_codex()
        policy = retrieval_runtime.load_policy(policy_path)
        with mock.patch.object(discovery, 'preflight', side_effect=discovery.DiscoveryError()):
            with self.assertRaises(retrieval_runtime.NeedsAttention) as caught:
                retrieval_runtime._ensure_discovery_binary(policy, str(self.codex))
        self.assertIn(json.loads(Path(policy['broker']['policyFile']).read_text())['discovery']['codex_sha256'][:16],
                      str(caught.exception))
        info = retrieval.public_retrieval_info(policy_path, run_dir=self.workspace)
        self.assertTrue(info['available'])
        self.assertIn('needs attention', info['attention'])



class IsolationCheckTests(RuntimePolicyFixture):
    """I-005: a check that could not finish is retried; only a proven violation stops."""

    def canary(self, report, exit_code=0, stdout=None):
        rpc = mock.Mock()
        rpc.call.return_value = {'exitCode': exit_code, 'stdout': json.dumps(report) if stdout is None else stdout}
        with mock.patch.object(retrieval_runtime.uuid, 'uuid4', return_value=mock.Mock(hex='nonce')), \
                mock.patch.object(retrieval_runtime, '_public_network_control'):
            retrieval_runtime._sandbox_canaries(rpc, self.policy, 'author')

    def test_canary_violations_stop_and_missing_evidence_is_retried(self):
        correct = {'nonce': 'nonce', 'insideOk': True, 'insideWrite': True, 'protectedWrite': False,
                   'settingsWrite': False, 'settingsDelete': False, 'settingsRename': False,
                   'settingsCanariesRead': True, 'outsideRead': False, 'network': False, 'publicNetwork': False,
                   'slashTmpRead': False, 'slashTmpWrite': False, 'varTmpRead': False, 'varTmpWrite': False}
        self.canary(correct)
        for key in ('outsideRead', 'network', 'publicNetwork', 'protectedWrite', 'settingsWrite', 'settingsDelete',
                    'settingsRename', 'slashTmpRead', 'slashTmpWrite', 'varTmpRead', 'varTmpWrite'):
            with self.subTest(violation=key), self.assertRaises(retrieval_runtime.RetrievalPolicyError) as caught:
                self.canary({**correct, key: True})
            self.assertNotIsInstance(caught.exception, retrieval_runtime.IsolationCheckUnavailable)
        for change in ({'insideOk': False}, {'insideWrite': False}, {'settingsCanariesRead': False},
                       {'nonce': 'other'}):
            with self.subTest(missing=change), self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                self.canary({**correct, **change})
        for options in ({'stdout': 'rosetta error'}, {'exit_code': 1}):
            with self.subTest(options=options), self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                self.canary(correct, **options)

    def test_a_violation_wins_over_connectivity_lost_during_the_probe(self):
        report = {'nonce': 'nonce', 'insideOk': True, 'insideWrite': True, 'protectedWrite': False,
                  'settingsWrite': False, 'settingsDelete': False, 'settingsRename': False,
                  'settingsCanariesRead': True, 'outsideRead': False, 'network': False, 'publicNetwork': True,
                  'slashTmpRead': False, 'slashTmpWrite': False, 'varTmpRead': False, 'varTmpWrite': False}
        rpc = mock.Mock()
        rpc.call.return_value = {'exitCode': 0, 'stdout': json.dumps(report)}
        offline = retrieval_runtime.IsolationCheckUnavailable('offline')
        with mock.patch.object(retrieval_runtime.uuid, 'uuid4', return_value=mock.Mock(hex='nonce')), \
                mock.patch.object(retrieval_runtime, '_public_network_control', side_effect=[None, offline]):
            with self.assertRaises(retrieval_runtime.RetrievalPolicyError) as caught:
                retrieval_runtime._sandbox_canaries(rpc, self.policy, 'author')
        self.assertNotIsInstance(caught.exception, retrieval_runtime.IsolationCheckUnavailable)
        with mock.patch.object(retrieval_runtime.uuid, 'uuid4', return_value=mock.Mock(hex='nonce')), \
                mock.patch.object(retrieval_runtime, '_public_network_control', side_effect=[None, offline]):
            rpc.call.return_value = {'exitCode': 0, 'stdout': json.dumps({**report, 'publicNetwork': False})}
            with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                retrieval_runtime._sandbox_canaries(rpc, self.policy, 'author')

    def test_offline_controller_and_slow_or_exited_app_server_are_retried(self):
        with mock.patch.object(retrieval_runtime.socket, 'create_connection', side_effect=OSError('unreachable')):
            with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                retrieval_runtime._public_network_control()
        # A line that is not a message is skipped (a launcher's own output); the exit after it is retried.
        for lines in ([None], ['not json\n', None], ['{"id": 1, "error": {"message": "busy"}}\n']):
            probe = object.__new__(retrieval_runtime._ProbeRPC)
            probe.process, probe.number, probe.messages = mock.Mock(stdin=io.StringIO()), 0, queue.Queue()
            for line in lines:
                probe.messages.put(line)
            with self.subTest(lines=lines), self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                probe.call('config/read', {})
        with mock.patch.object(retrieval_runtime, 'os') as fake_os:
            fake_os.environ, fake_os.pathsep = {'PATH': ''}, ':'
            with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                retrieval_runtime.codex_executable(self.policy, '')

    def test_broker_that_did_not_start_is_retried_but_extra_servers_stop(self):
        messages = self.rpc_messages()
        messages['mcpServerStatus/list']['data'][0] = {'name': retrieval_runtime.BROKER_NAME, 'tools': {},
                                                       'toolsError': 'process exited'}
        with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
            self.verify(messages)
        messages = self.rpc_messages()
        messages['mcpServerStatus/list']['data'].append({'name': 'browser', 'tools': {}})
        with self.assertRaises(retrieval_runtime.RetrievalPolicyError) as caught:
            self.verify(messages)
        self.assertNotIsInstance(caught.exception, retrieval_runtime.IsolationCheckUnavailable)

    def environment(self):
        # The author's private temp folder (retrieval_runtime.private_temp()), on macOS: one for this test.
        if getattr(self, 'temp', None) is None:
            self.temp = retrieval_runtime.private_temp(self.policy, 'author')
        return {'CODEX_HOME': str(retrieval_runtime.role_home(self.policy, 'author')), 'TMPDIR': str(self.temp)}

    def rpc_messages(self):
        retrieval_runtime.verify_protected_directories(self.policy, 'author', create=True)
        config = retrieval_runtime.configuration(self.policy, 'author')
        overrides = retrieval_runtime.temp_overrides(self.policy, self.environment())
        config['shell_environment_policy']['set'].update(overrides['shell_environment_policy']['set'])
        return {'config/read': {'config': config, 'layers': [{'name': {'type': 'sessionFlags'}, 'config': overrides}]},
                'mcpServerStatus/list': {'data': [{'name': retrieval_runtime.BROKER_NAME,
                                                   'tools': {name: {} for name in retrieval_runtime.BROKER_TOOLS}}]},
                'skills/list': {'data': [{'skills': [{'name': 'imagegen', 'enabled': False}], 'errors': []}]}}

    def verify(self, messages):
        rpc = mock.Mock()
        rpc.call.side_effect = lambda method, params: messages[method]
        with mock.patch.object(retrieval_runtime, '_sandbox_canaries'), \
                mock.patch.object(retrieval_runtime, '_macos_network_canaries'):
            retrieval_runtime.verify_rpc(rpc, self.policy, 'author', self.environment())


class RetriedPreflightTests(RetryCase):
    """I-005 through the runner: structured stages retry, the author retries in its thread."""

    def setUp(self):
        super().setUp()
        fixture = RuntimePolicyFixture('setUp')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.policy, self.workspace = fixture.policy, fixture.workspace
        for name, value in (('codex_executable', '/native/codex'),):
            patcher = mock.patch.object(retrieval_runtime, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module in (runtime,):
            patcher = mock.patch.object(module, 'retrieval_policy', return_value=self.policy)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_structured_stage_retries_a_check_that_could_not_finish(self):
        transient = retrieval_runtime.IsolationCheckUnavailable(
            'Cannot verify public network isolation: controller connectivity canary failed; checking again.')
        schema = {'type': 'object', 'properties': {'statement': {'type': 'string'}}, 'required': ['statement'],
                  'additionalProperties': False}
        with mock.patch.object(retrieval_runtime, 'preflight', side_effect=[transient, {'CODEX_HOME': '/isolated'}]) as check, \
                mock.patch.object(runtime, '_run_structured_process', return_value='{"statement": "S"}'):
            result, _ = runtime.structured('Review S.', schema, 'critic')
        self.assertEqual(result, {'statement': 'S'})
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.waits(), 60)

    def test_a_proven_violation_ends_the_structured_stage_at_once(self):
        with mock.patch.object(retrieval_runtime, 'preflight',
                               side_effect=retrieval_runtime.RetrievalPolicyError('Historical sandbox did not enforce '
                                                                                  'workspace I/O.')), \
                mock.patch.object(runtime, '_run_structured_process') as process:
            with self.assertRaises(runtime.ConfigurationError):
                runtime.structured('Review S.', {'type': 'object'}, 'critic')
        process.assert_not_called()
        self.assertEqual(self.waits(), 0)

    def author(self, failure):
        instance = Runtime([success()])
        with mock.patch.object(retrieval_runtime, 'preflight', return_value={'CODEX_HOME': '/isolated'}), \
                mock.patch.object(retrieval_runtime, 'verify_rpc', side_effect=[None, failure]), \
                mock.patch.object(runtime.subprocess, 'Popen', return_value=mock.Mock()):
            session = runtime.goal_session(instance, PROMPT, prompts=PROMPTS,
                                           settings={**SETTINGS, 'model': runtime.MODEL},
                                           options={'goal_cwd': str(self.workspace)})
            with self.assertRaises(runtime.WorkflowPaused) as paused:
                next(session)
        return paused.exception

    def test_author_turn_check_that_could_not_finish_continues_the_same_thread(self):
        paused = self.author(retrieval_runtime.IsolationCheckUnavailable('Historical isolation preflight timed '
                                                                         'out; checking again.'))
        self.assertFalse(paused.safety_stop)
        self.assertIsInstance(paused.retry, dict)
        self.assertEqual(paused.retry['thread'], 'thread-1')

    def test_a_violation_found_in_session_is_still_a_safety_stop(self):
        paused = self.author(retrieval_runtime.RetrievalPolicyError('Historical isolation found missing or '
                                                                    'unexpected live MCP servers.'))
        self.assertTrue(paused.safety_stop)
        self.assertIsNone(paused.retry)


class LoginSyncTests(RuntimePolicyFixture):
    """I-081: a renewed or refreshed login reaches every isolated role on its next start."""

    def setUp(self):
        super().setUp()
        self.user = self.root / 'user-codex'
        self.user.mkdir()
        environment = mock.patch.dict(os.environ, {retrieval_runtime.LOGIN_HOME_ENV: str(self.user)})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def login(path, token, refreshed):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({'tokens': {'access_token': token}, 'last_refresh': refreshed}))
        path.chmod(0o600)
        return path

    def home(self, role):
        return retrieval_runtime.role_home(self.policy, role) / 'auth.json'

    def test_newer_user_login_replaces_a_stale_isolated_copy_and_is_never_written(self):
        user = self.login(self.user / 'auth.json', 'renewed', '2026-10-20T00:00:00Z')
        self.login(self.home('author'), 'expired', '2026-10-01T00:00:00Z')
        before = user.read_bytes()
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(json.loads(self.home('author').read_text())['tokens']['access_token'], 'renewed')
        self.assertEqual(user.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.home('author').stat().st_mode), 0o600)

    def test_a_refresh_made_by_one_role_reaches_the_others_but_not_the_user(self):
        user = self.login(self.user / 'auth.json', 'user', '2026-10-01T00:00:00Z')
        self.login(self.home('author'), 'author-refreshed', '2026-10-22T00:00:00Z')
        self.login(self.home('structured'), 'old', '2026-10-01T00:00:00Z')
        retrieval_runtime.isolated_environment(self.policy, 'structured', {})
        self.assertEqual(json.loads(self.home('structured').read_text())['tokens']['access_token'],
                         'author-refreshed')
        self.assertEqual(json.loads(user.read_text())['tokens']['access_token'], 'user')

    def test_invalid_or_linked_logins_are_never_copied_or_followed(self):
        self.login(self.home('author'), 'author', '2026-10-01T00:00:00Z')
        (self.user / 'auth.json').write_text('{"tokens": {}}')
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(json.loads(self.home('author').read_text())['tokens']['access_token'], 'author')
        self.login(self.user / 'auth.json', 'renewed', '2026-10-20T00:00:00Z')
        structured = self.home('structured')
        structured.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / 'outside.json'
        outside.write_text('outside')
        structured.symlink_to(outside)
        retrieval_runtime.sync_login(self.policy, [structured.parent])
        self.assertEqual(outside.read_text(), 'outside')

    def test_critic_snapshot_gets_the_newest_login(self):
        self.login(self.user / 'auth.json', 'renewed', '2026-10-20T00:00:00Z')
        self.login(self.home('structured'), 'expired', '2026-10-01T00:00:00Z')
        with retrieval_runtime.critic_session(self.policy) as critic:
            copied = retrieval_runtime.role_home(critic, 'critic') / 'auth.json'
            self.assertEqual(json.loads(copied.read_text())['tokens']['access_token'], 'renewed')


class ConfigMigrationTests(RuntimePolicyFixture):
    """I-098: audited migration of controller-generated keys; anything else needs attention."""

    def saved(self, change):
        config = retrieval_runtime.role_home(self.policy, 'author') / 'config.toml'
        if not config.exists():
            retrieval_runtime.isolated_environment(self.policy, 'author', {})
        value = tomllib.loads(retrieval_runtime.config_text(self.policy, 'author'))
        change(value)
        config.write_text('\n'.join(retrieval_runtime._toml_table(value)) + '\n')
        return config

    def audit(self):
        path = Path(self.policy['broker']['auditLog'])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_an_update_that_adds_or_retires_settings_is_migrated_with_an_audit_entry(self):
        for change in (lambda value: value['features'].pop('image_generation'),
                       lambda value: value['features'].update(retired_flag=False),
                       lambda value: value['mcp_servers'][retrieval_runtime.BROKER_NAME].update(tool_timeout_sec=300)):
            with self.subTest(change=change):
                config = self.saved(change)
                retrieval_runtime.isolated_environment(self.policy, 'author', {})
                self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        migrated = [event for event in self.audit() if event['event'] == 'isolated_config_migrated']
        self.assertEqual(len(migrated), 3)
        self.assertEqual(migrated[0]['changed_keys'], ['features.image_generation'])
        self.assertEqual(migrated[0]['source'], 'controller')

    def test_a_config_saved_before_the_tmp_denials_is_migrated_and_a_widened_one_needs_attention(self):
        def without_denials(value):
            for key in retrieval_runtime.SHARED_TEMP_DENIED:
                value['permissions']['historical']['filesystem'].pop(key)

        config = self.saved(without_denials)
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        filesystem = tomllib.loads(config.read_text())['permissions']['historical']['filesystem']
        self.assertEqual((filesystem[':slash_tmp'], filesystem['/private/var/tmp']), ('deny', 'deny'))
        [migrated] = [event for event in self.audit() if event['event'] == 'isolated_config_migrated']
        self.assertEqual(sorted(migrated['changed_keys']), ['permissions.historical.filesystem./private/var/tmp',
                                                            'permissions.historical.filesystem.:slash_tmp'])
        config = self.saved(lambda value: value['permissions']['historical']['filesystem'].update({':slash_tmp': 'write'}))
        before = config.read_bytes()
        with self.assertRaises(retrieval_runtime.NeedsAttention):
            retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(config.read_bytes(), before)

    def test_a_config_saved_before_the_private_temp_folders_is_migrated_with_a_new_path(self):
        os.environ['PATH'] = '/controller/earlier/bin'
        config = self.saved(lambda value: value['permissions']['historical']['filesystem'].pop(':tmpdir'))
        # The controller restarted with another PATH since it wrote the config: both are migrated at once.
        os.environ['PATH'] = '/controller/restarted/bin'
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        saved = tomllib.loads(config.read_text())
        self.assertEqual(saved['permissions']['historical']['filesystem'][':tmpdir'], 'write')
        self.assertEqual(saved['shell_environment_policy'], {'inherit': 'none', 'set': {'PATH': '/controller/restarted/bin'}})
        [migrated] = [event for event in self.audit() if event['event'] == 'isolated_config_migrated']
        self.assertEqual(sorted(migrated['changed_keys']), ['permissions.historical.filesystem.:tmpdir',
                                                            'shell_environment_policy.set.PATH'])
        # A widened grant, or a shell environment that is not the controller's, still needs attention.
        for change in (lambda value: value['permissions']['historical']['filesystem'].update({':tmpdir': 'read',
                                                                                              '/Users': 'write'}),
                       lambda value: value['permissions']['historical']['filesystem'].update({':root': 'read'}),
                       lambda value: value['shell_environment_policy']['set'].update(TMPDIR='/private/tmp'),
                       lambda value: value['shell_environment_policy'].update(inherit='all')):
            with self.subTest(change=change):
                config = self.saved(lambda value: (value['permissions']['historical']['filesystem'].pop(':tmpdir'),
                                                   change(value)))
                before = config.read_bytes()
                with self.assertRaises(retrieval_runtime.NeedsAttention):
                    retrieval_runtime.isolated_environment(self.policy, 'author', {})
                self.assertEqual(config.read_bytes(), before)

    def test_codex_own_trust_record_does_not_hold_the_migration(self):
        """Codex writes [projects."<run>"] trust_level = "trusted" at an author's thread/start; it goes as at every start."""
        trust = {str(self.workspace): {'trust_level': 'trusted'}}

        def older(*keys, projects=trust):
            def change(value):
                for key in keys:
                    value['permissions']['historical']['filesystem'].pop(key)
                value['projects'] = projects
            return change

        # A run whose author started a thread before the private temp folders, under another PATH.
        os.environ['PATH'] = '/controller/earlier/bin'
        config = self.saved(older(':tmpdir'))
        os.environ['PATH'] = '/controller/restarted/bin'
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        self.assertNotIn('projects', tomllib.loads(config.read_text()))
        [migrated] = [event for event in self.audit() if event['event'] == 'isolated_config_migrated']
        self.assertEqual(sorted(migrated['changed_keys']), ['permissions.historical.filesystem.:tmpdir', 'projects',
                                                            'shell_environment_policy.set.PATH'])
        # One started before the /tmp denials too (a run started on main).
        config = self.saved(older(*retrieval_runtime.SHARED_TEMP_DENIED, ':tmpdir'))
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        self.assertEqual(len([event for event in self.audit() if event['event'] == 'isolated_config_migrated']), 2)
        # Any other [projects] table is not Codex's record of this run: it still needs attention.
        for projects in ({str(self.root): {'trust_level': 'trusted'}},
                         {str(self.workspace): {'trust_level': 'untrusted'}},
                         {**trust, str(self.root): {'trust_level': 'trusted'}},
                         {str(self.workspace): {'trust_level': 'trusted', 'extra': True}}):
            with self.subTest(projects=projects):
                config = self.saved(older(':tmpdir', projects=projects))
                before = config.read_bytes()
                with self.assertRaisesRegex(retrieval_runtime.NeedsAttention, 'projects'):
                    retrieval_runtime.isolated_environment(self.policy, 'author', {})
                self.assertEqual(config.read_bytes(), before)

    def test_settings_tcs_prover_never_writes_need_attention(self):
        for change in (lambda value: value.update(notify=['/bin/sh']),
                       lambda value: value['features'].update(view_image=True),
                       lambda value: value['permissions']['historical']['network'].update(enabled=True)):
            with self.subTest(change=change):
                config = self.saved(change)
                before = config.read_bytes()
                with self.assertRaises(retrieval_runtime.NeedsAttention) as caught:
                    retrieval_runtime.isolated_environment(self.policy, 'author', {})
                self.assertIn('configuration has changed', str(caught.exception))
                self.assertEqual(config.read_bytes(), before)
        self.assertFalse([event for event in self.audit() if event['event'] == 'isolated_config_migrated'])

    def test_a_newly_bundled_skill_is_recorded_and_turned_off_from_the_next_start(self):
        checks = IsolationCheckTests('rpc_messages')
        checks.policy = self.policy
        messages = checks.rpc_messages()
        new_skill = str(retrieval_runtime.role_home(self.policy, 'author') / 'skills/.system/new-helper/SKILL.md')
        messages['skills/list']['data'][0]['skills'].append({'name': 'new-helper', 'path': new_skill, 'enabled': True})
        with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable) as caught:
            checks.verify(messages)
        self.assertIn('new-helper', str(caught.exception))
        self.assertIn('new-helper', retrieval_runtime.bundled_skills(self.policy))
        expected = retrieval_runtime.configuration(self.policy, 'author')['skills']['config']
        self.assertIn({'path': new_skill, 'enabled': False}, expected)
        # The running process loaded the old list: its own checks still pass.
        messages['skills/list']['data'][0]['skills'][-1]['enabled'] = False
        checks.verify(messages)
        # The next start writes the longer list (a controller-generated key).
        config = self.saved(lambda value: None)
        old = tomllib.loads(config.read_text())
        old['skills']['config'] = [entry for entry in old['skills']['config'] if 'new-helper' not in entry['path']]
        config.write_text('\n'.join(retrieval_runtime._toml_table(old)) + '\n')
        retrieval_runtime.isolated_environment(self.policy, 'author', {})
        self.assertIn(new_skill, config.read_text())
        # An enabled skill outside the bundled folder is still a violation.
        messages['skills/list']['data'][0]['skills'].append({'name': 'project', 'path': str(self.workspace / 'SKILL.md'),
                                                            'enabled': True})
        with self.assertRaises(retrieval_runtime.RetrievalPolicyError) as caught:
            checks.verify(messages)
        self.assertNotIsInstance(caught.exception, retrieval_runtime.IsolationCheckUnavailable)



class DashboardTotalsTests(UIRetrievalFixture):
    """I-133: totals survive a large log and skip foreign or malformed records."""

    def write(self, policy_path, records, mode='w'):
        audit = Path(json.loads(Path(policy_path).read_text())['broker']['auditLog'])
        with audit.open(mode, encoding='utf-8') as stream:
            for record in records:
                stream.write(record if isinstance(record, str) else json.dumps(
                    {'cutoff': UI_CUTOFF, 'time': '2026-10-16T10:00:00Z', **record}) + '\n')

    @staticmethod
    def operation(name):
        return [{'event': 'mcp_accepted', 'operation_id': name}, {'event': 'search_requested', 'operation_id': name},
                {'event': 'mcp_response_ready', 'operation_id': name, 'is_error': False}]

    def test_a_large_log_keeps_its_totals_and_new_lines_are_folded_incrementally(self):
        policy_path = self.prepare()
        self.write(policy_path, [record for index in range(40) for record in self.operation(f'op{index}')])
        with mock.patch.object(retrieval, 'MAX_AUDIT_READ_BYTES', 2048):
            info = self.info(policy_path)
            self.assertTrue(info['available'])
            self.assertTrue(info['catchingUp'])
            while info['catchingUp']:
                info = self.info(policy_path)
        self.assertEqual((info['counts']['searches'], info['counts']['successes'], info['counts']['pending']),
                         (40, 40, 0))
        self.write(policy_path, self.operation('late'), mode='a')
        with mock.patch.object(retrieval, '_audit_fingerprint', wraps=retrieval._audit_fingerprint) as folds:
            info = self.info(policy_path)
        self.assertEqual(info['counts']['searches'], 41)
        self.assertLessEqual(folds.call_count, 2)  # The saved offset is reused; nothing is reread.

    def test_foreign_and_malformed_records_are_counted_and_skipped(self):
        policy_path = self.prepare()
        self.write(policy_path, [*self.operation('good'), '{"broken": \n',
                                 json.dumps({'event': 'search_requested', 'cutoff': '2020-01-01T00:00:00Z'}) + '\n'])
        info = self.info(policy_path)
        self.assertTrue(info['available'])
        self.assertEqual(info['skippedRecords'], 2)
        self.assertEqual(info['counts']['searches'], 1)

    def test_a_rewritten_log_is_folded_again(self):
        policy_path = self.prepare()
        self.write(policy_path, self.operation('a') + self.operation('b'))
        self.assertEqual(self.info(policy_path)['counts']['searches'], 2)
        self.write(policy_path, self.operation('c'))
        self.assertEqual(self.info(policy_path)['counts']['searches'], 1)


class PreflightCauseTests(UIRetrievalFixture):
    """I-133 and I-026: a failed check names its real cause, on the console too."""

    def setUp(self):
        super().setUp()
        self.login = self.root / 'login'
        self.login.mkdir()
        (self.login / 'auth.json').write_text('{"tokens": {"access_token": "test"}}')

    def preflight(self, **patches):
        stderr = io.StringIO()
        policy_path = self.prepare()
        with mock.patch.object(sys, 'stderr', stderr), \
                mock.patch.object(retrieval.subprocess, 'run', **patches.get('run', {'return_value': mock.Mock(
                    stdout=json.dumps({'status': 'ready', 'provider': 'codex_wayback',
                                       'discovery_preflight': 'ready'}))})), \
                mock.patch.object(retrieval_runtime, 'preflight', **patches.get('probe', {})), \
                self.assertRaises(ValueError) as caught:
            retrieval.preflight_policy(policy_path, auth_home=self.login)
        return caught.exception, stderr.getvalue()

    def test_connectivity_failure_is_named_and_retried(self):
        failure, log = self.preflight(probe={'side_effect': retrieval_runtime.IsolationCheckUnavailable(
            'Cannot verify public network isolation: controller connectivity canary failed; checking again.')})
        self.assertIsInstance(failure, retrieval.PreflightRetry)
        self.assertIn('controller connectivity canary failed', str(failure))
        self.assertIn('controller connectivity canary failed', log)

    def test_broker_exit_names_its_own_message(self):
        error = subprocess.CalledProcessError(2, ['broker'], stderr='Historical search unavailable: Shared '
                                                                    'historical request coordination is invalid.\n')
        failure, _ = self.preflight(run={'side_effect': error})
        self.assertIn('Shared historical request coordination is invalid', str(failure))
        failure, _ = self.preflight(run={'side_effect': subprocess.TimeoutExpired(['broker'], 30)})
        self.assertIsInstance(failure, retrieval.PreflightRetry)
        self.assertIn('did not finish within 30 seconds', str(failure))

    def test_a_violation_is_named_and_not_retried(self):
        failure, _ = self.preflight(probe={'side_effect': retrieval_runtime.RetrievalPolicyError(
            'Historical sandbox did not enforce workspace I/O, outside-read denial and TCP denial.')})
        self.assertNotIsInstance(failure, retrieval.PreflightRetry)
        self.assertIn('outside-read denial', str(failure))

    def test_compiler_failure_names_its_cause(self):
        from retrieval import isolated_compiler
        policy_path = self.prepare()
        with mock.patch.object(isolated_compiler, 'supported', return_value=True), \
                mock.patch.object(isolated_compiler, 'preflight', side_effect=isolated_compiler.CompilerIsolationError(
                    'Compiler sandbox canaries failed (exit -6): rosetta error')), \
                mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(ValueError) as caught:
            retrieval.preflight_compiler(policy_path)
        self.assertIn('rosetta error', str(caught.exception))


class RelocationTests(UIRetrievalFixture):
    """I-084: a moved bundle is adopted (audited); a missing one needs attention."""

    def move(self):
        policy_path = Path(self.prepare())
        old_root = policy_path.parent
        (self.workspace / 'job-settings.json').write_text(json.dumps({'retrievalPolicyPath': str(policy_path)}))
        retrieval_runtime.isolated_environment(retrieval_runtime.load_policy(policy_path), 'author', {})
        new_parent = self.root / 'moved-checkout' / 'ui'
        new_parent.mkdir(parents=True)
        old_root.rename(new_parent / old_root.name)
        return policy_path, new_parent

    def test_a_moved_controller_folder_is_adopted_with_an_audit_entry(self):
        policy_path, new_parent = self.move()
        adopted = Path(retrieval.check_run(policy_path, self.workspace, controller_root=new_parent))
        self.assertEqual(adopted, new_parent / policy_path.parent.name / 'retrieval-policy.json')
        policy = retrieval_runtime.load_policy(adopted)
        self.assertEqual(policy['cutoffExclusiveUtc'], UI_CUTOFF)
        self.assertTrue(Path(policy['broker']['policyFile']).is_relative_to(new_parent))
        events = [json.loads(line) for line in Path(policy['broker']['auditLog']).read_text().splitlines()]
        moved = [event for event in events if event['event'] == 'policy_relocated']
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]['previous_controller'], str(policy_path.parent))
        config = retrieval_runtime.role_home(policy, 'author') / 'config.toml'
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(policy, 'author'))
        self.assertNotIn(str(policy_path.parent), config.read_text())
        # A second check finds nothing to change.
        self.assertEqual(retrieval.check_run(adopted, self.workspace, controller_root=new_parent), str(adopted))

    def test_a_virtualenv_recreated_elsewhere_is_adopted(self):
        policy_path = Path(self.prepare())
        value = json.loads(policy_path.read_text())
        value['broker']['command'] = str(self.root / 'old-venv' / 'bin' / 'python')
        policy_path.write_text(json.dumps(value))
        with self.assertRaises(retrieval_runtime.NeedsAttention):
            retrieval_runtime.load_policy(policy_path)
        adopted = retrieval.check_run(policy_path, self.workspace, controller_root=self.root / 'controller')
        self.assertEqual(adopted, str(policy_path))
        self.assertEqual(retrieval_runtime.load_policy(adopted)['broker']['command'], str(Path(sys.executable).absolute()))

    def test_a_missing_or_foreign_bundle_needs_attention(self):
        policy_path, new_parent = self.move()
        with self.assertRaisesRegex(retrieval.NeedsAttention, 'controller folder was not found'):
            retrieval.check_run(policy_path, self.workspace, controller_root=self.root / 'empty')
        (self.workspace / 'input.md').write_text('A different statement.\n')
        with self.assertRaisesRegex(retrieval.NeedsAttention, 'does not belong to this run'):
            retrieval.check_run(policy_path, self.workspace, controller_root=new_parent)


class HistoricalJobFixture(unittest.TestCase):
    """A historical job in a socketless server; every process start is recorded, none runs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / 'runs'
        self.codex = self.root / 'codex'
        self.codex.write_bytes(b'\xcf\xfa\xed\xfe' + b'unit-test-native-binary')
        self.codex.chmod(0o700)
        self.manager = object.__new__(server.Server)
        self.manager.runs, self.manager.jobs, self.manager.jobs_lock = self.runs, {}, threading.RLock()
        self.launches, self.checks = [], []
        real_prepare = retrieval.prepare_policy
        self.preflight = mock.Mock(side_effect=lambda path: self.checks.append(path))

        def prepare(workspace, cutoff):
            return real_prepare(workspace, cutoff, controller_root=self.root / 'controller')

        changes = [mock.patch.object(retrieval_runtime, 'codex_executable', return_value=str(self.codex)),
                   mock.patch.object(server.runtime, 'verify_model_credentials'),
                   mock.patch.object(server, 'grant_windows_access'),
                   mock.patch.object(retrieval, 'capabilities', return_value={'historicalAvailable': True}),
                   mock.patch.object(retrieval, 'prepare_policy', side_effect=prepare),
                   mock.patch.object(retrieval, 'preflight_policy', self.preflight),
                   mock.patch.object(retrieval, 'preflight_compiler'),
                   mock.patch.object(server.App, '_spawn_worker'),
                   mock.patch.object(server.subprocess, 'Popen', side_effect=self.launch),
                   mock.patch.object(sys, 'stderr', io.StringIO())]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        retrieval._CACHE.clear()

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        return mock.Mock(stdin=io.StringIO(), poll=mock.Mock(return_value=None), pid=1)

    def start(self):
        return self.manager.start_direct_job({'statement': 'Prove a harmless identity.',
                                              'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF})

    def settle(self, app, **state):
        """End the launched run as the reader would, then apply ``state``."""
        app.process = app.active_token = app.worker_token = None
        app.state.update(**state)
        app._save_job_settings(app.state)


class HeldLaunchTests(HistoricalJobFixture):
    """I-026: a failed launch check holds the job with its cause and is retried every minute."""

    def test_a_check_that_could_not_finish_holds_the_job_and_is_retried(self):
        self.preflight.side_effect = retrieval.PreflightRetry(
            'Historical search preflight failed; no model was started: Cannot verify public network isolation.')
        app = self.start()
        self.assertEqual(app.state['phase'], 'prepared')
        self.assertTrue(app.state['launchHold'].startswith('Historical search preflight failed'))
        self.assertTrue(app.state['launchHold'].endswith(server.HISTORICAL_RETRY_SUFFIX))
        self.assertEqual(self.launches, [])
        self.assertAlmostEqual(app._hold_retry_at, time.time() + server.AUTO_RESUME_SECONDS, delta=5)
        plan = self.manager._stopped_continuation_plan(app)
        self.assertEqual((plan['action'], plan['label']), ('preflight', 'Re-run preflight'))
        self.assertFalse(app.retry_historical_hold_if_due(time.time()))  # Not due yet.
        self.preflight.side_effect = None

        class Inline:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(server.threading, 'Thread', Inline):
            self.assertTrue(app.retry_historical_hold_if_due(time.time() + server.AUTO_RESUME_SECONDS + 1))
        self.assertEqual(app.state['launchHold'], '')
        self.assertEqual(app.state['phase'], 'running')
        self.assertEqual(len(self.launches), 1)

    def test_a_permanent_failure_waits_for_re_run_preflight(self):
        self.preflight.side_effect = ValueError('Codex login is unavailable. Run codex login before starting '
                                                'historical search.')
        app = self.start()
        self.assertIn('Codex login is unavailable', app.state['launchHold'])
        self.assertFalse(app._hold_retry_at)
        self.preflight.side_effect = None
        continued = self.manager.continue_stopped_job(app.state['runId'])
        self.assertIs(continued, app)
        self.assertEqual(app.state['phase'], 'running')
        self.assertEqual(app.state['launchHold'], '')

    def test_batch_holds_name_the_cause_and_check_again(self):
        self.preflight.side_effect = retrieval.PreflightRetry(
            'Historical search preflight failed; no model was started: the check did not finish within 30 seconds.')
        with self.assertRaises(ValueError) as caught:
            self.manager.start_direct_batch({'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF,
                                             'statements': [{'statement': 'A.'}, {'statement': 'B.'}]})
        self.assertIn('which run again every minute', str(caught.exception))
        for app in self.manager.jobs.values():
            self.assertIn('did not finish within 30 seconds', app.state['launchHold'])
            self.assertTrue(app._hold_retry_at)

    def test_a_pending_hold_found_at_restart_is_checked_again_at_once(self):
        apps = []
        self.preflight.side_effect = lambda path: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):  # The server stopped during the batch preflight.
            self.manager.start_direct_batch({'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF,
                                             'statements': [{'statement': 'A.'}]})
        apps = list(self.manager.jobs.values())
        self.assertEqual(apps[0].state['launchHold'], server.HISTORICAL_PENDING_HOLD)
        restored = server.restore_saved_app(server.App(apps[0].run_dir / 'transcript.jsonl', self.runs))
        self.assertEqual(restored.state['launchHold'], server.HISTORICAL_PENDING_HOLD)
        self.assertLessEqual(restored._hold_retry_at, time.time())


class InPlaceContinuationTests(HistoricalJobFixture):
    """I-070: stopped or failed historical runs continue in their own folder; holds stop retries."""

    def test_stopped_author_continues_in_the_same_folder_with_the_same_policy(self):
        app = self.start()
        policy = app.state['retrievalPolicyPath']
        self.settle(app, phase='done', stage='solve', activeNode='author', manuallyStopped=True, stoppedStage='solve',
                    error='Stopped.', goalThreadId='thread-1', elapsedSeconds=7200)
        row = self.manager.job_list()[0]
        self.assertTrue(row['canContinueStopped'] and row['continueStoppedInPlace'])
        self.assertEqual(row['continueStoppedLabel'], 'Continue proof author')
        continued = self.manager.continue_stopped_job(app.state['runId'])
        self.assertIs(continued, app)
        argv, options = self.launches[-1]
        self.assertEqual(Path(options['cwd']), app.run_dir)
        self.assertEqual(options['env'][retrieval_runtime.POLICY_ENV], policy)
        self.assertIn('goal_thread_id="thread-1"', argv)
        self.assertFalse(app.state['manuallyStopped'])
        self.assertEqual(app.state['elapsedSeconds'], 0)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_critic_and_final_stages_continue_in_place(self):
        app = self.start()
        app._save(runtime.SAVED_CANDIDATE_FILENAME, 'A complete candidate proof.\n')
        self.settle(app, phase='done', stage='critic', activeNode='critic', error='Workflow failed.')
        self.assertEqual(self.manager._stopped_continuation_plan(app)['action'], 'critic')
        self.manager.continue_stopped_job(app.state['runId'])
        argv, options = self.launches[-1]
        self.assertEqual(Path(options['cwd']), app.run_dir)
        self.assertEqual(argv[argv.index('--start-node') + 1], 'critic')
        runtime.save_final_input('The statement.', 'A complete candidate proof.', directory=app.run_dir)
        self.settle(app, phase='done', stage='final', activeNode='latex_editor', finalInputReady=True,
                    error='Compile failed.')
        self.assertEqual(self.manager._stopped_continuation_plan(app)['label'], 'Retry LaTeX editor')
        self.manager.continue_stopped_job(app.state['runId'])
        argv, options = self.launches[-1]
        self.assertTrue(any(str(value).endswith('clean_up.yaml') for value in argv))
        self.assertEqual(Path(options['cwd']), app.run_dir)

    def test_resume_at_critic_continues_in_place_instead_of_a_new_run(self):
        app = self.start()
        app._save(runtime.SAVED_CANDIDATE_FILENAME, 'A complete candidate proof.\n')
        self.settle(app, phase='done', stage='solve', activeNode='author', manuallyStopped=True, stoppedStage='solve',
                    error='Stopped.')
        self.assertTrue(self.manager.job_list()[0]['canResumeCritic'])
        self.assertIs(self.manager.resume_critic_job(app.state['runId']), app)
        argv, options = self.launches[-1]
        self.assertEqual(argv[argv.index('--start-node') + 1], 'critic')
        self.assertEqual(Path(options['cwd']), app.run_dir)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_a_paused_run_whose_conversation_is_gone_starts_a_new_one_in_place(self):
        app = self.start()
        self.settle(app, phase='paused', stage='repair', activeNode='author', sessionUnavailable=True,
                    goalThreadId='gone-thread')
        plan = self.manager._stopped_continuation_plan(app)
        self.assertEqual(plan['action'], 'author')
        self.assertIn('new author conversation', plan['description'])
        self.manager.continue_stopped_job(app.state['runId'])
        argv, _ = self.launches[-1]
        self.assertFalse(any('gone-thread' in str(value) for value in argv))

    def test_a_held_paused_job_waits_without_retrying_every_minute(self):
        app = self.start()
        self.settle(app, phase='paused', stage='solve', activeNode='author',
                    launchHold='Needs attention: The historical retrieval policy is missing. Choose Re-run preflight '
                               'after fixing it.')
        checkpoint = {'pauseCause': 'error', 'error': 'Cannot read historical retrieval policy.'}
        app._configure_auto_resume(checkpoint, time.time())
        self.assertEqual(checkpoint['autoResumeAt'], 0)
        self.assertEqual(self.manager._stopped_continuation_plan(app)['action'], 'preflight')

    def test_a_moved_bundle_is_adopted_and_saved_before_the_run_continues(self):
        app = self.start()
        old = Path(app.state['retrievalPolicyPath'])
        self.settle(app, phase='done', stage='solve', activeNode='author', manuallyStopped=True, stoppedStage='solve',
                    error='Stopped.')
        new_parent = self.root / 'moved' / 'ui'
        new_parent.mkdir(parents=True)
        old.parent.rename(new_parent / old.parent.name)
        real_check = retrieval.check_run
        with mock.patch.object(retrieval, 'check_run', side_effect=lambda path, run_dir: real_check(
                path, run_dir, controller_root=new_parent)):
            self.manager.continue_stopped_job(app.state['runId'])
        moved = new_parent / old.parent.name / 'retrieval-policy.json'
        self.assertEqual(app.state['retrievalPolicyPath'], str(moved))
        saved = json.loads((app.run_dir / 'job-settings.json').read_text())
        self.assertEqual(saved['retrievalPolicyPath'], str(moved))
        self.assertEqual(self.launches[-1][1]['env'][retrieval_runtime.POLICY_ENV], str(moved))
        self.assertTrue(any(record.get('label') == 'Historical policy moved' for record in app.state['trace']))

    def test_a_missing_policy_found_before_resume_holds_the_job(self):
        app = self.start()
        Path(app.state['retrievalPolicyPath']).unlink()
        with self.assertRaisesRegex(ValueError, 'Needs attention'):
            with app.lock:
                app._check_historical_locked()
        self.assertTrue(app.state['launchHold'].startswith('Needs attention:'))
        self.assertEqual(app.state['autoResumeAt'], 0)


class InlineThread:
    """threading.Thread stand-in that runs its target at start()."""

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


class MergedServerHookTests(HistoricalJobFixture):
    """The historical hooks on the merged server: selected stages, owner locks and the retry loop."""

    OFFLINE = 'Historical search preflight failed; no model was started: Cannot verify public network isolation.'

    def pause(self, app):
        app._save_pause({'status': 'paused', 'node': 'author', 'stage': 'solve', 'pauseCause': 'error',
                         'state': {'statement': 'Prove a harmless identity.'}, 'elapsedSeconds': 5.0,
                         'autoResumeAt': 0, 'goalThreadId': 'thread-1', 'error': 'Codex exited.'})
        self.settle(app, phase='paused', stage='solve', activeNode='author', goalThreadId='thread-1',
                    error='Codex exited.')

    def stopped(self, app, **state):
        self.settle(app, phase='done', stage='solve', activeNode='author', manuallyStopped=True,
                    stoppedStage='solve', error='Stopped.', **state)

    def hold(self, app, exc):
        with app.lock:
            app._hold_after_failed_check(exc)

    def test_a_paused_run_held_by_an_unfinished_check_is_checked_again_and_resumes(self):
        app = self.start()
        self.pause(app)
        self.hold(app, retrieval.NeedsAttention('The historical retrieval policy is missing.'))
        self.preflight.side_effect = retrieval.PreflightRetry(self.OFFLINE)
        with self.assertRaisesRegex(ValueError, 'runs again every minute'):
            self.manager.continue_stopped_job(app.state['runId'])
        self.assertAlmostEqual(app._hold_retry_at, time.time() + server.AUTO_RESUME_SECONDS, delta=5)
        self.preflight.side_effect = None
        with mock.patch.object(server.threading, 'Thread', InlineThread):
            self.assertTrue(app.retry_historical_hold_if_due(time.time() + server.AUTO_RESUME_SECONDS + 1))
        self.assertEqual((app.state['phase'], app.state['launchHold']), ('running', ''))
        argv, options = self.launches[-1]
        self.assertEqual(argv[argv.index('--start-node') + 1], 'author')
        self.assertIn('goal_require_resume=true', argv)
        self.assertEqual(Path(options['cwd']), app.run_dir)

    def test_the_checks_run_without_the_job_lock(self):
        app = self.start()
        self.stopped(app)
        self.hold(app, retrieval.NeedsAttention('The historical retrieval policy is missing.'))
        taken = []

        def take_lock():
            if app.lock.acquire(timeout=5):
                app.lock.release()
                taken.append(True)

        def check(path):
            # The job list, this job's page and the retry loop take the job lock meanwhile.
            probe = threading.Thread(target=take_lock)
            probe.start()
            probe.join()

        self.preflight.side_effect = check
        self.assertIs(self.manager.continue_stopped_job(app.state['runId']), app)
        self.assertEqual(taken, [True])
        # A stopped run is released and waits for its own continuation.
        self.assertEqual((app.state['phase'], app.state['launchHold']), ('done', ''))
        self.assertEqual(self.manager._stopped_continuation_plan(app)['action'], 'author')

    def test_a_job_deleted_while_its_checks_run_never_starts(self):
        self.preflight.side_effect = retrieval.PreflightRetry(self.OFFLINE)
        app = self.start()
        self.preflight.side_effect = lambda path: self.manager.delete_job(app.state['runId'])
        with self.assertRaisesRegex(ValueError, 'changed while its historical checks ran'):
            app.rerun_historical_preflight()
        self.assertEqual(self.launches, [])
        self.assertEqual(self.manager.jobs, {})

    def test_a_run_that_passes_its_checks_but_cannot_start_is_held_again_and_retried(self):
        self.preflight.side_effect = retrieval.PreflightRetry(self.OFFLINE)
        app = self.start()
        self.preflight.side_effect = None
        with mock.patch.object(server.subprocess, 'Popen', side_effect=OSError('Too many open files')), \
                mock.patch.object(server.threading, 'Thread', InlineThread):
            self.assertTrue(app.retry_historical_hold_if_due(time.time() + server.AUTO_RESUME_SECONDS + 1))
        self.assertEqual(app.state['phase'], 'prepared')
        self.assertTrue(app.state['launchHold'].startswith(
            'Historical run check passed, but the run did not start: Too many open files.'))
        self.assertTrue(app.state['launchHold'].endswith(server.HISTORICAL_RETRY_SUFFIX))
        self.assertAlmostEqual(app._hold_retry_at, time.time() + server.AUTO_RESUME_SECONDS, delta=5)
        self.assertFalse(app._claims.held())
        with mock.patch.object(server.threading, 'Thread', InlineThread):
            self.assertTrue(app.retry_historical_hold_if_due(time.time() + 2 * server.AUTO_RESUME_SECONDS + 2))
        self.assertEqual((app.state['phase'], app.state['launchHold']), ('running', ''))

    def test_re_run_preflight_checks_the_compiler_only_when_writing_is_selected(self):
        self.preflight.side_effect = retrieval.PreflightRetry(self.OFFLINE)
        app = self.manager.start_direct_job({
            'statement': 'Prove a harmless identity.', 'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF,
            'workflowStages': {'review': False, 'author': True, 'critic': True, 'writing': False}})
        self.preflight.side_effect = None
        self.manager.continue_stopped_job(app.state['runId'])
        self.assertEqual(app.state['phase'], 'running')
        retrieval.preflight_compiler.assert_not_called()

    def test_in_place_continuations_offer_only_selected_stages(self):
        app = self.manager.start_direct_job({
            'statement': 'Prove a harmless identity.', 'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF,
            'workflowStages': {'review': False, 'author': True, 'critic': False, 'writing': False}})
        app._save(runtime.SAVED_CANDIDATE_FILENAME, 'A complete candidate proof.\n')
        self.settle(app, phase='done', stage='critic', activeNode='critic', error='Workflow failed.')
        self.assertIsNone(self.manager._stopped_continuation_plan(app))
        self.assertFalse(self.manager.job_list()[0]['canResumeCritic'])
        runtime.save_final_input('The statement.', 'A complete candidate proof.', directory=app.run_dir)
        self.settle(app, phase='done', stage='final', activeNode='latex_editor', finalInputReady=True,
                    error='Compile failed.')
        self.assertIsNone(self.manager._stopped_continuation_plan(app))
        with self.assertRaisesRegex(ValueError, 'critic stage is disabled'):
            self.manager.resume_critic_job(app.state['runId'])

    def test_an_automatic_resume_that_finds_a_problem_holds_the_job_without_a_retry(self):
        app = self.start()
        self.pause(app)
        with app.lock:
            checkpoint = server.load_pause_checkpoint(app.run_dir)
            app._configure_auto_resume(checkpoint, time.time())
            app._save_pause(checkpoint)
        self.assertTrue(app.state['autoResumeAt'])
        missing = retrieval.NeedsAttention('The historical retrieval policy is missing.')
        with mock.patch.object(retrieval, 'check_run', side_effect=missing):
            self.assertTrue(app.auto_resume_if_due(lambda: self.manager.resume_paused_job(app.state['runId']),
                                                   time.time() + server.AUTO_RESUME_SECONDS + 1))
        self.assertTrue(app.state['launchHold'].startswith('Needs attention: The historical retrieval policy'))
        self.assertEqual(app.state['autoResumeAt'], 0)
        self.assertEqual(server.load_pause_checkpoint(app.run_dir)['autoResumeAt'], 0)
        self.assertFalse(any('Retrying in' in str(record.get('text')) for record in app.state['trace']))
        # Nothing restarts a held job by itself, so it keeps no owner lock.
        self.assertFalse(app._claims.held())

    def test_a_failed_in_place_continuation_keeps_no_owner_lock(self):
        app = self.start()
        self.stopped(app)
        app._claims.release_all()
        with mock.patch.object(server.subprocess, 'Popen', side_effect=OSError('Too many open files')):
            with self.assertRaisesRegex(OSError, 'Too many open files'):
                self.manager.continue_stopped_job(app.state['runId'])
        self.assertFalse(app._claims.held())
        self.assertEqual((app.state['phase'], app.state['manuallyStopped']), ('done', True))
        # A pause checkpoint that restore could not save belongs to the replaced conversation.
        app._unsaved_checkpoint = {'status': 'paused', 'node': 'author', 'stage': 'solve', 'state': {},
                                   'elapsedSeconds': 1.0}
        self.manager.continue_stopped_job(app.state['runId'])
        self.assertEqual(app.state['phase'], 'running')
        self.assertIsNone(app._unsaved_checkpoint)

    def test_re_run_preflight_adopts_a_moved_bundle_with_its_workspace(self):
        app = self.start()
        self.stopped(app, goalWorkspace=str(self.root / 'old-checkout' / 'run'))
        self.hold(app, retrieval.NeedsAttention('The historical retrieval policy is missing.'))
        moved = str(self.root / 'moved' / 'retrieval-policy.json')
        with mock.patch.object(retrieval, 'check_run', return_value=moved):
            self.manager.continue_stopped_job(app.state['runId'])
        self.assertEqual((app.state['retrievalPolicyPath'], app.state['goalWorkspace']),
                         (moved, str(app.run_dir.resolve())))
        self.assertEqual(self.checks[-1], moved)
        saved = json.loads((app.run_dir / 'job-settings.json').read_text())
        self.assertEqual((saved['retrievalPolicyPath'], saved['launchHold']), (moved, ''))
        self.assertTrue(any(record.get('label') == 'Historical policy moved' for record in app.state['trace']))

    def move_checkout(self, app, *, controller=True):
        """Move the runs folder (and the controller folder) as moving the checkout does; restore the job."""
        app._claims.release_all()
        moved = self.root / 'moved'
        moved.mkdir()
        (self.root / 'runs').rename(moved / 'runs')
        controller_root = self.root / 'controller'
        if controller:
            controller_root = controller_root.rename(moved / 'controller')
        self.manager.runs = moved / 'runs'
        restored = server.restore_saved_app(
            server.App(moved / 'runs' / app.run_dir.name / 'transcript.jsonl', moved / 'runs'))
        self.manager.jobs = {restored.state['runId']: restored}
        real_check = retrieval.check_run
        relocation = mock.patch.object(retrieval, 'check_run', side_effect=lambda path, run_dir: real_check(
            path, run_dir, controller_root=controller_root))
        return restored, (moved / 'runs' / app.run_dir.name).resolve(), relocation

    def test_a_moved_checkout_continues_a_stopped_run_in_its_new_folder(self):
        app = self.start()
        self.stopped(app)
        app._save(server.MANUAL_STOP_FILENAME, json.dumps({'schemaVersion': 1, 'stage': 'solve', 'node': 'author',
                                                           'problemMode': 'statement', 'stoppedAt': ''}))
        restored, run_dir, relocation = self.move_checkout(app)
        with relocation:
            self.assertIs(self.manager.continue_stopped_job(restored.state['runId']), restored)
        argv, options = self.launches[-1]
        policy = options['env'][retrieval_runtime.POLICY_ENV]
        self.assertTrue(Path(policy).is_relative_to(self.root / 'moved' / 'controller'))
        self.assertEqual(Path(options['cwd']).resolve(), run_dir)
        self.assertIn('goal_cwd=' + json.dumps(str(run_dir)), argv)
        saved = json.loads((run_dir / 'job-settings.json').read_text())
        self.assertEqual((saved['retrievalPolicyPath'], saved['goalWorkspace']), (policy, str(run_dir)))

    def test_a_moved_runs_folder_resumes_a_paused_run_in_its_new_folder(self):
        app = self.start()
        self.pause(app)
        restored, run_dir, relocation = self.move_checkout(app, controller=False)
        self.assertEqual(restored.state['phase'], 'paused')
        with relocation:
            self.manager.resume_paused_job(restored.state['runId'])
        argv, options = self.launches[-1]
        self.assertEqual(Path(options['cwd']).resolve(), run_dir)
        self.assertIn('goal_cwd=' + json.dumps(str(run_dir)), argv)
        policy = retrieval_runtime.load_policy(restored.state['retrievalPolicyPath'])
        self.assertEqual(Path(policy['isolation']['workspace']), run_dir)

    def test_an_unfinished_check_is_rescheduled_at_restore_only_where_this_server_owns_the_run(self):
        app = self.start()
        self.pause(app)
        self.hold(app, retrieval.PreflightRetry(self.OFFLINE))
        app._claims.release_all()
        restored = server.restore_saved_app(server.App(app.run_dir / 'transcript.jsonl', self.runs))
        self.assertEqual(restored.state['phase'], 'paused')
        self.assertTrue(restored.state['launchHold'].endswith(server.HISTORICAL_RETRY_SUFFIX))
        self.assertLessEqual(restored._hold_retry_at, time.time())
        self.assertEqual(restored.state['autoResumeAt'], 0)
        elsewhere = server.restore_saved_app(server.App(app.run_dir / 'transcript.jsonl', self.runs), owned=False)
        self.assertFalse(getattr(elsewhere, '_hold_retry_at', 0))

    def test_a_finished_run_held_when_its_critic_runs_again_offers_re_run_preflight(self):
        app = self.start()
        app._save(runtime.SAVED_CANDIDATE_FILENAME, 'A complete candidate proof.\n')
        self.settle(app, phase='done', stage='final', activeNode='latex_editor', resultKind='final', error='')
        self.assertIsNone(self.manager._stopped_continuation_plan(app))
        self.assertTrue(self.manager.job_list()[0]['canResumeCritic'])
        missing = retrieval.NeedsAttention('The historical retrieval policy is missing.')
        with mock.patch.object(retrieval, 'check_run', side_effect=missing):
            with self.assertRaisesRegex(ValueError, 'Needs attention'):
                self.manager.resume_critic_job(app.state['runId'])
        # Upstream's finished-result rule must not hide the only way to release the hold.
        row = self.manager.job_list()[0]
        self.assertEqual((row['canContinueStopped'], row['continueStoppedLabel']), (True, 'Re-run preflight'))
        self.manager.continue_stopped_job(app.state['runId'])
        self.assertEqual((app.state['phase'], app.state['launchHold']), ('done', ''))
        self.assertIsNone(self.manager._stopped_continuation_plan(app))
        self.assertTrue(self.manager.job_list()[0]['canResumeCritic'])

    def test_a_batch_that_quotes_a_jobs_own_hold_gives_its_advice_once(self):
        checked = []

        def check_run(path, run_dir):
            checked.append(run_dir)
            if len(checked) == 2:
                raise retrieval.NeedsAttention('The historical retrieval policy is missing.')
            return path

        # A job that cannot start stops no other (I-027): the batch names the held one and its advice, once.
        with mock.patch.object(retrieval, 'check_run', side_effect=check_run):
            started = self.manager.start_direct_batch({'retrievalMode': 'historical', 'retrievalCutoffUtc': UI_CUTOFF,
                                                       'statements': [{'statement': 'A.'}, {'statement': 'B.'}]})
        held = [app for app in self.manager.jobs.values() if app.state['phase'] == 'prepared']
        self.assertEqual(len(held), 1)
        hold = 'Needs attention: The historical retrieval policy is missing. Choose Re-run preflight after fixing it.'
        self.assertEqual(held[0].state['launchHold'], hold)
        self.assertEqual(started.notice.count('Choose Re-run preflight'), 1)
        self.assertIn(hold, started.notice)

    def test_a_hold_that_quotes_a_jobs_own_hold_repeats_none_of_it(self):
        app = self.start()
        own = app._historical_hold_text(retrieval.NeedsAttention('The historical retrieval policy is missing.'))
        self.assertEqual(own, 'Needs attention: The historical retrieval policy is missing. Choose Re-run preflight '
                              'after fixing it.')
        # A failed batch that quotes the job's own hold (the cause it raised).
        self.assertEqual(app._historical_hold_text(ValueError(f'Historical batch launch failed: {own.rstrip(".")}.')),
                         f'Historical batch launch failed: {own}')
        # The same check failing again with the hold's own words.
        self.assertEqual(app._historical_hold_text(retrieval.NeedsAttention(own)), own)
        retry = app._historical_hold_text(retrieval.PreflightRetry(
            'Historical search preflight failed; no model was started: the check did not finish within 30 seconds.'))
        self.assertTrue(retry.endswith('within 30 seconds.' + server.HISTORICAL_RETRY_SUFFIX))
        self.assertEqual(app._historical_hold_text(retrieval.PreflightRetry(retry)), retry)


if __name__ == '__main__':
    unittest.main()
