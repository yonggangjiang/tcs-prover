"""URL-only discovery through the official arXiv and Crossref public APIs.

This is an explicitly selected independent provider, never a DuckDuckGo fallback.
Current titles, abstracts, dates, citations and relevance scores are discarded.
Publication/submission date filters are discovery hints only: callers must pass
every candidate through their unchanged historical archive evidence gate.

The client must enforce TLS, endpoint allowlisting, redirects, response byte
limits, and request timeouts. ``canonical_url`` is the broker's source-URL guard.
Per-process pacing below serializes arXiv requests at least three seconds apart
and Crossref requests at least one second apart. Multi-process deployments MUST
also enforce these limits across all brokers in the controller/client layer.

References:
https://info.arxiv.org/help/api/user-manual.html
https://info.arxiv.org/help/api/tou.html
https://www.crossref.org/documentation/retrieve-metadata/rest-api/
"""
from __future__ import annotations

from datetime import timedelta, timezone
import json
import re
import threading
import time
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET


MAX_DISCOVERY_BYTES = 2 * 1024 * 1024
ARXIV_HOST = 'export.arxiv.org'
CROSSREF_HOST = 'api.crossref.org'
ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV_IDENTIFIER = re.compile(r'(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z.-]*/\d{7})(?:v\d+)?')


