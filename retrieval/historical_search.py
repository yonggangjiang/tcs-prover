"""Independent, fail-closed historical retrieval MCP server.

The controller fixes the cutoff and policy before launch. The model sees only
``search(query)``, full-text ``open(url)``, and literal ``find(url, pattern)``;
it cannot supply dates, provider settings,
credentials, paths, or claimed provenance. No source website is ever fetched.

``brave_wayback`` uses today's Brave index ONLY to discover candidate URLs, then
retrieves identity-mode Wayback captures. Every returned byte must match the
archive's CDX payload digest, and its Memento datetime must match the CDX capture
strictly before the cutoff. Modern titles, snippets, summaries, and rankings are
never forwarded. Modern candidate selection still differs from historical search;
this is NOT a reconstruction of the original search service or model knowledge.

``duckduckgo_wayback`` instead discovers URLs using DuckDuckGo's official public
Lite page, with no credential. It parses only result-link hrefs and locally
unwraps DuckDuckGo redirect parameters, never following those redirects. It
rejects challenges/errors without retrying another endpoint or bypassing them.
The same mandatory archive evidence gate applies to every returned result.

``scholarly_wayback`` explicitly uses the arXiv and Crossref public APIs for
URL-only discovery. Shared controller-owned pacing covers all broker processes.
It is an explicit provider, not an automatic challenge fallback.

``codex_wayback`` uses a separate protected Codex search session to suggest
candidate URLs. Its URL-only structured output is untrusted discovery, never
archive evidence. Current snippets, titles, reasoning and answers remain outside
the solver. Every candidate still crosses the identical archive evidence gate.

``sealed_corpus`` accepts only a controller-pinned, HMAC-authenticated manifest
of archive captures. The controller's key, policy and manifest must be outside
the author's writable/readable boundary. HMAC authenticates controller ingestion,
not the archive's truthfulness. Python path checks do not provide OS isolation.

Primary protocol references:
https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server
https://api-dashboard.search.brave.com/app/documentation/web-search/query
https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript
https://modelcontextprotocol.io/specification/2025-03-26/basic/transports
"""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from html.parser import HTMLParser
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import uuid
import zlib
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, unquote, parse_qs

if __name__ == '__main__' and not __package__:
    # Codex launches the broker by path; import its siblings from the package.
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from retrieval.historical_transport import (ALLOWED_NETWORK_HOSTS, MAX_BYTES, HistoricalSearchError,
                                            HttpResponse, NoRedirect, RestrictedHttpClient)

UTC = timezone.utc
MAX_PROTOCOL_LINE = 64 * 1024
TEXT_MIMES = frozenset({'text/html', 'application/xhtml+xml', 'text/plain', 'application/pdf'})
LIMITATION = ('Historical-content approximation: current search ranking and any discovery model '
              'influence candidate selection; archive coverage and model knowledge differ from the original experiment.')


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_cutoff(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise HistoricalSearchError('Cutoff must be an explicit UTC ISO timestamp.')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)
    except ValueError:
        raise HistoricalSearchError('Invalid UTC timestamp.') from None


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def capture_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r'\d{14}', value):
        raise HistoricalSearchError('Archive capture has no valid timestamp.')
    try:
        return datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=UTC)
    except ValueError:
        raise HistoricalSearchError('Archive capture has no valid timestamp.') from None


