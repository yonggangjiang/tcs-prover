"""Offline regressions for bounded, concurrent historical MCP dispatch."""
from contextlib import contextmanager
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import historical_search as hs
import scholarly_discovery as sd


def request(request_id, method='tools/call', **params):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}


def search(request_id, query='lemma'):
    return request(request_id, name='search', arguments={'query': query})


class InputQueue:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, limit):
        return self.lines.get(timeout=10)

    def send(self, message):
        self.lines.put(json.dumps(message) + '\n')


class OutputQueue:
    def __init__(self):
        self.records = queue.Queue()
        self.lines = []
        self.writing = threading.Lock()

    def write(self, line):
        if not self.writing.acquire(blocking=False):
            raise AssertionError('Concurrent response writes interleaved')
        try:
            time.sleep(0.001)
            self.lines.append(line)
            self.records.put(json.loads(line))
        finally:
            self.writing.release()

    def flush(self):
        pass


class FakeEngine:
    operation_timeout = 2.0

    def __init__(self, behavior=lambda engine, value: {'results': [value]}, audit=None):
        self.behavior = behavior
        if audit is not None:
            self.audit = audit

    def search(self, value):
        return self.behavior(self, value)

    def open(self, url, **navigation):
        return self.behavior(self, url)

    def find(self, url, pattern, **navigation):
        return self.behavior(self, pattern)


class Harness:
    def __init__(self, testcase, factory, *, base=None, max_active=4):
        self.input, self.output = InputQueue(), OutputQueue()
        self.server = hs.StdioMCPServer(base or FakeEngine(), engine_factory=factory,
                                       max_active=max_active)
        self.errors, self.releases = [], []

        def serve():
            try:
                self.server.serve(self.input, self.output)
            except BaseException as error:
                self.errors.append(error)

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        testcase.addCleanup(self.close)
        self.input.send(request('initialize', 'initialize'))
        self.initialization = self.receive()

    def receive(self, timeout=2):
        return self.output.records.get(timeout=timeout)

    def release_on_close(self, event):
        self.releases.append(event)
        return event

    def close(self):
        for event in self.releases:
            event.set()
        self.input.lines.put('')
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise AssertionError('MCP reader failed to drain on EOF')
        if self.errors:
            raise self.errors[0]