class PacedRequests:
    """One connection at a time with a minimum interval between request starts."""

    def __init__(self, interval, monotonic=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self.monotonic = monotonic
        self.sleep = sleep
        self.next_allowed = 0.0
        self.lock = threading.Lock()

    def get(self, client, url):
        if getattr(client, 'coordinator', None) is not None:
            # The production transport's cross-process limiter already enforces
            # provider concurrency and pacing under its operation deadline.
            # A process-local lock here would queue calls outside that deadline.
            return client.get(url)
        with self.lock:
            delay = self.next_allowed - self.monotonic()
            if delay > 0:
                self.sleep(delay)
            self.next_allowed = self.monotonic() + self.interval
            return client.get(url)


_ARXIV_PACER = PacedRequests(3.0)
_CROSSREF_PACER = PacedRequests(1.0)


def _body(response, endpoint, accepted_mimes, error_type):
    if response.status != 200 or response.url != endpoint:
        raise error_type('Scholarly discovery returned an unsuccessful or redirected response.')
    mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if mime not in accepted_mimes:
        raise error_type('Scholarly discovery returned an unsupported response type.')
    if not isinstance(response.body, bytes) or len(response.body) > MAX_DISCOVERY_BYTES:
        raise error_type('Scholarly discovery response exceeds its byte limit.')
    return response.body


def _arxiv_search_expression(query, cutoff):
    # Treat user input as text, not an API expression that can alter date hints.
    terms = []
    for phrase, word in re.findall(r'"([^"\n]+)"|([^\s"]+)', query):
        if phrase:
            cleaned = ' '.join(re.findall(r'[\w-]+', phrase, flags=re.UNICODE))
            if cleaned:
                terms.append('all:"' + cleaned + '"')
        elif word.upper() not in ('AND', 'OR', 'NOT'):
            terms.extend('all:"' + token + '"' for token in re.findall(r'[\w-]+', word, flags=re.UNICODE))
    latest = (cutoff.astimezone(timezone.utc) - timedelta(microseconds=1)).strftime('%Y%m%d%H%M')
    return ' AND '.join(terms[:32]) + f' AND submittedDate:[199101010000 TO {latest}]'


def _arxiv_urls(query, cutoff, count, client, error_type, canonical_url):
    endpoint = 'https://' + ARXIV_HOST + '/api/query?' + urlencode({
        'search_query': _arxiv_search_expression(query, cutoff), 'start': 0,
        'max_results': count, 'sortBy': 'relevance', 'sortOrder': 'descending',
    })
    body = _body(_ARXIV_PACER.get(client, endpoint), endpoint,
                 {'application/atom+xml', 'application/xml', 'text/xml'}, error_type)
    try:
        xml = body.decode('utf-8')
    except UnicodeError:
        raise error_type('Scholarly discovery XML must use UTF-8.') from None
    if '\0' in xml or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', xml, re.IGNORECASE):
        raise error_type('Scholarly discovery XML declarations are not allowed.')
    try:
        feed = ET.fromstring(xml)
    except ET.ParseError:
        raise error_type('Scholarly discovery returned invalid XML.') from None
    if feed.tag != ATOM + 'feed':
        raise error_type('Scholarly discovery returned an invalid Atom feed.')
    identifiers = set()
    for entry in feed.findall(ATOM + 'entry')[:count]:
        identity = entry.findtext(ATOM + 'id', '')
        try:
            parsed = urlsplit(identity)
            if (parsed.scheme not in ('http', 'https') or parsed.hostname != 'arxiv.org'
                    or parsed.username or parsed.password or parsed.port is not None
                    or parsed.query or parsed.fragment or not parsed.path.startswith('/abs/')):
                continue
        except ValueError:
            continue
        identifier = parsed.path[len('/abs/'):]
        if ARXIV_IDENTIFIER.fullmatch(identifier):
            # A current latest-version URL may not exist before the cutoff.
            # The standard versionless URL lets the archive choose old bytes;
            # no live arXiv document is fetched here or by the archive gate.
            identifiers.add(re.sub(r'v\d+$', '', identifier))
    urls = []
    for identifier in sorted(identifiers):
        for kind in ('abs', 'pdf'):
            try:
                urls.append(canonical_url(f'https://arxiv.org/{kind}/{identifier}'))
            except (error_type, ValueError):
                continue
    return urls


def _crossref_urls(query, cutoff, count, client, error_type, canonical_url):
    endpoint = 'https://' + CROSSREF_HOST + '/works?' + urlencode({
        'query': query, 'rows': count,
        'filter': 'until-pub-date:' + (cutoff.astimezone(timezone.utc) - timedelta(microseconds=1)).date().isoformat(),
        'select': 'URL,link,resource',
    })
    body = _body(_CROSSREF_PACER.get(client, endpoint), endpoint,
                 {'application/json'}, error_type)
    try:
        value = json.loads(body)
        rows = value['message']['items']
        if not isinstance(rows, list):
            raise TypeError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise error_type('Scholarly discovery returned an invalid Crossref result list.') from None
    urls = set()
    for row in rows[:count]:
        if not isinstance(row, dict):
            continue
        possible = [row.get('URL')]
        resource = row.get('resource')
        primary = resource.get('primary') if isinstance(resource, dict) else None
        if isinstance(primary, dict):
            possible.append(primary.get('URL'))
        links = row.get('link')
        if isinstance(links, list):
            possible.extend(link.get('URL') for link in links if isinstance(link, dict)
                            and link.get('content-type') in ('application/pdf', 'text/html', 'application/xhtml+xml'))
        for candidate in possible:
            try:
                source = canonical_url(candidate)
                if urlsplit(source).hostname in ('doi.org', 'dx.doi.org'):
                    continue  # DOI resolvers require redirects; use direct publisher URLs.
                urls.add(source)
            except (error_type, ValueError, TypeError):
                continue
    return sorted(urls)


def discover(query, cutoff, max_candidates, client, error_type, canonical_url, on_failure=None):
    """Return bounded, sorted URL candidates; never authorizes source content.

    Both configured public APIs are queried independently. A provider failure
    does not discard URLs from the other provider, but if neither can be queried
    successfully, fail closed with a static diagnostic. No endpoint fallback,
    challenge bypass, credentials, or source-website request is performed.
    """
    if (not isinstance(query, str) or not query.strip() or len(query) > 2000
            or re.search(r'[\x00-\x1f\x7f]', query)
            or type(max_candidates) is not int or not 1 <= max_candidates <= 20):
        raise error_type('Scholarly discovery requires bounded query text and candidate count.')
    candidates = []
    successes = 0
    failures = 0
    for fetch, host in ((_arxiv_urls, ARXIV_HOST), (_crossref_urls, CROSSREF_HOST)):
        try:
            candidates.append(fetch(query, cutoff, max_candidates, client, error_type, canonical_url))
            successes += 1
        except (error_type, OSError, ValueError) as error:
            # An unavailable provider can yield partial discovery, but a
            # broken audit boundary or exhausted operation budget cannot.
            if isinstance(error, error_type) and getattr(error, 'code', None) in ('audit_failure', 'deadline_exceeded'):
                raise
            candidates.append([])
            failures += 1
            if on_failure is not None:
                code = getattr(error, 'code', None)
                if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code):
                    code = 'discovery_provider_failure'
                status = getattr(error, 'status', getattr(error, 'http_status', None))
                if type(status) is not int or not 100 <= status <= 599:
                    status = None
                # Deliberately omit exception messages, URLs, query text and
                # provider bodies; diagnostics contain only static metadata.
                on_failure({'host': host, 'code': code, 'status': status})
    if not successes or (failures and not any(candidates)):
        raise error_type('Independent scholarly discovery unavailable; no live fallback is permitted.',
                         code='discovery_unavailable', operational=True)
    # Alternate deterministic provider lists so the limit cannot starve either
    # API simply because arxiv.org sorts before a publisher hostname.
    selected = set()
    for position in range(max(map(len, candidates), default=0)):
        for provider_urls in candidates:
            if position < len(provider_urls):
                selected.add(provider_urls[position])
                if len(selected) == max_candidates:
                    return sorted(selected)
    return sorted(selected)
