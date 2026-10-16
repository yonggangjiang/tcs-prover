#!/usr/bin/env python3
"""Exercise real broker stdio search/open outside solver conversations.

The opt-in --concurrent10 check starts ten independent brokers with distinct,
harmless scholarly queries, individual policies and one initially empty shared
controller cache/rate limiter. It sends no benchmark input. The codex_wayback
provider invokes isolated URL-discovery models; no proof models are started.
Every result must still pass the broker's unchanged historical evidence gate.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


CONCURRENT_QUERIES = (
    'Euler characteristic', 'prime number theorem', 'Fourier transform',
    'central limit theorem', 'linear algebra', 'graph coloring',
    'Markov chains', 'Riemannian geometry', 'convex optimization', 'finite groups',
)
BROKER_SOURCES = ('historical_search.py', 'historical_transport.py',
                  'historical_rate_limit.py', 'historical_cache.py', 'scholarly_discovery.py',
                  'historical_codex_discovery.py', 'historical_discovery_auth.py')


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Evidence timestamp must include its timezone.')
    return parsed


def source_hashes():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in BROKER_SOURCES}


def call(policy, cutoff, audit, name, arguments):
    """Run a fresh broker and retain its complete stdio exchange even on error."""
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}},
    ]
    policy_bytes = policy.read_bytes()
    timeout = json.loads(policy_bytes).get('operation_timeout_seconds', 300) + 20
    request = ''.join(json.dumps(message) + '\n' for message in messages)
    audit.with_suffix('.request.jsonl').write_text(request, encoding='utf-8')
    metadata = {'startedAtUtc': utc_now(), 'method': name,
                'policySha256': hashlib.sha256(policy_bytes).hexdigest(), 'timeoutSeconds': timeout}
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name('historical_search.py')),
         '--cutoff', cutoff, '--policy-file', str(policy),
         '--policy-sha256', metadata['policySha256'], '--audit-log', str(audit)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8',
    )
    metadata['pid'] = process.pid
    timed_out = False
    try:
        stdout, stderr = process.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    metadata.update(completedAtUtc=utc_now(), elapsedSeconds=round(time.monotonic() - started, 3),
                    returncode=process.returncode, timedOut=timed_out)
    audit.with_suffix('.stdout.jsonl').write_text(stdout, encoding='utf-8')
    audit.with_suffix('.stderr.txt').write_text(stderr, encoding='utf-8')
    write_json(audit.with_suffix('.process.json'), metadata)
    if timed_out:
        raise RuntimeError('Broker exceeded its operation budget plus process shutdown allowance.')
    if process.returncode:
        raise RuntimeError('Broker exited unsuccessfully; retained stdio contains the diagnostic.')
    responses = [json.loads(line) for line in stdout.splitlines()]
    matching = [response for response in responses if response.get('id') == 2]
    if len(matching) != 1:
        raise ValueError('Broker did not return exactly one tool-call response.')
    response = matching[0]
    result = response.get('result', {})
    if result.get('isError') or 'error' in response:
        raise RuntimeError(json.dumps(result.get('content', response.get('error'))))
    value = json.loads(result['content'][0]['text'])
    if not isinstance(value, dict):
        raise ValueError('Broker returned an invalid response object.')
    return value


def check_document(document, cutoff_time):
    capture = parse_time(document['capture_time'])
    if (capture >= cutoff_time or not isinstance(document['text'], str)
            or not document['text'].strip()
            or not re.fullmatch(r'[0-9a-f]{64}', document['sha256'])):
        raise ValueError('Result violated the cutoff, readability or source-hash check.')


def read_audit(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def check_audit(path, documents, event):
    records = read_audit(path)
    for document in documents:
        source_hash = hashlib.sha256(document['url'].encode('utf-8')).hexdigest()
        if not any(record.get('event') == event and record.get('url_sha256') == source_hash
                   and record.get('body_sha256') == document['sha256']
                   and record.get('capture') == document['capture_time'] for record in records):
            raise ValueError('Returned capture does not have matching broker audit evidence.')


def trial(policy, cutoff, directory, query, index, barrier=None):
    row = {'trial': index, 'query': query, 'passed': False, 'startedAtUtc': utc_now(),
           'policySha256': hashlib.sha256(policy.read_bytes()).hexdigest()}
    started = time.monotonic()
    cutoff_time = parse_time(cutoff)
    phase = 'search'
    try:
        if barrier is not None:
            barrier.wait(timeout=30)
        search = call(policy, cutoff, directory / 'search-audit.jsonl', 'search', {'query': query})
        write_json(directory / 'search-response.json', search)
        if not search['results'] or parse_time(search['cutoff']) != cutoff_time:
            raise ValueError('Search returned no verified result or an unexpected cutoff.')
        for document in search['results']:
            check_document(document, cutoff_time)
        check_audit(directory / 'search-audit.jsonl', search['results'], 'capture_verified')
        row.update(searchPassed=True, returned=len(search['results']), incomplete=search.get('incomplete', False),
                   searchElapsedSeconds=round(time.monotonic() - started, 3),
                   sources=[{key: document[key] for key in ('url', 'capture_time', 'sha256')}
                            for document in search['results']])
        first = search['results'][0]
        phase = 'open'
        opened = call(policy, cutoff, directory / 'open-audit.jsonl', 'open', {'url': first['url']})
        write_json(directory / 'open-response.json', opened)
        document = opened['result']
        check_document(document, cutoff_time)
        if (parse_time(opened['cutoff']) != cutoff_time or document['url'] != first['url']
                or document['sha256'] != first['sha256']
                or document['capture_time'] != first['capture_time']):
            raise ValueError('Independent open did not match the verified search capture.')
        check_audit(directory / 'open-audit.jsonl', [document], 'open_completed')
        row.update(passed=True, openPassed=True, openedUrl=document['url'],
                   capture=document['capture_time'], sha256=document['sha256'])
    except (RuntimeError, ValueError, KeyError, TypeError, IndexError, OSError,
            subprocess.SubprocessError, threading.BrokenBarrierError) as error:
        row.update(failedPhase=phase, error=str(error))
        write_json(directory / (phase + '-error.json'), {'error': str(error), 'type': type(error).__name__})
    row.update(completedAtUtc=utc_now(), elapsedSeconds=round(time.monotonic() - started, 3))
    write_json(directory / 'trial.json', row)
    return row


def aggregate_evidence(output):
    records, processes = [], []
    for directory in sorted(output.glob('trial-*')):
        for audit in directory.glob('*-audit.jsonl'):
            records.extend(read_audit(audit))
        for metadata in directory.glob('*-audit.process.json'):
            processes.append(json.loads(metadata.read_text(encoding='utf-8')))
    boundaries = []
    for process in processes:
        boundaries.extend(((parse_time(process['startedAtUtc']), 1),
                           (parse_time(process['completedAtUtc']), -1)))
    active = peak = 0
    for _, change in sorted(boundaries):
        active += change
        peak = max(peak, active)
    searches = [parse_time(p['startedAtUtc']) for p in processes if p['method'] == 'search']
    return {
        'brokerProcesses': len(processes), 'peakConcurrentBrokerProcesses': peak,
        'initialSearchLaunchSpreadSeconds': round((max(searches) - min(searches)).total_seconds(), 3) if searches else None,
        'auditEvents': dict(Counter(record.get('event') for record in records)),
        'httpFailures': dict(Counter(str(record.get('status')) + ':' + str(record.get('code'))
                                    for record in records if record.get('event') == 'http_failure')),
        'captureRejections': dict(Counter(record.get('failure_code') for record in records
                                         if record.get('event') == 'capture_rejected')),
        'verifiedCaptures': sum(record.get('event') == 'capture_verified' for record in records),
        'cacheHits': sum(record.get('event') == 'archive_cache_hit' for record in records),
    }


def validate(policy, cutoff, output, query='Euler characteristic', repeats=3, concurrent10=False):
    parse_time(cutoff)
    base_policy = json.loads(policy.read_bytes())
    if concurrent10 and base_policy.get('provider') not in ('scholarly_wayback', 'codex_wayback'):
        raise ValueError('Concurrent validation requires scholarly_wayback or codex_wayback.')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Validation output must be empty so prior evidence is preserved.')
    output.mkdir(parents=True, exist_ok=True)
    queries = CONCURRENT_QUERIES if concurrent10 else (query,) * repeats
    shared_state = output / 'retrieval-state'
    report = {'startedAtUtc': utc_now(), 'cutoff': cutoff, 'proofModelsStarted': 0,
              'discoveryUsesModel': base_policy.get('provider') == 'codex_wayback',
              'benchmarkInputsSent': 0, 'protectedArchiveCacheEnabled': True,
              'mode': 'concurrent10' if concurrent10 else 'sequential',
              'sharedArchiveCache': True, 'archiveCacheInitiallyEmpty': concurrent10,
              'rateLimitDirectory': str(shared_state) if concurrent10 else base_policy.get('rate_limit_directory'),
              'maxCandidates': base_policy.get('max_candidates', 10),
              'operationTimeoutSeconds': base_policy.get('operation_timeout_seconds', 300),
              'policySha256': hashlib.sha256(policy.read_bytes()).hexdigest(),
              'brokerSourceSha256': source_hashes(), 'queries': list(queries), 'trials': [], 'passed': False}
    write_json(output / 'report.json', report)
    prepared = []
    for index, selected_query in enumerate(queries, 1):
        directory = output / f'trial-{index:02d}'
        directory.mkdir()
        policy_copy = dict(base_policy)
        if base_policy.get('provider') == 'codex_wayback':
            # Each validation trial keeps current-web traces out of the real
            # experiment's discovery state. The auth-only home is read-only input.
            policy_copy['discovery'] = {**base_policy['discovery'],
                                        'state_root': str(directory / 'discovery-state')}
        if concurrent10:
            workspace = directory / 'solver-workspace'
            workspace.mkdir()
            policy_copy.update(rate_limit_directory=str(shared_state), solver_workspace=str(workspace))
        trial_policy = directory / 'broker-policy.json'
        write_json(trial_policy, policy_copy)
        prepared.append((trial_policy, cutoff, directory, selected_query, index))
    started = time.monotonic()

    def record(row):
        report['trials'].append(row)
        report['trials'].sort(key=lambda item: item['trial'])
        report['completedTrials'] = len(report['trials'])
        write_json(output / 'report.json', report)
        print(json.dumps(row), flush=True)

    if concurrent10:
        barrier = threading.Barrier(10)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(trial, *args, barrier=barrier) for args in prepared]
            for future in as_completed(futures):
                record(future.result())
    else:
        for args in prepared:
            record(trial(*args))
    report.update(completedAtUtc=utc_now(), elapsedSeconds=round(time.monotonic() - started, 3),
                  brokerSourceSha256After=source_hashes(), evidence=aggregate_evidence(output))
    report['discoverySessionsStarted'] = report['evidence']['auditEvents'].get('codex_discovery_model_started', 0)
    report['discoverySessionsCompleted'] = report['evidence']['auditEvents'].get('codex_discovery_completed', 0)
    report['brokerSourcesUnchanged'] = report['brokerSourceSha256After'] == report['brokerSourceSha256']
    report['passed'] = (len(report['trials']) == len(queries)
                        and all(row['passed'] for row in report['trials'])
                        and report['brokerSourcesUnchanged'])
    write_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy-file', type=Path, required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--query', default='Euler characteristic')
    parser.add_argument('--repeats', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--concurrent10', action='store_true',
                        help='Ten distinct harmless queries with fresh processes and shared cold state.')
    args = parser.parse_args()
    report = validate(args.policy_file.resolve(), args.cutoff, args.output.resolve(),
                      args.query, args.repeats, concurrent10=args.concurrent10)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
