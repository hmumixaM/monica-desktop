"""Retry logged failures only when an artificial HTTPS upgrade fails TLS validation."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import re
import socket
import ssl
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--prefetch', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:5030')
    parser.add_argument('--all-failures', action='store_true', help='Retry all logged missing media, not only TLS-upgrade failures')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('prefetch_retry', args.prefetch)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with args.log.open('rb') as log:
        failures = set(re.findall(rb'FAIL [^\n]*?cache=([a-f0-9]{32})\.bin', log.read(args.log.stat().st_size)))
    failures = {x.decode() + '.bin' for x in failures}
    resources = module.read_media(args.db, {'1', '6', '7', '15', '54'})
    candidates = []
    for item in resources:
        if not args.all_failures and not item.url.startswith('http://'):
            continue
        old_url = item.url.replace('http://', 'https://', 1)
        old_name = hashlib.md5((old_url + '|' + item.key).encode()).hexdigest() + '.bin'
        if old_name in failures or (args.all_failures and item.cache_name in failures):
            candidates.append(item)
    checks = {}
    context = ssl.create_default_context()
    for host in sorted({urlsplit(x.url).hostname for x in candidates}):
        try:
            with socket.create_connection((host, 443), timeout=8) as sock, \
                 context.wrap_socket(sock, server_hostname=host):
                pass
            checks[host] = 'valid'
        except ssl.SSLCertVerificationError:
            checks[host] = 'certificate_failure'
        except OSError:
            checks[host] = 'unconfirmed_connection_failure'
    selected = candidates if args.all_failures else [x for x in candidates if checks[urlsplit(x.url).hostname] == 'certificate_failure']
    selected = module.media_for_today(selected, SimpleNamespace(cache_dir=args.cache_dir, limit=5000, daily_cap=5000))
    report = {'tls_checks': checks, 'selected': len(selected), 'hosts': dict(Counter(urlsplit(x.url).hostname for x in selected)),
              'completed': 0, 'failed': 0, 'skipped_cached': 0, 'bytes': 0, 'state': 'planned', 'results': []}
    def save():
        temp = args.report.with_suffix('.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(args.report)
    if not args.apply:
        print(json.dumps({k:v for k,v in report.items() if k != 'results'}), flush=True)
        return
    report['state'] = 'waiting_for_cache_lock';save()
    print(json.dumps({k:v for k,v in report.items() if k != 'results'}), flush=True)
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report['state'] = 'running';save()
        for item in selected:
            path = args.cache_dir / item.cache_name
            if path.is_file() and path.stat().st_size:
                report['skipped_cached'] += 1
                continue
            start = time.monotonic()
            result = {'tid': item.tid, 'kind': item.kind, 'cache': item.cache_name, 'host': urlsplit(item.url).hostname}
            try:
                size = module.fetch(item, args.base_url, .5, 2.0)
                if not path.is_file() or path.stat().st_size != size:
                    raise ValueError('Cache file size mismatch')
                report['completed'] += 1;report['bytes'] += size
                result.update(status='cached', bytes=size)
            except (OSError, ValueError, module.HTTPException) as exc:
                report['failed'] += 1
                result.update(status='failed', error=type(exc).__name__, http=getattr(exc, 'code', None))
            result['seconds'] = round(time.monotonic() - start, 2)
            report['results'].append(result);save()
            print(json.dumps(result), flush=True)
        report['state'] = 'complete';save()
        print(json.dumps({k:v for k,v in report.items() if k != 'results'}), flush=True)


if __name__ == '__main__':
    main()
