"""Bounded, allowlisted HTTP transport for the historical retrieval broker.

Provider bodies and exception text never enter errors or audit events. A bounded
daemon worker pool covers DNS as well as socket I/O; a blocked OS resolver cannot
keep an MCP operation alive past its deadline or create unlimited new workers.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import errno
import http.client
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from retrieval.historical_rate_limit import RateLimitError

ALLOWED_NETWORK_HOSTS = frozenset({
    'api.search.brave.com', 'lite.duckduckgo.com', 'web.archive.org',
    'export.arxiv.org', 'api.crossref.org',
})
MAX_BYTES = 4 * 1024 * 1024
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
FAILURE_KINDS = frozenset({'dns', 'timeout', 'connection', 'protocol', 'tls', 'other'})


class HistoricalSearchError(RuntimeError):
    """Static controller-written message, with sanitized classification fields."""

    def __init__(self, message, *, code='retrieval_rejected', retryable=False,
                 operational=False, status=None, host=None, failure_kind=None):
        super().__init__(message)
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,64}', code) else 'retrieval_rejected'
        self.retryable = bool(retryable)
        self.operational = bool(operational)
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.host = host if host in ALLOWED_NETWORK_HOSTS else None
        self.failure_kind = failure_kind if isinstance(failure_kind, str) and failure_kind in FAILURE_KINDS else None


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: dict[str, str]
    url: str
    status: int = 200


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HistoricalSearchError('Historical retrieval redirects are forbidden.',
                                    code='redirect_rejected', status=code)


@dataclass
class _Outcome:
    response: HttpResponse | None = None
    error: HistoricalSearchError | None = None
    delay: float = 0.0


class _HttpFailure(Exception):
    def __init__(self, error, retry_after=None):
        self.error, self.retry_after = error, retry_after


def _failure_kind(error):
    """Classify only Python exception types and numeric errno, never messages."""
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        error = error.reason
    if isinstance(error, (socket.gaierror, socket.herror)):
        return 'dns'
    if isinstance(error, TimeoutError) or getattr(error, 'errno', None) == errno.ETIMEDOUT:
        return 'timeout'
    if isinstance(error, ssl.SSLError):
        return 'tls'
    if isinstance(error, ConnectionError) or getattr(error, 'errno', None) in {
        errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE,
        errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENOTCONN,
    }:
        return 'connection'
    if isinstance(error, http.client.HTTPException):
        return 'protocol'
    return 'other'


class RestrictedHttpClient:
    def __init__(self, timeout=15, *, coordinator=None, audit=None, retries=2,
                 clock=time.monotonic, sleeper=time.sleep, wall_clock=time.time):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('HTTP timeout must be a finite positive number.')
        if type(retries) is not int or not 0 <= retries <= 2:
            raise ValueError('Historical HTTP retries must be between zero and two.')
        self.timeout, self.retries = float(timeout), retries
        self.coordinator, self.audit = coordinator, audit
        self.clock, self.sleeper, self.wall_clock = clock, sleeper, wall_clock
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self._deadline = ContextVar('historical_http_deadline', default=None)
        self._workers = threading.BoundedSemaphore(2)

    @contextmanager
    def operation(self, timeout_seconds=300):
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise HistoricalSearchError('Historical retrieval deadline is invalid.',
                                        code='deadline_exceeded', operational=True)
        deadline = self.clock() + timeout_seconds
        previous = self._deadline.get()
        if previous is not None:
            deadline = min(previous, deadline)
        token = self._deadline.set(deadline)
        try:
            self._remaining(deadline)
            yield self
        finally:
            self._deadline.reset(token)

    def _remaining(self, deadline, host=None):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise HistoricalSearchError('Historical retrieval deadline was exhausted.',
                                        code='deadline_exceeded', operational=True, host=host)
        return remaining

    def _emit(self, event, **fields):
        if self.audit is not None:
            try:
                self.audit(event, **fields)
            except Exception:
                raise HistoricalSearchError('Historical retrieval audit could not be recorded.',
                                            code='audit_failure', operational=True) from None

    def _host(self, url):
        try:
            if (not isinstance(url, str) or len(url) > 16384
                    or re.search(r'[\s\\\x00-\x1f\x7f]', url)
                    or re.search(r'[\x00-\x1f\x7f]', unquote(url))):
                raise ValueError
            parts = urlsplit(url)
            if (parts.scheme != 'https' or parts.hostname not in ALLOWED_NETWORK_HOSTS
                    or parts.username is not None or parts.password is not None
                    or parts.port not in (None, 443) or parts.fragment):
                raise ValueError
            return parts.hostname
        except (ValueError, UnicodeError):
            raise HistoricalSearchError('Historical retrieval destination is forbidden.',
                                        code='destination_rejected') from None

    def _retry_after(self, headers):
        value = headers.get('Retry-After', headers.get('retry-after', '')) if headers else ''
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    return None
                delay = date.timestamp() - self.wall_clock()
            except (TypeError, ValueError, OverflowError, AttributeError):
                return None
        return max(0.0, delay) if math.isfinite(delay) else None

    def _status_error(self, status, host):
        redirect = 300 <= status < 400
        return HistoricalSearchError(
            'Historical retrieval provider rejected the request.',
            code=('redirect_rejected' if redirect else 'rate_limited' if status == 429
                  else 'provider_challenge' if status in (202, 403) else 'http_error'),
            retryable=status in RETRY_STATUSES,
            operational=status in RETRY_STATUSES or status in (202, 401, 403),
            status=status, host=host)

    def _request(self, url, host, headers, deadline):
        response = None
        try:
            self._remaining(deadline, host)
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            self._remaining(deadline, host)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                raise HistoricalSearchError('Historical retrieval resolved to a forbidden address.',
                                            code='destination_rejected', host=host)
            request_headers = {'User-Agent': 'tcs-prover-historical/1.0', 'Accept-Encoding': 'identity'}
            request_headers.update(headers or {})
            request = Request(url, headers=request_headers)
            response = self.opener.open(request, timeout=min(self.timeout, self._remaining(deadline, host)))
            self._remaining(deadline, host)
            status = response.status
            if response.geturl() != url:
                raise HistoricalSearchError('Historical retrieval response destination changed.',
                                            code='redirect_rejected', status=status, host=host)
            if status != 200:
                raise _HttpFailure(self._status_error(status, host), self._retry_after(response.headers))
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            content_length = response_headers.get('content-length', '')
            if content_length.isdigit() and int(content_length) > MAX_BYTES:
                raise HistoricalSearchError('Historical retrieval response exceeds the size limit.',
                                            code='response_too_large', host=host)
            chunks, length = [], 0
            reader = getattr(response, 'read1', response.read)
            while True:
                remaining = self._remaining(deadline, host)
                raw = getattr(getattr(response, 'fp', None), 'raw', None)
                connection_socket = getattr(raw, '_sock', None)
                if connection_socket is not None:
                    connection_socket.settimeout(min(self.timeout, remaining))
                chunk = reader(min(64 * 1024, MAX_BYTES + 1 - length))
                self._remaining(deadline, host)
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > MAX_BYTES:
                    raise HistoricalSearchError('Historical retrieval response exceeds the size limit.',
                                                code='response_too_large', host=host)
            return HttpResponse(b''.join(chunks), response_headers, url, status)
        except HTTPError as error:
            try:
                raise _HttpFailure(self._status_error(error.code, host), self._retry_after(error.headers)) from None
            finally:
                error.close()
        except (ssl.SSLCertVerificationError, ssl.CertificateError):
            raise HistoricalSearchError('Historical retrieval TLS verification failed.',
                                        code='tls_rejected', host=host, failure_kind='tls') from None
        except (URLError, OSError, http.client.HTTPException) as error:
            if isinstance(getattr(error, 'reason', None), (ssl.SSLCertVerificationError, ssl.CertificateError)):
                raise HistoricalSearchError('Historical retrieval TLS verification failed.',
                                            code='tls_rejected', host=host, failure_kind='tls') from None
            raise _HttpFailure(HistoricalSearchError(
                'Historical retrieval transport failed.', code='transport_failure',
                retryable=True, operational=True, host=host,
                failure_kind=_failure_kind(error))) from None
        finally:
            if response is not None:
                response.close()

    def _attempt(self, url, host, headers, attempt, deadline):
        try:
            slot = self.coordinator.slot(host, deadline=deadline) if self.coordinator else nullcontext()
            with slot:
                try:
                    return _Outcome(response=self._request(url, host, headers, deadline))
                except _HttpFailure as failure:
                    backoff = min(10.0, 5.0 * 2 ** (attempt - 1))
                    delay = max(backoff, failure.retry_after or 0.0) if failure.error.retryable else 0.0
                    # A failed individual connection must not stall other jobs
                    # or unrelated URLs on this provider. Only provider-origin
                    # cooldown signals justify extending the shared host timer.
                    provider_busy = failure.error.status in (429, 503)
                    if self.coordinator and (provider_busy or failure.retry_after is not None):
                        shared_delay = max(backoff if provider_busy else 0.0, failure.retry_after or 0.0)
                        self.coordinator.defer(host, shared_delay, deadline=deadline)
                    return _Outcome(error=failure.error, delay=delay)
        except HistoricalSearchError as error:
            return _Outcome(error=error)
        except RateLimitError as error:
            code = error.code if error.code in ('deadline_exceeded', 'rate_limit_state', 'rate_limit_policy') else 'rate_limit_state'
            return _Outcome(error=HistoricalSearchError('Historical retrieval coordination failed.',
                            code=code, operational=True, host=host))

    def _bounded_attempt(self, url, host, headers, attempt, deadline):
        remaining = self._remaining(deadline, host)
        if not self._workers.acquire(timeout=remaining):
            raise HistoricalSearchError('Historical retrieval workers remain occupied.',
                                        code='deadline_exceeded', operational=True, host=host)
        outcomes = queue.Queue(maxsize=1)

        def run():
            try:
                outcomes.put(self._attempt(url, host, headers, attempt, deadline))
            except Exception:
                outcomes.put(_Outcome(error=HistoricalSearchError(
                    'Historical retrieval transport could not complete.',
                    code='transport_internal', operational=True, host=host)))
            finally:
                self._workers.release()

        try:
            threading.Thread(target=run, name='historical-http', daemon=True).start()
        except Exception:
            self._workers.release()
            raise HistoricalSearchError('Historical retrieval worker could not start.',
                                        code='transport_internal', operational=True, host=host) from None
        try:
            result = outcomes.get(timeout=self._remaining(deadline, host))
        except queue.Empty:
            raise HistoricalSearchError('Historical retrieval deadline was exhausted.',
                                        code='deadline_exceeded', operational=True, host=host) from None
        self._remaining(deadline, host)
        return result

    def get(self, url, headers=None):
        if self._deadline.get() is None:
            with self.operation():
                return self.get(url, headers=headers)
        host, deadline = self._host(url), self._deadline.get()
        for attempt in range(1, self.retries + 2):
            self._remaining(deadline, host)
            self._emit('http_attempt', host=host, attempt=attempt)
            try:
                outcome = self._bounded_attempt(url, host, headers, attempt, deadline)
            except HistoricalSearchError as error:
                outcome = _Outcome(error=error)
            if outcome.error is None:
                self._emit('http_success', host=host, attempt=attempt, status=outcome.response.status)
                return outcome.response
            error = outcome.error
            self._emit('http_failure', host=host, attempt=attempt, code=error.code,
                       status=error.status, retryable=error.retryable, failure_kind=error.failure_kind)
            if not error.retryable or attempt > self.retries:
                raise error from None
            self._emit('http_retry', host=host, attempt=attempt, code=error.code,
                       status=error.status, retryable=True, delay_seconds=outcome.delay,
                       failure_kind=error.failure_kind)
            if outcome.delay >= self._remaining(deadline, host):
                raise HistoricalSearchError('Historical retrieval cooldown exceeds the operation deadline.',
                                            code='deadline_exceeded', operational=True, host=host)
            self.sleeper(outcome.delay)
        raise AssertionError('Unreachable historical transport state.')