def canonical_url(value: str) -> str:
    """No local names, credentials, custom ports, archive wrappers, or URL tricks."""
    if not isinstance(value, str) or len(value) > 8192 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        raise HistoricalSearchError('Only public HTTP(S) source URLs are accepted.')
    if re.search(r'[\x00-\x1f\x7f]', unquote(value)):
        raise HistoricalSearchError('URL contains encoded control characters.')
    try:
        parts = urlsplit(value)
        port, host = parts.port, parts.hostname
        if parts.scheme not in ('http', 'https') or not host or parts.username or parts.password:
            raise ValueError
        if port not in (None, 80 if parts.scheme == 'http' else 443):
            raise ValueError
        host = host.encode('idna').decode('ascii').lower().rstrip('.')
        if host in ALLOWED_NETWORK_HOSTS or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                raise ValueError
            if all(label.isdigit() for label in host.split('.')) or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.')):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = '[' + host + ']'
        return urlunsplit((parts.scheme, host, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        raise HistoricalSearchError('Only public HTTP(S) source URLs are accepted.') from None


class ArchivedText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.title: list[str] = []
        self.ignored: list[str] = []
        self.in_title = False
        self.challenge_widget = False
        self.refresh_redirect = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.ignored:
            self.challenge_widget |= 'g-recaptcha' in (attributes.get('class') or '').casefold().split()
            self.refresh_redirect |= (tag == 'meta'
                                      and (attributes.get('http-equiv') or '').casefold() == 'refresh'
                                      and 'url=' in (attributes.get('content') or '').casefold().replace(' ', ''))
        if tag in ('script', 'style', 'noscript', 'template', 'iframe', 'object', 'svg'):
            self.ignored.append(tag)
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in self.ignored:
            del self.ignored[len(self.ignored) - 1 - self.ignored[::-1].index(tag):]
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if not self.ignored:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)


def well_formed_text(value: str) -> str:
    """Rejoin split surrogate pairs and replace lone surrogates with U+FFFD."""
    return value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace')


def pdf_supported() -> bool:
    try:
        import pypdf
        version = tuple(int(part) for part in pypdf.__version__.split('.')[:3])
        return (6, 20, 1) <= version < (7,) and hasattr(pypdf, 'apply_configuration')
    except (ImportError, ValueError, AttributeError):
        return False


def extract_pdf(body: bytes) -> tuple[str, str]:
    """Decode verified bytes only; never follow links, attachments, or PDF actions.

    Requires pypdf>=6.20.1,<7 for its public resource-limit configuration. Large,
    encrypted, image-only and malformed documents fail closed; no OCR fallback.
    """
    if not pdf_supported():
        raise HistoricalSearchError('Verified PDF requires the optional pypdf>=6.20.1,<7 dependency.')
    from pypdf import PdfReader, apply_configuration
    try:
        with apply_configuration(maximum_declared_stream_length=MAX_BYTES,
                                 array_based_stream_maximum_output_length=MAX_BYTES,
                                 jbig2_maximum_output_length=MAX_BYTES,
                                 lzw_maximum_output_length=MAX_BYTES,
                                 run_length_maximum_output_length=MAX_BYTES,
                                 zlib_maximum_output_length=MAX_BYTES,
                                 brotli_maximum_output_length=MAX_BYTES,
                                 zlib_maximum_recovery_input_length=100000,
                                 image_maximum_buffer_size=MAX_BYTES,
                                 stream_decoding_work_maximum_length=MAX_BYTES * 4,
                                 stream_filters_maximum_length=4,
                                 page_tree_maximum_entries=10000,
                                 page_tree_maximum_depth=50,
                                 xform_maximum_invocations_per_extraction=100,
                                 jbig2dec_binary=None, disable_legacy_handling=True):
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted or len(reader.pages) > 250:
                raise HistoricalSearchError('Encrypted or oversized PDF cannot be extracted.')
            # pypdf can decode malformed text maps into lone surrogates, which
            # no UTF-8 reply, digest or audit can carry. Offsets follow this text.
            title = well_formed_text(str(reader.metadata.title or '')) if reader.metadata else ''
            pages = []
            stream_bytes = 0
            for page in reader.pages:
                contents = page.get_contents()
                stream_bytes += len(contents.get_data()) if contents is not None else 0
                if stream_bytes > MAX_BYTES * 4:
                    raise HistoricalSearchError('PDF content exceeds the extraction resource limit.')
                pages.append(well_formed_text(page.extract_text()))
                if sum(len(item) for item in pages) > 1000000:
                    raise HistoricalSearchError('PDF text exceeds the extraction resource limit.')
            content = '\n\n'.join(pages).strip()
            if not content:
                raise HistoricalSearchError('Verified PDF has no extractable text; OCR is not enabled.')
            return title[:500], content
    except HistoricalSearchError:
        raise
    except Exception:
        raise HistoricalSearchError('Verified PDF extraction failed closed.') from None


def decode_verified_payload(body: bytes, content_encoding: str) -> bytes:
    """Decode only after authenticating raw payload bytes against CDX.

    Archives can preserve either a source gzip payload or a stale gzip header
    after decoding it. Require both gzip magic and its header before decoding;
    reject malformed, concatenated, or oversized streams without fallback.
    """
    encoding = content_encoding.lower().strip()
    if encoding not in ('', 'identity', 'gzip', 'x-gzip'):
        raise HistoricalSearchError('Archived content encoding is not supported.')
    if not body.startswith(b'\x1f\x8b'):
        return body
    if encoding not in ('gzip', 'x-gzip'):
        raise HistoricalSearchError('Archived gzip payload has no matching encoding header.')
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(body, MAX_BYTES + 1)
        if (len(decoded) > MAX_BYTES or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise HistoricalSearchError('Archived gzip payload exceeds limits or is incomplete.')
        return decoded
    except zlib.error:
        raise HistoricalSearchError('Archived gzip payload could not be decoded.') from None


def extract_text(body: bytes, mime_type: str, content_encoding: str = 'identity') -> tuple[str, str]:
    body = decode_verified_payload(body, content_encoding)
    if mime_type == 'application/pdf':
        return extract_pdf(body)
    # Conservative encoding: the byte integrity gate precedes any decoding.
    decoded = body.decode('utf-8', errors='replace')
    if mime_type == 'text/plain':
        return '', decoded.strip()
    parser = ArchivedText()
    try:
        parser.feed(decoded)
    except (AssertionError, ValueError):
        # CPython's parser asserts on some malformed marked sections. Reject
        # only this capture so older captures and other candidates still run.
        raise HistoricalSearchError('Archived HTML could not be parsed.', code='unreadable_capture') from None
    title = ' '.join(' '.join(parser.title).split())[:500]
    content = ' '.join(' '.join(parser.text).split())
    # These observed archive records authenticate correctly but contain only
    # an access challenge or redirect stub. Inspect inert HTML structure and
    # exact boilerplate; papers discussing CAPTCHA/redirects remain readable.
    challenge_boilerplate = (
        'arxiv recaptcha',
        'arxiv recaptcha we gratefully acknowledge support from the simons '
        'foundation and member institutions. web accessibility assistance',
    )
    challenge = (title.casefold() == 'arxiv recaptcha' and parser.challenge_widget
                 and content.casefold() in challenge_boilerplate)
    redirect = (title.casefold() == 'redirecting' and parser.refresh_redirect
                and content.casefold() in ('redirecting', 'redirecting redirecting'))
    if challenge or redirect:
        raise HistoricalSearchError('Verified archive contains only an access or redirect placeholder.',
                                    code='unusable_capture')
    return title, content


def unfolded_offset(text: str, folded_offset: int) -> int:
    """Map an offset in text.casefold() to the character of text producing it.

    Case folding can lengthen characters (the ligature U+FB01 folds to 'fi'),
    so folded offsets cannot slice the original text directly.
    """
    index = consumed = 0
    while index < len(text):
        chunk = text[index:index + 4096]
        size = len(chunk.casefold())  # Folding is per character, without context.
        if consumed + size <= folded_offset:
            consumed += size
            index += len(chunk)
            continue
        for character in chunk:
            consumed += len(character.casefold())
            if consumed > folded_offset:
                return index
            index += 1
    return len(text)


@dataclass(frozen=True)
class Snapshot:
    url: str
    capture: str
    archive_url: str
    mime_type: str
    cdx_digest: str
    memento_datetime: str
    body: bytes
    body_sha256: str
    content_encoding: str = 'identity'

    def validate(self, cutoff: datetime) -> None:
        url = canonical_url(self.url)
        captured = capture_datetime(self.capture)
        if captured >= cutoff:
            raise HistoricalSearchError('Capture is at or after the fixed cutoff.')
        expected_archive = f'https://web.archive.org/web/{self.capture}id_/{url}'
        if self.url != url or self.archive_url != expected_archive:
            raise HistoricalSearchError('Archive URL or source identity does not match the evidence.')
        try:
            memento = parsedate_to_datetime(self.memento_datetime)
        except (TypeError, ValueError, OverflowError):
            raise HistoricalSearchError('Missing or invalid archive Memento datetime.') from None
        if memento.tzinfo is None or memento.astimezone(UTC) != captured:
            raise HistoricalSearchError('Archive Memento datetime does not match the capture.')
        if self.mime_type not in TEXT_MIMES:
            raise HistoricalSearchError('Only archived HTML, plain text and supported PDF are accepted.')
        if not isinstance(self.content_encoding, str):
            raise HistoricalSearchError('Archive content encoding is invalid.')
        if not self.body or len(self.body) > MAX_BYTES or sha256(self.body) != self.body_sha256:
            raise HistoricalSearchError('Archived bytes failed their SHA256 integrity check.')
        digest = base64.b32encode(hashlib.sha1(self.body).digest()).decode('ascii')
        if not re.fullmatch(r'[A-Z2-7]{32}', self.cdx_digest) or not hmac.compare_digest(digest, self.cdx_digest):
            raise HistoricalSearchError('Archived bytes do not match the trusted CDX payload digest.')

    def document(self, cutoff: datetime) -> tuple[dict, str]:
        """Extract the full verified document; navigation never truncates it."""
        self.validate(cutoff)
        title, content = extract_text(self.body, self.mime_type, self.content_encoding)
        if not content:
            raise HistoricalSearchError('Verified capture has no extractable text.')
        metadata = {'url': self.url, 'title': title, 'capture_time': utc_text(capture_datetime(self.capture)),
                    'archive_url': self.archive_url, 'sha256': self.body_sha256,
                    'untrusted_source_content': True}
        return metadata, content

    def result(self, cutoff: datetime, limit: int, query: str | None = None) -> dict:
        # Every output, including a cached result, crosses the same evidence gate.
        metadata, content = self.document(cutoff)
        if query is not None:
            words = re.findall(r'\w+', query.casefold())
            folded = content.casefold()
            positions = [folded.find(word) for word in words if len(word) > 2]
            positions = [position for position in positions if position >= 0]
            start = max(0, unfolded_offset(content, min(positions)) - 120) if positions else 0
            content = content[start:start + min(limit, 1200)]
        else:
            content = content[:limit]
        return {**metadata, 'text': content}


class AuditLog:
    _write_lock = threading.Lock()

    def __init__(self, path: Path, cutoff: datetime, provider: str):
        self.path = path
        self.operation_id = None
        self.cutoff, self.provider = utc_text(cutoff), provider
        self.write('startup', status='ready')

    def write(self, event: str, **fields):
        record = {'event': event, 'time': utc_text(datetime.now(UTC)), 'cutoff': self.cutoff,
                  'provider': self.provider, **fields}
        if self.operation_id is not None and 'operation_id' not in record:
            record['operation_id'] = self.operation_id
        try:
            with self._write_lock, self.path.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise HistoricalSearchError('Audit logging failed; retrieval is disabled.',
                                        code='audit_failure', operational=True) from None


class WaybackProvider:
    def __init__(self, cutoff: datetime, token: str, client: RestrictedHttpClient, max_candidates: int):
        self.cutoff, self.token, self.client, self.max_candidates = cutoff, token, client, max_candidates
        self.cache: OrderedDict[tuple[str, str], Snapshot] = OrderedDict()
        self.negative_cache: OrderedDict[tuple[str, str], float] = OrderedDict()
        # Guards compound updates of the bounded in-memory caches, which calls
        # may share. Never held during network, extraction or persistent I/O.
        self.cache_lock = threading.Lock()
        self.persistent_cache = None
        self.audit = None

    def share_memory_caches(self, source):
        """Reuse another call's bounded in-memory caches, never its request state.

        Clients, audit, deadlines, discovery status and the persistent cache
        stay per call. Every cached snapshot is still revalidated before use.
        """
        if type(source) is not type(self) or source.cutoff != self.cutoff:
            return
        self.cache_lock, self.cache, self.negative_cache = source.cache_lock, source.cache, source.negative_cache
        if hasattr(source, 'discovery_cache'):
            self.discovery_cache = source.discovery_cache

    def _cached_capture(self, url):
        if self.persistent_cache is None:
            return None
        from retrieval.historical_cache import CacheError
        try:
            record = self.persistent_cache.get(url)
            if record is None:
                return None
            record = dict(record)
            record['body'] = base64.b64decode(record['body'], validate=True)
            snapshot = Snapshot(**record)
            # A shared cache can contain evidence from another fixed cutoff.
            # Check its integrity first, then apply this caller's strict date.
            snapshot.validate(datetime.max.replace(tzinfo=UTC))
            if capture_datetime(snapshot.capture) >= self.cutoff:
                return None
            snapshot.validate(self.cutoff)
            if not extract_text(snapshot.body, snapshot.mime_type, snapshot.content_encoding)[1]:
                raise ValueError
        except HistoricalSearchError as error:
            if error.code == 'unusable_capture':
                return None
            raise HistoricalSearchError('Protected archive cache failed validation.', code='cache_invalid') from None
        except (CacheError, ValueError, TypeError):
            raise HistoricalSearchError('Protected archive cache failed validation.', code='cache_invalid') from None
        if self.audit is not None:
            self.audit('archive_cache_hit', url_sha256=sha256(url.encode()),
                       capture=utc_text(capture_datetime(snapshot.capture)), body_sha256=snapshot.body_sha256)
        return snapshot

    def _newer_usable_capture(self, snapshot):
        """Whether the shared entry already holds a newer readable capture.

        Jobs with different cutoffs share URL-keyed entries. An earlier-cutoff
        job must not replace a later-cutoff job's newer capture, or that job's
        digest-pinned reads change. Any doubt leaves the entry replaceable.
        """
        try:
            record = self.persistent_cache.get(snapshot.url)
            if record is None or capture_datetime(record['capture']) <= capture_datetime(snapshot.capture):
                return False
            existing = Snapshot(**{**record, 'body': base64.b64decode(record['body'], validate=True)})
            existing.validate(datetime.max.replace(tzinfo=UTC))
            return bool(extract_text(existing.body, existing.mime_type, existing.content_encoding)[1])
        except Exception:
            return False

    def _store_capture(self, snapshot):
        if self.persistent_cache is None:
            return
        from retrieval.historical_cache import CacheError
        snapshot.validate(self.cutoff)
        if self._newer_usable_capture(snapshot):
            return
        record = {**snapshot.__dict__, 'body': base64.b64encode(snapshot.body).decode('ascii')}
        try:
            self.persistent_cache.put(snapshot.url, record)
        except CacheError:
            raise HistoricalSearchError('Protected archive cache could not be updated.',
                                        code='cache_unavailable', operational=True) from None

    @staticmethod
    def _json(response: HttpResponse, expected_url: str):
        if response.status != 200 or response.url != expected_url:
            raise HistoricalSearchError('Redirected or unsuccessful retrieval was rejected.')
        try:
            return json.loads(response.body)
        except (ValueError, UnicodeError):
            raise HistoricalSearchError('Provider returned invalid JSON.',
                                        code='provider_response_invalid', operational=True) from None

    def candidates(self, query: str) -> list[str]:
        # Date metadata is a discovery hint only. It NEVER authorizes returned content.
        parameters = {'q': query, 'count': self.max_candidates, 'extra_snippets': 'false',
                      'text_decorations': 'false', 'result_filter': 'web',
                      'freshness': '1900-01-01to' + (self.cutoff - timedelta(microseconds=1)).date().isoformat()}
        endpoint = 'https://api.search.brave.com/res/v1/web/search?' + urlencode(parameters)
        value = self._json(self.client.get(endpoint, {'X-Subscription-Token': self.token}), endpoint)
        try:
            rows = value['web']['results']
            if not isinstance(rows, list):
                raise TypeError
        except (KeyError, TypeError):
            raise HistoricalSearchError('Discovery provider returned an invalid result list.') from None
        urls = set()
        for row in rows[:self.max_candidates]:
            try:
                urls.add(canonical_url(row['url']))
            except (HistoricalSearchError, KeyError, TypeError):
                continue
        # Never retain provider titles, snippets, summaries, or relative ranking.
        return sorted(urls)

    def open(self, url: str) -> Snapshot:
        url = canonical_url(url)
        key = (utc_text(self.cutoff), url)
        with self.cache_lock:
            snapshot = self.cache.get(key)
        if snapshot is not None:
            snapshot.validate(self.cutoff)
            try:
                extract_text(snapshot.body, snapshot.mime_type, snapshot.content_encoding)
            except HistoricalSearchError as error:
                if error.code != 'unusable_capture':
                    raise
                with self.cache_lock:
                    if self.cache.get(key) is snapshot:
                        del self.cache[key]
            else:
                with self.cache_lock:
                    if key in self.cache:
                        self.cache.move_to_end(key)
                return snapshot
        cached = self._cached_capture(url)
        if cached is not None:
            with self.cache_lock:
                self.cache[key] = cached
                while len(self.cache) > 16:
                    self.cache.popitem(last=False)
            return cached
        with self.cache_lock:
            absent = self.negative_cache.get(key, 0) > time.monotonic()
        if absent:
            raise HistoricalSearchError(
                "No verifiable historical capture is available (short-lived cached absence).",
                code="no_capture")
        # CDX 'to' is inclusive, so exclude equality independently below as well.
        latest = (self.cutoff - timedelta(microseconds=1)).strftime('%Y%m%d%H%M%S')
        parameters = {'url': url, 'matchType': 'exact', 'output': 'json', 'gzip': 'false',
                      'fl': 'timestamp,original,mimetype,statuscode,digest', 'to': latest,
                      'filter': 'statuscode:200', 'limit': '-5'}
        endpoint = 'https://web.archive.org/cdx/search/cdx?' + urlencode(parameters)
        rows = self._json(self.client.get(endpoint), endpoint)
        fields = ['timestamp', 'original', 'mimetype', 'statuscode', 'digest']
        if not isinstance(rows, list) or (rows and rows[0] != fields):
            raise HistoricalSearchError('Archive index response has an invalid schema.',
                                        code='provider_response_invalid', operational=True)
        if not rows:
            raise HistoricalSearchError('No verifiable historical capture is available.', code='no_capture')
        accepted = []
        for row in rows[1:]:
            if not isinstance(row, list) or len(row) != len(fields) or not all(isinstance(value, str) for value in row):
                continue
            record = dict(zip(fields, row))
            try:
                if (capture_datetime(record['timestamp']) < self.cutoff
                        and canonical_url(record['original']) == url
                        and record['statuscode'] == '200'
                        and record['mimetype'] in TEXT_MIMES
                        and re.fullmatch(r'[A-Z2-7]{32}', record['digest'])):
                    accepted.append(record)
            except HistoricalSearchError:
                continue
        last_operational_error = None
        unusable_digests = set()
        for record in sorted(accepted, key=lambda item: item['timestamp'], reverse=True):
            if record['digest'] in unusable_digests:
                continue
            archive_url = f"https://web.archive.org/web/{record['timestamp']}id_/{url}"
            try:
                response = self.client.get(archive_url)
                if response.status != 200 or response.url != archive_url:
                    raise HistoricalSearchError('Redirected archive retrieval was rejected.')
                # Replays can retain the source Content-Encoding header even
                # when their delivered bytes are already decoded. Verify the
                # exact bytes against CDX rather than trusting that header.
                # Decompression, if needed, happens only after validation.
                snapshot = Snapshot(url, record['timestamp'], archive_url, record['mimetype'],
                                    record['digest'], response.headers.get('memento-datetime', ''),
                                    response.body, sha256(response.body),
                                    response.headers.get('content-encoding', 'identity'))
                snapshot.validate(self.cutoff)
                # An unreadable newest capture must not mask an older readable one.
                try:
                    extracted = extract_text(snapshot.body, snapshot.mime_type, snapshot.content_encoding)
                except HistoricalSearchError as error:
                    if error.code == 'unusable_capture':
                        # Only authenticated, positively identified placeholder
                        # bytes justify skipping an identical older digest.
                        unusable_digests.add(snapshot.cdx_digest)
                    raise
                if not extracted[1]:
                    raise HistoricalSearchError('Verified capture has no extractable text.', code='unreadable_capture')
            except HistoricalSearchError as error:
                if error.code == 'audit_failure':
                    raise
                if error.operational:
                    last_operational_error = error
                if error.code == 'deadline_exceeded':
                    break
                continue
            self._store_capture(snapshot)
            with self.cache_lock:
                self.cache[key] = snapshot
                while len(self.cache) > 16:
                    self.cache.popitem(last=False)
            return snapshot
        if last_operational_error is not None:
            raise last_operational_error
        with self.cache_lock:
            self.negative_cache[key] = time.monotonic() + 60
            while len(self.negative_cache) > 64:
                self.negative_cache.popitem(last=False)
        raise HistoricalSearchError('No verifiable historical capture is available.', code='no_capture')


class DuckDuckGoResultLinks(HTMLParser):
    """Read only link attributes, never retain search titles/snippets/answers."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.challenge = False
        self.no_results = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        identifier = attrs.get('id') or ''
        if (identifier in ('challenge-form', 'img-form', 'captcha')
                or any(value.startswith('anomaly-') for value in classes)
                or (tag == 'form' and '/anomaly.js' in (attrs.get('action') or ''))):
            self.challenge = True
        if any(value.startswith('no-results') for value in classes):
            self.no_results = True
        if tag == 'a' and 'result-link' in classes and isinstance(attrs.get('href'), str):
            self.links.append(attrs['href'])


def duckduckgo_source_url(href: str) -> str:
    """Decode a known wrapper locally; no network request to redirect endpoints."""
    if href.startswith('//'):
        href = 'https:' + href
    parsed = urlsplit(href)
    if parsed.hostname in ('duckduckgo.com', 'www.duckduckgo.com', 'lite.duckduckgo.com', 'html.duckduckgo.com'):
        if (parsed.scheme != 'https' or parsed.hostname not in ('duckduckgo.com', 'www.duckduckgo.com')
                or parsed.path != '/l/' or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise HistoricalSearchError('Unsupported search redirect wrapper.')
        targets = parse_qs(parsed.query).get('uddg', [])
        if len(targets) != 1:
            raise HistoricalSearchError('Search redirect has no unambiguous source URL.')
        href = targets[0]
    result = canonical_url(href)
    # Nested wrappers/search pages are never candidate source documents.
    if urlsplit(result).hostname in ('duckduckgo.com', 'www.duckduckgo.com', 'lite.duckduckgo.com', 'html.duckduckgo.com'):
        raise HistoricalSearchError('Nested search wrappers are not source documents.')
    return result


class DuckDuckGoWaybackProvider(WaybackProvider):
    """Credential-free URL discovery; retains WaybackProvider.open unchanged."""
    def __init__(self, cutoff: datetime, client: RestrictedHttpClient, max_candidates: int):
        super().__init__(cutoff, '', client, max_candidates)

    def candidates(self, query: str) -> list[str]:
        endpoint = 'https://lite.duckduckgo.com/lite/?' + urlencode({'q': query})
        response = self.client.get(endpoint)
        content_type = response.headers.get('content-type', '').split(';')[0].lower().strip()
        if response.status != 200 or response.url != endpoint or content_type != 'text/html':
            raise HistoricalSearchError('Public search discovery unavailable; no alternative endpoint is permitted.')
        parser = DuckDuckGoResultLinks()
        try:
            parser.feed(response.body.decode('utf-8', errors='replace'))
        except (AssertionError, ValueError, UnicodeError):
            raise HistoricalSearchError('Public search markup could not be validated.') from None
        if parser.challenge:
            raise HistoricalSearchError('Public search presented a challenge; retrieval stopped without bypass.')
        if not parser.links and not parser.no_results:
            raise HistoricalSearchError('Public search result markup was not recognized.')
        candidates = set()
        for href in parser.links[:self.max_candidates]:
            try:
                candidates.add(duckduckgo_source_url(href))
            except (HistoricalSearchError, ValueError):
                continue
        # Ranking is recomputed later using only verified historical text.
        # Today's ranking still influences which candidates enter this set.
        return sorted(candidates)


class ScholarlyWaybackProvider(WaybackProvider):
    """Explicit scholarly discovery through public APIs, with the same archive gate."""
    def __init__(self, cutoff, client, max_candidates):
        super().__init__(cutoff, '', client, max_candidates)
        self.discovery_cache = OrderedDict()

    def candidates(self, query):
        from retrieval.scholarly_discovery import discover
        self.discovery_incomplete = False
        key = sha256(query.encode('utf-8'))
        with self.cache_lock:
            cached = self.discovery_cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                self.discovery_cache.move_to_end(key)
                return list(cached[1])
        def failed(fields):
            self.discovery_incomplete = True
            callback = getattr(self.client, 'audit', None)
            if callback is not None:
                try:
                    callback('discovery_provider_failed', **fields)
                except Exception:
                    raise HistoricalSearchError('Historical discovery audit could not be recorded.',
                                                code='audit_failure', operational=True) from None

        urls = discover(query, self.cutoff, self.max_candidates, self.client,
                        HistoricalSearchError, canonical_url, on_failure=failed)
        with self.cache_lock:
            if not self.discovery_incomplete:
                self.discovery_cache[key] = (time.monotonic() + 300, tuple(urls))
            while len(self.discovery_cache) > 64:
                self.discovery_cache.popitem(last=False)
        return urls


class CodexWaybackProvider(WaybackProvider):
    """Untrusted URL suggestions from a separate controller-owned search session."""

    def __init__(self, cutoff, client, max_candidates, settings, audit):
        super().__init__(cutoff, '', client, max_candidates)
        self.discovery_settings = dict(settings)
        self.audit = audit
        self.discovery_deadline = None

    def candidates(self, query):
        from retrieval.historical_codex_discovery import DiscoveryError, discover
        try:
            urls = discover(query, self.cutoff, self.max_candidates, self.discovery_settings,
                            self.audit, deadline=self.discovery_deadline)
        except HistoricalSearchError as error:
            if error.code == 'audit_failure':
                raise
            raise HistoricalSearchError('Independent Codex discovery failed; no live content or fallback is returned.',
                                        code='discovery_unavailable', operational=True) from None
        except Exception as error:
            code = ('discovery_timeout' if isinstance(error, DiscoveryError)
                    and getattr(error, 'code', None) == 'discovery_timeout' else 'discovery_unavailable')
            raise HistoricalSearchError('Independent Codex discovery failed; no live content or fallback is returned.',
                                        code=code, operational=True) from None
        if not isinstance(urls, list) or len(urls) > self.max_candidates:
            raise HistoricalSearchError('Independent Codex discovery returned invalid URL candidates.',
                                        code='discovery_unavailable', operational=True)
        try:
            # Canonicalization is not provenance. Only exact archived captures
            # can authorize these URLs and their extracted content for release.
            return sorted({canonical_url(url) for url in urls})
        except (HistoricalSearchError, TypeError):
            raise HistoricalSearchError('Independent Codex discovery returned invalid URL candidates.',
                                        code='discovery_unavailable', operational=True) from None


def codex_discovery_settings(settings, workspace):
    """Keep discovery credentials, state and executable outside the author."""
    required = {'codex_path', 'codex_sha256', 'auth_home', 'state_root', 'model', 'timeout_seconds'}
    if not isinstance(settings, dict) or set(settings) != required:
        raise HistoricalSearchError('Codex discovery requires fixed controller settings.', code='discovery_policy')
    if (not isinstance(settings['codex_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', settings['codex_sha256'])
            or not isinstance(settings['model'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', settings['model'])
            or type(settings['timeout_seconds']) is not int or not 1 <= settings['timeout_seconds'] <= 300):
        raise HistoricalSearchError('Codex discovery settings are invalid.', code='discovery_policy')
    try:
        workspace = Path(workspace).resolve()
        for name in ('codex_path', 'auth_home', 'state_root'):
            value = settings[name]
            if not isinstance(value, str):
                raise ValueError
            path = Path(value)
            if not path.is_absolute() or path != path.resolve() or path == workspace or workspace in path.parents:
                raise ValueError
        from retrieval.historical_codex_discovery import validate_settings
        validate_settings(settings, workspace=workspace)
    except Exception:
        raise HistoricalSearchError('Codex discovery controller paths or authentication are invalid.',
                                    code='discovery_policy') from None
    return dict(settings)


class SealedCorpusProvider:
    """Offline controller-authenticated ingestion; no caller-supplied proof accepted."""
    def __init__(self, policy: dict, cutoff: datetime):
        self.cutoff = cutoff
        try:
            root = Path(policy['corpus_root']).resolve(strict=True)
            manifest_path = Path(policy['manifest_path']).resolve(strict=True)
            key_path = Path(policy['signing_key_path']).resolve(strict=True)
            if not root.is_dir() or not all(Path(policy[name]).is_absolute() for name in ('corpus_root', 'manifest_path', 'signing_key_path')):
                raise ValueError
            manifest_bytes = manifest_path.read_bytes()
            if not hmac.compare_digest(sha256(manifest_bytes), policy['manifest_sha256']):
                raise ValueError
            key = key_path.read_bytes()
            if len(key) < 32 or key_path.is_relative_to(root):
                raise ValueError
            manifest = json.loads(manifest_bytes)
            signature = manifest.pop('signature')
            if set(manifest) != {'schema_version', 'entries'} or manifest['schema_version'] != 1 or not isinstance(manifest['entries'], list):
                raise ValueError
            expected = hmac.new(key, canonical_json(manifest), hashlib.sha256).hexdigest()
            if not isinstance(signature, dict) or signature.get('algorithm') != 'hmac-sha256' or not hmac.compare_digest(expected, signature.get('value', '')):
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError):
            raise HistoricalSearchError('Corpus manifest pin/signature/trust boundary validation failed.') from None
        self.snapshots: dict[str, Snapshot] = {}
        for entry in manifest['entries']:
            try:
                expected_fields = {'url', 'capture', 'archive_url', 'mime_type', 'cdx_digest', 'memento_datetime', 'body_path', 'body_sha256'}
                if (not isinstance(entry, dict) or not expected_fields.issubset(entry)
                        or set(entry) - expected_fields - {'content_encoding'}):
                    raise ValueError
                body_path = (root / entry['body_path']).resolve(strict=True)
                if Path(entry['body_path']).is_absolute() or not body_path.is_relative_to(root):
                    raise ValueError
                snapshot = Snapshot(**{name: value for name, value in entry.items() if name != 'body_path'}, body=body_path.read_bytes())
                # Validate provenance/integrity even for excluded future records.
                snapshot.validate(datetime.max.replace(tzinfo=UTC))
                if capture_datetime(snapshot.capture) >= cutoff:
                    continue
                previous = self.snapshots.get(snapshot.url)
                if previous is None or previous.capture < snapshot.capture:
                    self.snapshots[snapshot.url] = snapshot
            except (HistoricalSearchError, OSError, TypeError, ValueError):
                raise HistoricalSearchError('Corpus entry provenance, path, or byte integrity validation failed.') from None

    def candidates(self, query: str) -> list[str]:
        return sorted(self.snapshots)

    def open(self, url: str) -> Snapshot:
        snapshot = self.snapshots.get(canonical_url(url))
        if snapshot is None:
            raise HistoricalSearchError('No verified capture exists in the sealed corpus.')
        snapshot.validate(self.cutoff)
        return snapshot


def seal_snapshots(snapshots: list[Snapshot], corpus_root: Path, signing_key: bytes) -> bytes:
    """Controller-only export of verified ingestion; signing key must stay external.

    Call with snapshots obtained from WaybackProvider.open. This helper is not an
    MCP tool. A controller that signs fabricated evidence violates the trust model.
    """
    if len(signing_key) < 32:
        raise HistoricalSearchError('Controller signing key must contain at least 32 bytes.')
    corpus_root.mkdir(parents=True, exist_ok=True)
    entries = []
    for snapshot in snapshots:
        snapshot.validate(datetime.max.replace(tzinfo=UTC))
        filename = snapshot.body_sha256 + '.bin'
        (corpus_root / filename).write_bytes(snapshot.body)
        entry = dict(snapshot.__dict__)
        del entry['body']
        entry['body_path'] = filename
        entries.append(entry)
    manifest = {'schema_version': 1, 'entries': sorted(entries, key=lambda item: (item['url'], item['capture']))}
    signature = hmac.new(signing_key, canonical_json(manifest), hashlib.sha256).hexdigest()
    manifest['signature'] = {'algorithm': 'hmac-sha256', 'value': signature}
    return canonical_json(manifest)


def bounded_int(policy: dict, name: str, default: int, low: int, high: int) -> int:
    value = policy.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise HistoricalSearchError('Policy contains an invalid numeric setting.')
    return value


class HistoricalSearchEngine:
    def __init__(self, policy_path: Path, cutoff: str, audit_log: Path,
                 expected_policy_sha256: str | None = None, client=None, environment=None):
        self.cutoff = parse_cutoff(cutoff)
        try:
            policy_bytes = policy_path.read_bytes()
            if expected_policy_sha256 is not None and not hmac.compare_digest(sha256(policy_bytes), expected_policy_sha256):
                raise HistoricalSearchError('Policy SHA256 pin does not match.')
            self.policy_sha256 = sha256(policy_bytes)
            policy = json.loads(policy_bytes)
        except (OSError, ValueError):
            raise HistoricalSearchError('Cannot read the controller policy.') from None
        if not isinstance(policy, dict) or policy.get('schema_version') != 1:
            raise HistoricalSearchError('Unsupported policy schema.')
        provider = policy.get('provider')
        common = {'schema_version', 'provider', 'max_results', 'max_content_chars', 'deny_url_prefixes'}
        network_options = {'max_candidates', 'request_timeout_seconds', 'operation_timeout_seconds',
                           'rate_limit_directory', 'solver_workspace'}
        specific = ({'credential_env'} | network_options if provider == 'brave_wayback'
                    else network_options if provider in ('duckduckgo_wayback', 'scholarly_wayback')
                    else {'discovery'} | network_options if provider == 'codex_wayback'
                    else {'corpus_root', 'manifest_path', 'manifest_sha256', 'signing_key_path'} if provider == 'sealed_corpus' else set())
        if provider not in ('brave_wayback', 'duckduckgo_wayback', 'scholarly_wayback', 'codex_wayback', 'sealed_corpus') or set(policy) - common - specific:
            raise HistoricalSearchError('Unknown provider or policy settings.')
        self.max_results = bounded_int(policy, 'max_results', 5, 1, 10)
        # This legacy policy field limits search excerpts only. Direct document
        # reads and literal navigation expose the full verified extraction.
        self.max_content_chars = bounded_int(policy, 'max_content_chars', 20000, 100, 50000)
        denied = policy.get('deny_url_prefixes', [])
        if not isinstance(denied, list) or len(denied) > 100:
            raise HistoricalSearchError('Invalid URL exclusion policy.')
        self.denied_prefixes = tuple(canonical_url(value) for value in denied)
        self.audit = AuditLog(audit_log, self.cutoff, provider)
        self.operation_timeout = bounded_int(policy, 'operation_timeout_seconds', 300, 10, 300)
        self.client = None
        if provider in ('brave_wayback', 'duckduckgo_wayback', 'scholarly_wayback', 'codex_wayback'):
            timeout = bounded_int(policy, 'request_timeout_seconds', 15, 1, 60)
            candidates = bounded_int(policy, 'max_candidates', 10, 1, 20)
            coordinator = None
            if provider in ('scholarly_wayback', 'codex_wayback') or 'rate_limit_directory' in policy or 'solver_workspace' in policy:
                from retrieval.historical_rate_limit import SharedHostLimiter, RateLimitError
                if not all(isinstance(policy.get(key), str) for key in ('rate_limit_directory', 'solver_workspace')):
                    raise HistoricalSearchError('Shared pacing requires fixed controller and solver paths.')
                try:
                    coordinator = SharedHostLimiter(Path(policy['rate_limit_directory']),
                                                    workspace_root=Path(policy['solver_workspace']))
                except RateLimitError:
                    raise HistoricalSearchError('Shared historical request coordination is invalid.',
                                                code='rate_limit_policy') from None
            self.client = client or RestrictedHttpClient(timeout, coordinator=coordinator, audit=self.audit.write)
            if provider == 'brave_wayback':
                env_name = policy.get('credential_env', 'BRAVE_SEARCH_API_KEY')
                if not isinstance(env_name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{2,100}', env_name):
                    raise HistoricalSearchError('Invalid credential environment variable name.')
                token = (os.environ if environment is None else environment).get(env_name, '')
                if not isinstance(token, str) or not token.strip() or re.search(r'[\r\n]', token):
                    raise HistoricalSearchError('The independent search credential is not configured.')
                self.provider = WaybackProvider(self.cutoff, token, self.client, candidates)
            elif provider == 'scholarly_wayback':
                self.provider = ScholarlyWaybackProvider(self.cutoff, self.client, candidates)
            elif provider == 'codex_wayback':
                settings = codex_discovery_settings(policy.get('discovery'), policy['solver_workspace'])
                self.provider = CodexWaybackProvider(self.cutoff, self.client, candidates, settings, self.audit.write)
            else:
                self.provider = DuckDuckGoWaybackProvider(self.cutoff, self.client, candidates)
            if coordinator is not None:
                from retrieval.historical_cache import CaptureCache, CacheError
                try:
                    self.provider.persistent_cache = CaptureCache(
                        Path(policy['rate_limit_directory']) / 'captures',
                        workspace_root=Path(policy['solver_workspace']))
                except CacheError:
                    raise HistoricalSearchError('Protected archive cache configuration is invalid.',
                                                code='cache_invalid') from None
                self.provider.audit = self.audit.write
        else:
            self.provider = SealedCorpusProvider(policy, self.cutoff)
        self.provider_name = provider

    def _operation(self):
        timeout = self.operation_timeout
        deadline = getattr(self, 'request_deadline', None)
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise HistoricalSearchError('Historical retrieval deadline was exhausted.',
                                            code='deadline_exceeded', operational=True)
        if isinstance(self.provider, CodexWaybackProvider):
            discovery_deadline = time.monotonic() + timeout
            self.provider.discovery_deadline = (min(discovery_deadline, deadline)
                                               if deadline is not None else discovery_deadline)
        if self.client is not None and hasattr(self.client, 'operation'):
            return self.client.operation(timeout_seconds=timeout)
        return nullcontext()

    @staticmethod
    def _failure(error):
        fields = {'failure_code': error.code, 'operational': error.operational}
        if error.status is not None:
            fields['http_status'] = error.status
        return fields

    def _output(self, snapshot: Snapshot, query: str | None = None) -> dict:
        self._check_url(snapshot.url)
        return snapshot.result(self.cutoff, self.max_content_chars, query)

    def _check_url(self, url: str) -> str:
        url = canonical_url(url)
        source = urlsplit(url)
        for prefix in self.denied_prefixes:
            denied = urlsplit(prefix)
            if source.hostname == denied.hostname and unquote(source.path).startswith(unquote(denied.path)):
                raise HistoricalSearchError('Source is excluded by the controller benchmark policy.')
        return url

    def search(self, query: str) -> dict:
        with self._operation():
            return self._search(query)

    def _search(self, query: str) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000 or re.search(r'[\x00-\x1f\x7f]', query):
            raise HistoricalSearchError('Query must be nonempty text of at most 2000 characters.')
        query_hash = sha256(query.encode('utf-8'))
        self.audit.write('search_requested', query_sha256=query_hash)
        terms = set(re.findall(r'\w+', query.casefold()))
        results = []
        unavailable = 0
        try:
            candidates = self.provider.candidates(query)
            for url in candidates:
                try:
                    self._check_url(url)
                    snapshot = self.provider.open(url)
                    result = self._output(snapshot, query)
                    title, content = extract_text(snapshot.body, snapshot.mime_type, snapshot.content_encoding)
                    text_terms = set(re.findall(r'\w+', (title + ' ' + content).casefold()))
                    score = len(terms & text_terms)
                except HistoricalSearchError as error:
                    if error.code in ('audit_failure', 'cache_invalid'):
                        raise
                    unavailable += int(error.operational)
                    self.audit.write('capture_rejected', url_sha256=sha256(url.encode()), **self._failure(error))
                    if error.code == 'deadline_exceeded':
                        break
                    continue
                # A logging failure must abort the entire request, not turn a
                # verified capture into a rejection while retaining its result.
                self.audit.write('capture_verified', url_sha256=sha256(url.encode()), capture=result['capture_time'], body_sha256=result['sha256'])
                if score:
                    results.append((score, result))
            results.sort(key=lambda item: (-item[0], item[1]['url']))
            selected = [item[1] for item in results[:self.max_results]]
            if not selected and unavailable:
                raise HistoricalSearchError('Archive services were unavailable for candidate documents; no verified result was returned.',
                                            code='archive_unavailable', operational=True)
            self.audit.write('search_completed', query_sha256=query_hash, returned=len(selected), unavailable_candidates=unavailable)
            response = {'cutoff': utc_text(self.cutoff), 'results': selected, 'limitation': LIMITATION}
            if unavailable or getattr(self.provider, 'discovery_incomplete', False):
                response.update(incomplete=True, warning='Some discovery or archive requests were unavailable; only verified results are shown.')
            return response
        except HistoricalSearchError as error:
            self.audit.write('search_failed', query_sha256=query_hash, **self._failure(error))
            raise

    def _document(self, url, expected_sha256):
        snapshot = self.provider.open(url)
        self._check_url(snapshot.url)
        if snapshot.url != url:
            raise HistoricalSearchError('Archive source identity changed.', code='capture_changed')
        metadata, content = snapshot.document(self.cutoff)
        if expected_sha256 is not None and not hmac.compare_digest(snapshot.body_sha256, expected_sha256):
            raise HistoricalSearchError('Archived document changed; reopen without a digest to select its current verified capture.',
                                        code='capture_changed')
        metadata.update(total_chars=len(content), document_text_sha256=sha256(content.encode('utf-8')),
                        offset_unit='unicode_code_points')
        return metadata, content

    @staticmethod
    def _offset(offset, total):
        if offset > total:
            raise HistoricalSearchError('Offset is beyond the verified document.', code='invalid_navigation')

    def open(self, url: str, offset=0, length=None, sha256=None) -> dict:
        with self._operation():
            return self._open(url, offset, length, sha256)

    def _open(self, url: str, offset=0, length=None, expected_sha256=None) -> dict:
        navigation_arguments('open', {'url': url, 'offset': offset,
                             **({'length': length} if length is not None else {}),
                             **({'sha256': expected_sha256} if expected_sha256 is not None else {})})
        url = self._check_url(url)
        url_hash = sha256(url.encode())
        self.audit.write('open_requested', url_sha256=url_hash, offset=offset, length=length)
        try:
            metadata, content = self._document(url, expected_sha256)
            self._offset(offset, len(content))
            page = content[offset:] if length is None else content[offset:offset + length]
            end = offset + len(page)
            more = end < len(content)
            result = {**metadata, 'text': page, 'offset': offset, 'end_offset': end,
                      'next_offset': end if more else None, 'has_more': more, 'truncated': more,
                      'text_sha256': sha256(page.encode('utf-8'))}
            self.audit.write('open_completed', url_sha256=url_hash, capture=result['capture_time'],
                             body_sha256=result['sha256'], offset=offset, end_offset=end,
                             total_chars=len(content), next_offset=result['next_offset'], has_more=more,
                             text_sha256=result['text_sha256'], document_text_sha256=result['document_text_sha256'],
                             result_sha256=sha256(canonical_json(result)))
            return {'cutoff': utc_text(self.cutoff), 'result': result, 'limitation': LIMITATION}
        except HistoricalSearchError as error:
            self.audit.write('open_rejected', url_sha256=url_hash, **self._failure(error))
            raise

    def find(self, url: str, pattern: str, offset=0, max_matches=10, sha256=None) -> dict:
        with self._operation():
            return self._find(url, pattern, offset, max_matches, sha256)

    def _find(self, url, pattern, offset=0, max_matches=10, expected_sha256=None):
        navigation_arguments('find', {'url': url, 'pattern': pattern, 'offset': offset,
                             'max_matches': max_matches,
                             **({'sha256': expected_sha256} if expected_sha256 is not None else {})})
        url = self._check_url(url)
        url_hash, pattern_hash = sha256(url.encode()), sha256(pattern.encode('utf-8'))
        self.audit.write('find_requested', url_sha256=url_hash, pattern_sha256=pattern_hash,
                         offset=offset, max_matches=max_matches)
        try:
            metadata, content = self._document(url, expected_sha256)
            self._offset(offset, len(content))
            matches, cursor = [], offset
            while len(matches) < max_matches:
                position = content.find(pattern, cursor)
                if position < 0:
                    break
                size = max(400, len(pattern))
                start = max(0, position - min(120, size - len(pattern)))
                excerpt = content[start:start + size]
                matches.append({'offset': position, 'end_offset': position + len(pattern),
                                'excerpt_offset': start, 'excerpt_end_offset': start + len(excerpt),
                                'text': excerpt, 'text_sha256': sha256(excerpt.encode('utf-8'))})
                cursor = position + 1  # Retain overlapping literal matches.
            more = content.find(pattern, cursor) >= 0 if matches else False
            result = {**metadata, 'pattern': pattern, 'offset': offset, 'matches': matches,
                      'next_offset': cursor if more else None, 'has_more': more, 'truncated': more}
            self.audit.write('find_completed', url_sha256=url_hash, capture=result['capture_time'],
                             body_sha256=result['sha256'], pattern_sha256=pattern_hash, offset=offset,
                             total_chars=len(content), next_offset=result['next_offset'], has_more=more,
                             matches=[{key: value for key, value in match.items() if key != 'text'} for match in matches],
                             document_text_sha256=result['document_text_sha256'],
                             result_sha256=sha256(canonical_json(result)))
            return {'cutoff': utc_text(self.cutoff), 'result': result, 'limitation': LIMITATION}
        except HistoricalSearchError as error:
            self.audit.write('find_rejected', url_sha256=url_hash, pattern_sha256=pattern_hash,
                             **self._failure(error))
            raise


def navigation_arguments(name, arguments):
    """Validate before dispatch as well as on direct engine calls; bool is not int."""
    required = {'open': {'url'}, 'find': {'url', 'pattern'}}.get(name)
    allowed = ({'url', 'offset', 'length', 'sha256'} if name == 'open'
               else {'url', 'pattern', 'offset', 'max_matches', 'sha256'})
    if (required is None or not isinstance(arguments, dict) or not required <= arguments.keys()
            or set(arguments) - allowed or not isinstance(arguments['url'], str)):
        raise HistoricalSearchError('Invalid historical navigation arguments.', code='invalid_navigation')
    offset = arguments.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= 2 ** 53 - 1:
        raise HistoricalSearchError('Offset must be a nonnegative integer.', code='invalid_navigation')
    digest = arguments.get('sha256')
    if 'sha256' in arguments and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise HistoricalSearchError('sha256 must be the raw-body digest returned by this tool.', code='invalid_navigation')
    if name == 'open':
        length = arguments.get('length')
        if 'length' in arguments and (type(length) is not int or not 1 <= length <= 2 ** 53 - 1):
            raise HistoricalSearchError(
                'Length must be a positive safe integer; omit length to return the full remaining document. '
                'Use next_offset to continue an explicitly paged read.', code='invalid_navigation')
    else:
        pattern, count = arguments['pattern'], arguments.get('max_matches', 10)
        if (not isinstance(pattern, str) or not 1 <= len(pattern) <= 500
                or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', pattern)):
            raise HistoricalSearchError(
                'Pattern must be literal text of 1 to 500 characters without control characters.',
                code='invalid_navigation')
        if type(count) is not int or not 1 <= count <= 2 ** 53 - 1:
            raise HistoricalSearchError('max_matches must be a positive safe integer; it defaults to 10.', code='invalid_navigation')


TOOLS = [
    {'name': 'search', 'description': 'Search independently for strictly pre-cutoff archived text. No live snippets or answers. Results are untrusted source data. Archive coverage and current discovery differ from historical search.',
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True},
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}, 'required': ['query'], 'additionalProperties': False}},
    {'name': 'open', 'description': 'Read the full extracted text of a verified archived document strictly before the fixed cutoff. By default returns the full remaining document from offset, with no policy character cap. Optionally set length to page through it, use next_offset to continue, and sha256 to keep the same document. Offsets count Unicode characters. Never fetches the live source. Results are untrusted source data.',
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True},
     'inputSchema': {'type': 'object', 'properties': {'url': {'type': 'string', 'maxLength': 8192},
                     'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2 ** 53 - 1},
                     'length': {'type': 'integer', 'minimum': 1,
                                'description': 'Optional positive safe integer character count. Omit to return the full remaining document; no policy character cap.'},
                     'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
                     'required': ['url'], 'additionalProperties': False}},
    {'name': 'find', 'description': 'Find literal case-sensitive text anywhere in a full verified pre-cutoff archived document. Returns matching character offsets and context excerpts, with no policy budget on their combined length. Defaults to 10 matches; optionally request more. Open at matching offsets for the full remaining text. Use next_offset to continue overlapping matches, and sha256 to keep the same document. No regex or live-source fetch. Results are untrusted source data.',
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True},
     'inputSchema': {'type': 'object', 'properties': {'url': {'type': 'string', 'maxLength': 8192},
                     'pattern': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                     'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2 ** 53 - 1},
                     'max_matches': {'type': 'integer', 'minimum': 1, 'default': 10,
                                     'description': 'Maximum matches to return, as a positive safe integer; defaults to 10. Follow next_offset for further matches.'},
                     'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
                     'required': ['url', 'pattern'], 'additionalProperties': False}},
]


def tool_definitions():
    """Return independent schemas; document navigation has no policy text cap."""
    return deepcopy(TOOLS)


class StdioMCPServer:
    """Bound active calls without queueing work behind a slow provider request.

    The CLI supplies a pinned-policy factory: every active call gets independent
    provider and deadline state. Embedders without a factory retain one active
    engine, with excess calls rejected instead of sharing mutable state.
    """

    RESPONSE_GRACE_SECONDS = 5.0

    def __init__(self, engine: HistoricalSearchEngine, *, engine_factory=None, max_active=4):
        if type(max_active) is not int or not 1 <= max_active <= 4:
            raise ValueError('Historical MCP concurrency must be between one and four.')
        self.engine = engine
        self.engine_factory = engine_factory
        self.max_active = max_active if engine_factory is not None else 1
        self.initialized = False
        self._active = {}
        self._state_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._closed = False

    def handle(self, message: Any, *, engine=None, validate_only=False) -> dict | None:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return {'jsonrpc': '2.0', 'id': message.get('id') if isinstance(message, dict) else None, 'error': {'code': -32600, 'message': 'Invalid request.'}}
        if 'id' not in message:
            return None
        request_id, method, params = message['id'], message['method'], message.get('params', {})
        response = {'jsonrpc': '2.0', 'id': request_id}
        if not isinstance(params, dict):
            return {**response, 'error': {'code': -32602, 'message': 'Invalid parameters.'}}
        if method == 'initialize':
            self.initialized = True
            requested = params.get('protocolVersion')
            version = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-03-26'
            instructions = (LIMITATION + f' Issue at most {self.max_active} concurrent retrieval calls. '
                            'If retrieval is busy, wait for an active call to finish before retrying. '
                            'Open returns the full verified document by default; set length only for optional pagination. '
                            'Use find to locate text anywhere in the document, or open.next_offset to continue a requested page; '
                            'pass the returned sha256 on later calls to keep the same archived document.')
            return {**response, 'result': {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'historical_search', 'version': '1.1.0'}, 'instructions': instructions}}
        if method == 'ping':
            return {**response, 'result': {}}
        if not self.initialized:
            return {**response, 'error': {'code': -32000, 'message': 'Initialize first.'}}
        if method == 'tools/list':
            return {**response, 'result': {'tools': tool_definitions()}}
        if method != 'tools/call':
            return {**response, 'error': {'code': -32601, 'message': 'Method not found.'}}
        name, arguments = params.get('name'), params.get('arguments', {})
        try:
            if name == 'search':
                if not isinstance(arguments, dict) or set(arguments) != {'query'} or not isinstance(arguments['query'], str):
                    raise HistoricalSearchError('Invalid search arguments.')
            else:
                navigation_arguments(name, arguments)
        except HistoricalSearchError as error:
            # These validation messages contain only controller text, never
            # submitted URLs, patterns or credentials.
            return {**response, 'error': {'code': -32602, 'message': str(error)}}
        except TypeError:
            return {**response, 'error': {'code': -32602, 'message': 'Use search(query), open(url, offset?, length?, sha256?), or find(url, pattern, offset?, max_matches?, sha256?); cutoff and policy are fixed.'}}
        if validate_only:
            return None
        try:
            selected_engine = self.engine if engine is None else engine
            result = (selected_engine.search(arguments['query']) if name == 'search'
                      else getattr(selected_engine, name)(**arguments))
            text = json.dumps(result, ensure_ascii=False)
            # Text a UTF-8 reply cannot carry fails closed here, before the
            # audit records the reply, instead of muting the stdio stream.
            text.encode('utf-8')
            output = {'content': [{'type': 'text', 'text': text}], 'isError': False}
        except HistoricalSearchError as error:
            output = {'content': [{'type': 'text', 'text': str(error)}], 'isError': True}
        except Exception:
            # Never return an exception containing provider metadata or credentials.
            output = {'content': [{'type': 'text', 'text': 'Historical retrieval failed closed.'}], 'isError': True}
        return {**response, 'result': output}

    @staticmethod
    def _tool_error(request_id, message):
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {
            'content': [{'type': 'text', 'text': message}], 'isError': True}}

    def _audit(self, event, **fields):
        audit = getattr(self.engine, 'audit', None)
        if audit is not None:
            audit.write(event, **fields)

    def _emit(self, output_stream, response):
        if response is None or self._closed:
            return
        with self._output_lock:
            output_stream.write(json.dumps(response, ensure_ascii=False) + '\n')
            output_stream.flush()

    def _emit_call(self, output_stream, call, response):
        # A text stream encodes a whole line before writing any of it, so an
        # unencodable reply is replaced rather than closing the stream.
        try:
            self._emit(output_stream, response)
        except UnicodeError:
            self._emit(output_stream, self._tool_error(call['id'], 'Historical retrieval failed closed.'))

    def _deadline(self, call, output_stream):
        with self._state_lock:
            if self._closed or call['cancelled'] or call['responded']:
                return
            response = self._tool_error(call['id'], 'Historical retrieval deadline was exhausted.')
            try:
                self._audit('mcp_deadline', operation_id=call['operation_id'])
            except HistoricalSearchError:
                response = self._tool_error(call['id'], 'Historical retrieval audit failed closed.')
            call['responded'] = True
            try:
                self._emit_call(output_stream, call, response)
            except (OSError, ValueError):
                self._closed = True
            # A timed-out or cancelled worker keeps its capacity slot until it
            # actually exits. We never admit replacement work over a live call.

    def _run(self, call, output_stream):
        response = None
        try:
            with self._state_lock:
                if call['cancelled'] or call['responded'] or self._closed:
                    return
            engine = self.engine_factory() if self.engine_factory is not None else self.engine
            engine.request_deadline = call['deadline']
            if isinstance(getattr(engine, 'audit', None), AuditLog):
                engine.audit.operation_id = call['operation_id']
            with self._state_lock:
                if call['cancelled'] or call['responded'] or self._closed:
                    return
            response = self.handle(call['message'], engine=engine)
        except HistoricalSearchError as error:
            response = self._tool_error(call['id'], str(error))
        except Exception:
            response = self._tool_error(call['id'], 'Historical retrieval failed closed.')
        finally:
            with self._state_lock:
                call['timer'].cancel()
                try:
                    if not self._closed and not call['cancelled'] and not call['responded']:
                        if time.monotonic() >= call['response_deadline']:
                            response = self._tool_error(call['id'], 'Historical retrieval deadline was exhausted.')
                        try:
                            self._audit('mcp_response_ready', operation_id=call['operation_id'],
                                        is_error=bool((response or {}).get('result', {}).get('isError', True)))
                        except HistoricalSearchError:
                            response = self._tool_error(call['id'], 'Historical retrieval audit failed closed.')
                        call['responded'] = True
                        self._emit_call(output_stream, call, response)
                except (OSError, ValueError):
                    self._closed = True
                finally:
                    self._active.pop(call['id'], None)

    def _dispatch(self, message, output_stream):
        request_id = message['id']
        if type(request_id) not in (int, str):
            self._emit(output_stream, {'jsonrpc': '2.0', 'id': None,
                                      'error': {'code': -32600, 'message': 'Invalid request identifier.'}})
            return
        with self._state_lock:
            if request_id in self._active:
                self._emit(output_stream, {'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32600, 'message': 'Request identifier is already active.'}})
                return
            if len(self._active) >= self.max_active:
                response = self._tool_error(request_id,
                    'Historical retrieval is busy. Wait for an active request to finish before retrying.')
                try:
                    self._audit('mcp_busy', active_calls=len(self._active))
                except HistoricalSearchError:
                    response = self._tool_error(request_id, 'Historical retrieval audit failed closed.')
                self._emit(output_stream, response)
                return
            timeout = min(float(getattr(self.engine, 'operation_timeout', 300)), 300.0)
            call = {'id': request_id, 'message': message, 'deadline': time.monotonic() + timeout,
                    'operation_id': uuid.uuid4().hex, 'cancelled': False, 'responded': False}
            # Provider I/O still stops at the original deadline. Allow bounded
            # finalization of already verified partial evidence after it expires.
            call['response_deadline'] = call['deadline'] + self.RESPONSE_GRACE_SECONDS
            try:
                self._audit('mcp_accepted', operation_id=call['operation_id'])
            except HistoricalSearchError:
                self._emit(output_stream, self._tool_error(request_id, 'Historical retrieval audit failed closed.'))
                return
            call['timer'] = threading.Timer(max(0.0, call['response_deadline'] - time.monotonic()),
                                             self._deadline, args=(call, output_stream))
            call['timer'].daemon = True
            call['thread'] = threading.Thread(target=self._run, args=(call, output_stream),
                                               name='historical-mcp', daemon=True)
            self._active[request_id] = call
            try:
                call['timer'].start()
                call['thread'].start()
            except Exception:
                call['timer'].cancel()
                self._active.pop(request_id, None)
                self._emit(output_stream, self._tool_error(request_id, 'Historical retrieval failed closed.'))

    def _cancel(self, message):
        params = message.get('params', {})
        request_id = params.get('requestId') if isinstance(params, dict) else None
        if type(request_id) not in (int, str):
            return
        with self._state_lock:
            call = self._active.get(request_id)
            if call is None or call['responded']:
                return
            call['cancelled'] = True
            call['timer'].cancel()
            try:
                self._audit('mcp_cancelled', operation_id=call['operation_id'])
            except HistoricalSearchError:
                pass  # No provider content or response may escape cancellation.

    def serve(self, input_stream, output_stream):
        clean_eof = False
        try:
            while True:
                line = input_stream.readline(MAX_PROTOCOL_LINE + 1)
                if not line:
                    clean_eof = True
                    break
                if len(line) > MAX_PROTOCOL_LINE:
                    raise HistoricalSearchError('MCP input exceeds the protocol message limit.')
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error.'}}
                else:
                    if (isinstance(message, dict) and message.get('jsonrpc') == '2.0'
                            and message.get('method') == 'notifications/cancelled' and 'id' not in message):
                        self._cancel(message)
                        continue
                    if isinstance(message, dict) and message.get('method') == 'tools/call' and 'id' in message:
                        response = self.handle(message, validate_only=True)
                        if response is None:
                            self._dispatch(message, output_stream)
                            continue
                    else:
                        response = self.handle(message)
                self._emit(output_stream, response)
        finally:
            with self._state_lock:
                active = list(self._active.values())
            if clean_eof:
                for call in active:
                    call['thread'].join(timeout=max(0.0, call['response_deadline'] - time.monotonic()) + 0.1)
            with self._state_lock:
                self._closed = True
                for call in self._active.values():
                    call['cancelled'] = True
                    call['timer'].cancel()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--policy-file', required=True, type=Path)
    parser.add_argument('--policy-sha256', help='Controller-pinned policy digest (recommended).')
    parser.add_argument('--audit-log', required=True, type=Path)
    parser.add_argument('--preflight', action='store_true', help='Validate local policy/credentials only; no network requests.')
    args = parser.parse_args(argv)
    try:
        engine = HistoricalSearchEngine(args.policy_file, args.cutoff, args.audit_log, args.policy_sha256)
        if args.preflight:
            result = {'status': 'ready', 'provider': engine.provider_name, 'cutoff': utc_text(engine.cutoff),
                      'network_checked': False, 'pdf_supported': pdf_supported(), 'limitation': LIMITATION}
            if isinstance(engine.provider, CodexWaybackProvider):
                try:
                    from retrieval.historical_codex_discovery import preflight
                    preflight(engine.provider.discovery_settings)
                except Exception:
                    raise HistoricalSearchError('Independent Codex discovery preflight failed.',
                                                code='discovery_unavailable', operational=True) from None
                result['discovery_preflight'] = 'ready'
            print(json.dumps(result))
        else:
            def engine_factory():
                created = HistoricalSearchEngine(args.policy_file, args.cutoff, args.audit_log, engine.policy_sha256)
                # Per-call engines keep independent deadlines and audit state;
                # only the bounded in-memory caches are shared across calls.
                if isinstance(created.provider, WaybackProvider):
                    created.provider.share_memory_caches(engine.provider)
                return created
            # MCP stdio is UTF-8 JSON-RPC regardless of the platform's default
            # pipe encoding (for example cp1252 on Windows).
            for stream, options in ((sys.stdin, {}), (sys.stdout, {'newline': '\n'})):
                reconfigure = getattr(stream, 'reconfigure', None)
                if reconfigure is not None:
                    reconfigure(encoding='utf-8', errors='replace', **options)
            StdioMCPServer(engine, engine_factory=engine_factory).serve(sys.stdin, sys.stdout)
        return 0
    except HistoricalSearchError as error:
        print('Historical search unavailable: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
