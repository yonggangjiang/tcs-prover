"""Offline transport reliability tests; no provider traffic or real backoff."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
import errno
import http.client
import io
import json
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import historical_transport as ht
from retrieval.historical_rate_limit import RateLimitError

URL = 'https://web.archive.org/cdx/search/cdx?url=SECRET_QUERY'
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, body=b'historical bytes', *, status=200, headers=None,
                 url=URL, on_read=None):
        self.body, self.status = io.BytesIO(body), status
        self.headers, self.url = headers or {}, url
        self.on_read, self.closed = on_read, False

    def geturl(self):
        return self.url

    def read(self, size):
        if self.on_read:
            self.on_read()
        return self.body.read(size)

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, steps, on_open=None):
        self.steps, self.calls, self.on_open = iter(steps), [], on_open

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.on_open:
            self.on_open()
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


class Coordinator:
    def __init__(self):
        self.active, self.slots, self.deferrals = False, [], []

    @contextmanager
    def slot(self, host, deadline):
        assert not self.active
        self.active = True
        self.slots.append((host, deadline))
        try:
            yield
        finally:
            self.active = False

    def defer(self, host, seconds, deadline):
        assert self.active, 'Cooldown must be persisted before the slot is released'
        self.deferrals.append((host, seconds, deadline))


class TransportTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(ht.socket, 'getaddrinfo', return_value=PUBLIC_DNS)
        self.dns = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.events = []

    def client(self, steps, **kwargs):
        options = {'clock': self.clock, 'sleeper': self.clock.sleep,
                   'wall_clock': lambda: 1700000000.0,
                   'audit': lambda event, **fields: self.events.append((event, fields))}
        options.update(kwargs)
        client = ht.RestrictedHttpClient(**options)
        client.opener = Opener(steps)
        return client

    def test_error_defaults_remain_compatible(self):
        error = ht.HistoricalSearchError('Static rejection.')
        self.assertEqual(error.code, 'retrieval_rejected')
        self.assertFalse(error.retryable)
        self.assertFalse(error.operational)
        self.assertIsNone(error.status)
        self.assertIsNone(error.host)
        self.assertIsNone(error.failure_kind)
        for invalid in ('SECRET_EXCEPTION', {'token': 'SECRET'}, None):
            self.assertIsNone(ht.HistoricalSearchError('Static.', failure_kind=invalid).failure_kind)

    def test_429_honors_retry_after_and_recovers(self):
        denied = HTTPError(URL, 429, 'SECRET_BODY', {'Retry-After': '7'}, io.BytesIO(b'SECRET_BODY'))
        client = self.client([denied, Response()])
        self.assertEqual(client.get(URL).body, b'historical bytes')
        self.assertEqual(self.clock.sleeps, [7.0])
        self.assertEqual(len(client.opener.calls), 2)
        self.assertTrue(denied.fp.closed)
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_http_date_retry_after(self):
        retry_date = format_datetime(datetime.fromtimestamp(1700000009, timezone.utc), usegmt=True)
        client = self.client([Response(status=503, headers={'Retry-After': retry_date}), Response()])
        client.get(URL)
        self.assertEqual(self.clock.sleeps, [9.0])

    def test_timeouts_and_bad_gateway_recover(self):
        client = self.client([URLError(TimeoutError('SECRET_EXCEPTION')),
                              Response(status=502), Response()])
        client.get(URL, headers={'X-Subscription-Token': 'SECRET_CREDENTIAL'})
        self.assertEqual(len(client.opener.calls), 3)
        self.assertEqual(self.clock.sleeps, [5.0, 10.0])
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_retry_exhaustion_is_operational_and_bounded(self):
        coordinator = Coordinator()
        client = self.client([OSError('SECRET_EXCEPTION')] * 4, coordinator=coordinator)
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            client.get(URL)
        self.assertTrue(caught.exception.operational)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(client.opener.calls), 3)
        self.assertEqual(self.clock.sleeps, [5.0, 10.0])
        self.assertEqual(coordinator.deferrals, [])
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_failure_categories_use_only_allowlisted_values(self):
        cases = [
            (URLError(socket.gaierror(socket.EAI_AGAIN, 'SECRET')), 'dns'),
            (TimeoutError('SECRET'), 'timeout'),
            (URLError(TimeoutError('SECRET')), 'timeout'),
            (ConnectionResetError(errno.ECONNRESET, 'SECRET'), 'connection'),
            (OSError(errno.EHOSTUNREACH, 'SECRET'), 'connection'),
            (http.client.RemoteDisconnected('SECRET'), 'connection'),
            (http.client.BadStatusLine('SECRET_PROVIDER_BODY'), 'protocol'),
            (ssl.SSLError('SECRET'), 'tls'),
            (URLError(ssl.SSLError('SECRET')), 'tls'),
            (OSError('SECRET'), 'other'),
            (URLError('SECRET_URL'), 'other'),
        ]
        for cause, expected in cases:
            with self.subTest(kind=expected, cause=type(cause).__name__):
                client = self.client([cause], retries=0)
                with self.assertRaises(ht.HistoricalSearchError) as caught:
                    client.get(URL)
                self.assertEqual(caught.exception.failure_kind, expected)
                self.assertEqual(self.events[-1][1]['failure_kind'], expected)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_retry_audits_keep_sanitized_failure_kind(self):
        client = self.client([ConnectionResetError('SECRET'), Response()])
        client.get(URL)
        retry = next(fields for event, fields in self.events if event == 'http_retry')
        self.assertEqual(retry['failure_kind'], 'connection')
        self.assertEqual(retry['delay_seconds'], 5.0)
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_connection_dns_and_timeout_retries_do_not_defer_other_jobs(self):
        for cause in (ConnectionResetError('SECRET'), socket.gaierror('SECRET'),
                      TimeoutError('SECRET'), URLError(TimeoutError('SECRET'))):
            with self.subTest(cause=type(cause).__name__):
                coordinator = Coordinator()
                client = self.client([cause, cause, Response()], coordinator=coordinator)
                before = len(self.clock.sleeps)
                client.get(URL)
                self.assertEqual(self.clock.sleeps[before:], [5.0, 10.0])
                self.assertEqual(coordinator.deferrals, [])
                self.assertEqual(len(coordinator.slots), 3)
                # A separate job's next request does not inherit a cooldown
                # from failures of the first job's individual connection.
                other = self.client([Response()], coordinator=coordinator)
                other.get(URL)
                self.assertEqual(coordinator.deferrals, [])

    def test_only_provider_signals_extend_shared_cooldown(self):
        for status in ht.RETRY_STATUSES:
            with self.subTest(status=status):
                coordinator = Coordinator()
                client = self.client([Response(status=status) for _ in range(3)], coordinator=coordinator)
                with self.assertRaises(ht.HistoricalSearchError):
                    client.get(URL)
                expected = [5.0, 10.0, 10.0] if status in (429, 503) else []
                self.assertEqual([row[1] for row in coordinator.deferrals], expected)

    def test_explicit_retry_after_is_shared_without_extra_global_backoff(self):
        for value, shared_delay, local_delay in (('0', 0.0, 5.0), ('2', 2.0, 5.0), ('8', 8.0, 8.0)):
            with self.subTest(retry_after=value):
                coordinator = Coordinator()
                client = self.client([Response(status=502, headers={'Retry-After': value}), Response()],
                                     coordinator=coordinator)
                before = len(self.clock.sleeps)
                client.get(URL)
                self.assertEqual(self.clock.sleeps[before:], [local_delay])
                self.assertEqual([row[1] for row in coordinator.deferrals], [shared_delay])

    def test_invalid_retry_after_does_not_defer_unrelated_jobs(self):
        for value in ('', 'SECRET_INVALID', 'NaN', 'Infinity'):
            with self.subTest(retry_after=value):
                coordinator = Coordinator()
                client = self.client([Response(status=502, headers={'Retry-After': value}), Response()],
                                     coordinator=coordinator)
                client.get(URL)
                self.assertEqual(coordinator.deferrals, [])

    def test_explicit_cooldown_on_challenge_is_shared_without_retry(self):
        coordinator = Coordinator()
        client = self.client([Response(status=403, headers={'Retry-After': '60'})], coordinator=coordinator)
        with self.assertRaises(ht.HistoricalSearchError):
            client.get(URL)
        self.assertEqual([row[1] for row in coordinator.deferrals], [60.0])
        self.assertEqual(len(client.opener.calls), 1)

    def test_default_operation_budget_is_three_hundred_seconds(self):
        coordinator = Coordinator()
        client = self.client([Response()], coordinator=coordinator)
        client.get(URL)
        self.assertEqual(coordinator.slots[0][1], self.clock.now + 300)

    def test_all_retryable_statuses_use_at_most_three_attempts(self):
        for status in ht.RETRY_STATUSES:
            with self.subTest(status=status):
                client = self.client([Response(status=status) for _ in range(3)])
                with self.assertRaises(ht.HistoricalSearchError) as caught:
                    client.get(URL)
                self.assertEqual(len(client.opener.calls), 3)
                self.assertEqual(caught.exception.status, status)
                self.assertTrue(caught.exception.operational)

    def test_challenge_and_permanent_responses_are_not_retried(self):
        for status in (202, 403, 404, 410, 400, 301, 302, 307, 308):
            with self.subTest(status=status):
                response = Response(status=status, body=b'SECRET_PROVIDER_BODY')
                client = self.client([response, Response()])
                with self.assertRaises(ht.HistoricalSearchError) as caught:
                    client.get(URL)
                self.assertEqual(caught.exception.operational, status in (202, 403))
                self.assertEqual(response.body.tell(), 0)
                self.assertTrue(response.closed)
                self.assertEqual(len(client.opener.calls), 1)
                self.assertNotIn('SECRET', str(caught.exception))

    def test_redirect_handler_and_changed_final_url_fail_closed(self):
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            ht.NoRedirect().redirect_request(None, None, 302, 'SECRET', {}, 'https://127.0.0.1/SECRET')
        self.assertEqual(caught.exception.code, 'redirect_rejected')
        self.assertFalse(caught.exception.operational)
        response = Response(url='https://web.archive.org/changed')
        client = self.client([response])
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            client.get(URL)
        self.assertEqual(caught.exception.code, 'redirect_rejected')
        self.assertEqual(response.body.tell(), 0)

    def test_private_dns_and_disallowed_destinations_never_open(self):
        client = self.client([])
        for url in ('http://web.archive.org/', 'https://example.org/',
                    'https://user:pass@web.archive.org/', 'https://web.archive.org:444/',
                    'https://web.archive.org/%0aHEADER', 'https://web.archive.org/#fragment'):
            with self.subTest(url=url), self.assertRaises(ht.HistoricalSearchError):
                client.get(url)
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1'):
            self.dns.return_value = PUBLIC_DNS + [(socket.AF_INET, 1, 6, '', (address, 443))]
            with self.subTest(address=address), self.assertRaises(ht.HistoricalSearchError) as caught:
                client.get(URL)
            self.assertEqual(caught.exception.code, 'destination_rejected')
            self.assertFalse(caught.exception.operational)
        self.assertEqual(client.opener.calls, [])

    def test_tls_verification_failure_is_not_retried(self):
        for error in (ssl.SSLCertVerificationError('SECRET'),
                      URLError(ssl.SSLCertVerificationError('SECRET'))):
            client = self.client([error, Response()])
            with self.assertRaises(ht.HistoricalSearchError) as caught:
                client.get(URL)
            self.assertEqual(caught.exception.code, 'tls_rejected')
            self.assertEqual(caught.exception.failure_kind, 'tls')
            self.assertFalse(caught.exception.operational)
            self.assertEqual(len(client.opener.calls), 1)

    def test_proxies_disabled_and_redirects_forbidden(self):
        with patch.object(ht, 'build_opener', wraps=ht.build_opener) as build:
            ht.RestrictedHttpClient()
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, ht.NoRedirect)

    def test_size_limit_without_retry(self):
        for response in (Response(body=b'x' * (ht.MAX_BYTES + 1)),
                         Response(headers={'Content-Length': str(ht.MAX_BYTES + 1)})):
            client = self.client([response, Response()])
            with self.assertRaises(ht.HistoricalSearchError) as caught:
                client.get(URL)
            self.assertEqual(caught.exception.code, 'response_too_large')
            self.assertFalse(caught.exception.operational)
            self.assertEqual(len(client.opener.calls), 1)
            self.assertTrue(response.closed)

    def test_budget_is_shared_across_requests_and_nested_operations(self):
        client = self.client([Response()])
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            with client.operation(timeout_seconds=5):
                client.get(URL)
                self.clock.now += 4
                with client.operation(timeout_seconds=100):
                    self.clock.now += 2
                    client.get(URL)
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertEqual(len(client.opener.calls), 1)

    def test_socket_open_timeout_is_capped_by_remaining_budget(self):
        client = self.client([Response()], timeout=15)
        with client.operation(timeout_seconds=5):
            client.get(URL)
        self.assertEqual(client.opener.calls[0][1], 5.0)

    def test_trickle_body_cannot_extend_operation(self):
        response = Response(body=b'x' * 200000, on_read=lambda: setattr(self.clock, 'now', self.clock.now + 2))
        client = self.client([response])
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            with client.operation(timeout_seconds=5):
                client.get(URL)
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertTrue(caught.exception.operational)
        self.assertTrue(response.closed)

    def test_coordinator_is_held_across_open_read_and_cooldown(self):
        coordinator = Coordinator()
        def assert_held():
            self.assertTrue(coordinator.active)
        client = self.client([Response(status=429, headers={'Retry-After': '3'}),
                              Response(on_read=assert_held)], coordinator=coordinator)
        client.opener.on_open = assert_held
        client.get(URL)
        self.assertFalse(coordinator.active)
        self.assertEqual(len(coordinator.slots), 2)
        self.assertEqual(coordinator.deferrals[0][1], 5.0)

    def test_cooldown_beyond_deadline_is_persisted_without_early_retry(self):
        coordinator = Coordinator()
        client = self.client([Response(status=429, headers={'Retry-After': '3600'}), Response()],
                             coordinator=coordinator)
        with self.assertRaises(ht.HistoricalSearchError) as caught:
            with client.operation(timeout_seconds=5):
                client.get(URL)
        self.assertEqual(caught.exception.code, 'deadline_exceeded')
        self.assertEqual(coordinator.deferrals[0][1], 3600)
        self.assertEqual(len(client.opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_coordinator_failures_are_sanitized_operational_failures(self):
        coordinator = Coordinator()
        with patch.object(coordinator, 'slot', side_effect=RateLimitError('SECRET_STATE', 'rate_limit_state')):
            client = self.client([], coordinator=coordinator)
            with self.assertRaises(ht.HistoricalSearchError) as caught:
                client.get(URL)
        self.assertEqual(caught.exception.code, 'rate_limit_state')
        self.assertTrue(caught.exception.operational)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(client.opener.calls, [])

    def test_audit_failure_aborts_without_retry(self):
        for failed_event in ('http_attempt', 'http_failure', 'http_retry', 'http_success'):
            def audit(event, **fields):
                if event == failed_event:
                    raise OSError('SECRET_LOG_PATH')
            response = Response() if failed_event == 'http_success' else Response(status=503)
            client = self.client([response, Response()], audit=audit)
            with self.subTest(event=failed_event), self.assertRaises(ht.HistoricalSearchError) as caught:
                client.get(URL)
            self.assertEqual(caught.exception.code, 'audit_failure')
            self.assertLessEqual(len(client.opener.calls), 1)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_dns_stall_is_bounded_and_workers_do_not_accumulate(self):
        # This is a deadline/liveness test, not a backoff sleep. The resolver is
        # explicitly released and all worker threads finish before patch teardown.
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def resolver(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(2)
            if len(calls) == 2:
                finished.set()
            return PUBLIC_DNS
        self.dns.side_effect = resolver
        client = self.client([], clock=time.monotonic, sleeper=time.sleep)
        started = time.monotonic()
        try:
            for _ in range(3):
                with self.assertRaises(ht.HistoricalSearchError) as caught:
                    with client.operation(timeout_seconds=0.03):
                        client.get(URL)
                self.assertEqual(caught.exception.code, 'deadline_exceeded')
            self.assertTrue(entered.is_set())
            self.assertEqual(len(calls), 2)
            self.assertEqual(client.opener.calls, [])
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))


if __name__ == '__main__':
    unittest.main()