class ConcurrentMCPTests(unittest.TestCase):
    def wait_idle(self, harness):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with harness.server._state_lock:
                if not harness.server._active:
                    return
            time.sleep(0.002)
        self.fail('Worker retained its slot after exiting')

    def test_slow_call_does_not_block_ping_list_or_fast_call(self):
        started, release = threading.Event(), threading.Event()
        engines = []

        def behavior(engine, query):
            if query == 'slow':
                started.set()
                release.wait(3)
            return {'results': [query]}

        def factory():
            engine = FakeEngine(behavior)
            engines.append(engine)
            return engine

        harness = Harness(self, factory)
        harness.release_on_close(release)
        self.assertIn('at most 4 concurrent', harness.initialization['result']['instructions'])
        harness.input.send(search(1, 'slow'))
        self.assertTrue(started.wait(1))
        harness.input.send(request(2, 'ping'))
        harness.input.send(request(3, 'tools/list'))
        harness.input.send(search(4, 'fast'))
        records = [harness.receive() for _ in range(3)]
        self.assertEqual({record['id'] for record in records}, {2, 3, 4})
        self.assertEqual(len(engines), 2)
        self.assertIsNot(engines[0], engines[1])
        release.set()
        self.assertEqual(harness.receive()['id'], 1)

    def test_four_calls_and_immediate_busy_without_factory(self):
        entered, release = queue.Queue(), threading.Event()
        factories = []

        def behavior(engine, query):
            entered.put(query)
            release.wait(3)
            return {'results': [query]}

        def factory():
            factories.append(object())
            return FakeEngine(behavior)

        harness = Harness(self, factory)
        harness.release_on_close(release)
        for number in range(4):
            harness.input.send(search(number))
        for _ in range(4):
            entered.get(timeout=1)
        harness.input.send(search(5))
        rejected = harness.receive()
        self.assertEqual(rejected['id'], 5)
        self.assertIn('busy', rejected['result']['content'][0]['text'])
        self.assertEqual(len(factories), 4)
        release.set()
        records = [harness.receive() for _ in range(4)]
        self.assertEqual({record['id'] for record in records}, {0, 1, 2, 3})
        self.assertTrue(all(not record['result']['isError'] for record in records))
        self.assertEqual(len(harness.output.lines), 6)
        self.assertTrue(all(line.endswith('\n') and len(line.splitlines()) == 1
                            for line in harness.output.lines))

    def test_cancellation_suppresses_reply_and_retains_capacity(self):
        started, release = threading.Event(), threading.Event()

        def behavior(engine, query):
            started.set()
            release.wait(3)
            return {'results': ['verified evidence']}

        harness = Harness(self, lambda: FakeEngine(behavior), max_active=1)
        harness.release_on_close(release)
        harness.input.send(search(1))
        self.assertTrue(started.wait(1))
        harness.input.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                            'params': {'requestId': 1}})
        harness.input.send(search(2))
        rejected = harness.receive()
        self.assertEqual(rejected['id'], 2)
        self.assertIn('busy', rejected['result']['content'][0]['text'])
        release.set()
        self.wait_idle(harness)
        self.assertTrue(harness.output.records.empty())
        harness.input.send(search(3))
        self.assertEqual(harness.receive()['id'], 3)

    def test_deadline_includes_blocked_factory_and_retains_slot(self):
        started, release = threading.Event(), threading.Event()
        provider_calls = []

        def factory():
            started.set()
            release.wait(3)
            return FakeEngine(lambda engine, query: provider_calls.append(query))

        base = FakeEngine()
        base.operation_timeout = 0.04
        with patch.object(hs.StdioMCPServer, 'RESPONSE_GRACE_SECONDS', 0.04):
            harness = Harness(self, factory, base=base, max_active=1)
            harness.release_on_close(release)
            began = time.monotonic()
            harness.input.send(search(1))
            self.assertTrue(started.wait(1))
            expired = harness.receive(timeout=1)
            self.assertLess(time.monotonic() - began, 0.8)
            self.assertIn('deadline', expired['result']['content'][0]['text'])
            harness.input.send(search(2))
            self.assertIn('busy', harness.receive()['result']['content'][0]['text'])
            release.set()
            self.wait_idle(harness)
            self.assertFalse(provider_calls)
            self.assertTrue(harness.output.records.empty())

    def test_cancellation_during_factory_prevents_provider_execution(self):
        started, release = threading.Event(), threading.Event()
        provider_calls = []

        def factory():
            started.set()
            release.wait(3)
            return FakeEngine(lambda engine, query: provider_calls.append(query))

        harness = Harness(self, factory)
        harness.release_on_close(release)
        harness.input.send(search(1))
        self.assertTrue(started.wait(1))
        harness.input.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                            'params': {'requestId': 1}})
        harness.input.send(request(2, 'ping'))
        self.assertEqual(harness.receive()['id'], 2)
        release.set()
        self.wait_idle(harness)
        self.assertFalse(provider_calls)
        self.assertTrue(harness.output.records.empty())

    def test_cancelled_page_and_find_never_deliver_late_document_text(self):
        for name, arguments in (('open', {'url': 'https://example.org/paper', 'offset': 115000}),
                                ('find', {'url': 'https://example.org/paper', 'pattern': 'Definition'})):
            with self.subTest(name=name):
                started, release = threading.Event(), threading.Event()

                def behavior(engine, value):
                    started.set()
                    release.wait(3)
                    return {'result': {'text': 'LATE ARCHIVED DOCUMENT TEXT'}}

                harness = Harness(self, lambda: FakeEngine(behavior), max_active=1)
                harness.release_on_close(release)
                harness.input.send(request(1, name=name, arguments=arguments))
                self.assertTrue(started.wait(1))
                harness.input.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                    'params': {'requestId': 1}})
                harness.input.send(request(2, 'ping'))
                self.assertEqual(harness.receive()['id'], 2)
                release.set()
                self.wait_idle(harness)
                self.assertTrue(harness.output.records.empty())
                self.assertNotIn('LATE ARCHIVED', ''.join(harness.output.lines))
                harness.close()

    def test_verified_partial_response_survives_network_deadline_finalization(self):
        observed = []

        def behavior(engine, query):
            observed.append(engine.request_deadline)
            time.sleep(max(0, engine.request_deadline - time.monotonic()) + 0.025)
            return {'results': ['already verified capture'], 'incomplete': True}

        base = FakeEngine()
        base.operation_timeout = 0.04
        with patch.object(hs.StdioMCPServer, 'RESPONSE_GRACE_SECONDS', 0.3):
            harness = Harness(self, lambda: FakeEngine(behavior), base=base)
            started = time.monotonic()
            harness.input.send(search(1))
            response = harness.receive()
            self.assertLess(observed[0] - started, 0.15)
            self.assertGreater(time.monotonic(), observed[0])
            self.assertFalse(response['result']['isError'])
            data = json.loads(response['result']['content'][0]['text'])
            self.assertTrue(data['incomplete'])
            self.assertEqual(data['results'], ['already verified capture'])

    def test_stuck_execution_times_out_once_after_grace(self):
        started, release = threading.Event(), threading.Event()

        def behavior(engine, query):
            started.set()
            release.wait(3)
            return {'results': ['late evidence']}

        base = FakeEngine()
        base.operation_timeout = 0.04
        with patch.object(hs.StdioMCPServer, 'RESPONSE_GRACE_SECONDS', 0.04):
            harness = Harness(self, lambda: FakeEngine(behavior), base=base)
            harness.release_on_close(release)
            began = time.monotonic()
            harness.input.send(search(1))
            self.assertTrue(started.wait(1))
            response = harness.receive(timeout=1)
            self.assertLess(time.monotonic() - began, 0.8)
            self.assertTrue(response['result']['isError'])
            release.set()
            self.wait_idle(harness)
            self.assertTrue(harness.output.records.empty())
            self.assertNotIn('late evidence', ''.join(harness.output.lines))

    def test_eof_drains_accepted_calls(self):
        started, release = threading.Event(), threading.Event()

        def behavior(engine, query):
            started.set()
            release.wait(3)
            return {'results': [query]}

        harness = Harness(self, lambda: FakeEngine(behavior))
        harness.release_on_close(release)
        harness.input.send(search(1))
        self.assertTrue(started.wait(1))
        harness.input.lines.put('')
        release.set()
        self.assertEqual(harness.receive()['id'], 1)
        harness.thread.join(timeout=1)
        self.assertFalse(harness.thread.is_alive())

    def test_invalid_calls_do_not_create_engine_or_consume_slots(self):
        created = []

        def factory():
            created.append(True)
            return FakeEngine()

        harness = Harness(self, factory)
        invalid = [request(1, name='search', arguments={'query': 'lemma', 'cutoff': 'future'}),
                   request(2, name='fetch', arguments={'url': 'https://example.org'}),
                   request(3, name='search', arguments={'query': []}),
                   request(4, name='open', arguments={}), search(True),
                   request(5, name='open', arguments={'url': 'https://example.org', 'offset': True}),
                   request(6, name='find', arguments={'url': 'https://example.org', 'pattern': 'lemma', 'max_matches': 2 ** 53}),
                   request(7, name='find', arguments={'url': 'https://example.org', 'pattern': 'lemma', 'cutoff': 'future'})]
        for message in invalid:
            harness.input.send(message)
        for _ in invalid:
            self.assertIn('error', harness.receive())
        self.assertEqual(created, [])

    def test_concurrent_audit_records_keep_distinct_operation_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'audit.jsonl'
            cutoff = hs.parse_cutoff('2026-10-02T12:00:00Z')
            base = FakeEngine(audit=hs.AuditLog(path, cutoff, 'test'))
            barrier = threading.Barrier(4)

            def behavior(engine, query):
                barrier.wait(timeout=2)
                for index in range(5):
                    engine.audit.write('capture_verified', sequence=index)
                return {'results': [query]}

            def factory():
                return FakeEngine(behavior, audit=hs.AuditLog(path, cutoff, 'test'))

            harness = Harness(self, factory, base=base)
            for number in range(4):
                harness.input.send(search(number))
            self.assertTrue(all(not harness.receive()['result']['isError'] for _ in range(4)))
            self.wait_idle(harness)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            accepted = {record['operation_id'] for record in records if record['event'] == 'mcp_accepted'}
            completed = {record['operation_id'] for record in records if record['event'] == 'mcp_response_ready'}
            verified = [record for record in records if record['event'] == 'capture_verified']
            self.assertEqual(len(accepted), 4)
            self.assertEqual(accepted, completed)
            self.assertEqual(len(verified), 20)
            self.assertEqual({record['operation_id'] for record in verified}, accepted)
            harness.close()

    def test_audit_failure_prevents_evidence_delivery(self):
        def write(event, **fields):
            if event == 'mcp_response_ready':
                raise hs.HistoricalSearchError('Audit failure', code='audit_failure')

        base = FakeEngine(audit=SimpleNamespace(write=write))
        harness = Harness(self, lambda: FakeEngine(lambda engine, value: {'results': ['SECRET EVIDENCE']}), base=base)
        harness.input.send(search(1))
        response = harness.receive()
        self.assertTrue(response['result']['isError'])
        self.assertNotIn('SECRET EVIDENCE', ''.join(harness.output.lines))

    def test_cli_factory_pins_startup_policy_without_explicit_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path = root / 'policy.json'
            policy = {'schema_version': 1, 'provider': 'duckduckgo_wayback'}
            policy_path.write_text(json.dumps(policy))
            initial_hash = hs.sha256(policy_path.read_bytes())
            cutoff = '2026-10-02T12:00:00Z'
            testcase = self
            verified = []

            class Server:
                def __init__(self, engine, *, engine_factory):
                    self.engine, self.factory = engine, engine_factory

                def serve(self, input_stream, output_stream):
                    other = self.factory()
                    testcase.assertIsNot(self.engine, other)
                    testcase.assertEqual(other.cutoff, self.engine.cutoff)
                    testcase.assertEqual(other.policy_sha256, initial_hash)
                    policy['max_results'] = 1
                    policy_path.write_text(json.dumps(policy))
                    with testcase.assertRaisesRegex(hs.HistoricalSearchError, 'pin'):
                        self.factory()
                    verified.append(True)

            with patch.object(hs, 'StdioMCPServer', Server):
                status = hs.main(['--cutoff', cutoff, '--policy-file', str(policy_path),
                                  '--audit-log', str(root / 'audit.jsonl')])
            self.assertEqual(status, 0)
            self.assertEqual(verified, [True])

    def test_coordinated_transport_bypasses_local_lock_but_enters_shared_slot(self):
        entered = []

        class Coordinator:
            @contextmanager
            def slot(self, host, *, deadline):
                entered.append((host, deadline))
                yield

        client = hs.RestrictedHttpClient(coordinator=Coordinator(), retries=0)
        pacer = sd.PacedRequests(3)
        url = 'https://export.arxiv.org/api/query?search_query=lemma'
        response = hs.HttpResponse(b'archived', {}, url)
        result = []

        def retrieve():
            with client.operation(timeout_seconds=0.2):
                result.append(pacer.get(client, url))

        with patch.object(client, '_request', return_value=response), pacer.lock:
            # Holding the old process-local lock must not block a client
            # whose shared coordinator already owns pacing and deadlines.
            began = time.monotonic()
            thread = threading.Thread(target=retrieve, daemon=True)
            thread.start()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [response])
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0][0], 'export.arxiv.org')
        self.assertLess(entered[0][1] - began, 0.4)


if __name__ == '__main__':
    unittest.main()
