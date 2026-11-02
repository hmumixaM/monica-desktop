#!/usr/bin/env python3
"""Measure authenticated business requests under a separately controlled load.

Use identical paths, concurrency, warmup, data and load for before/after runs.
MONICA_COOKIE supplies a session cookie without putting it in output or argv.
"""
import argparse
import concurrent.futures
import json
import os
import statistics
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_url')
    parser.add_argument('--path', action='append', required=True)
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--warmup', type=int, default=5)
    args = parser.parse_args()
    cookie = os.environ['MONICA_COOKIE']
    def request(path):
        started = time.monotonic()
        req = urllib.request.Request(args.base_url.rstrip('/')+path, headers={'Cookie': cookie})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except (OSError, TimeoutError):
            status = 0
        return time.monotonic()-started, status
    result = {}
    for path in args.path:
        for _ in range(args.warmup):
            request(path)
        with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
            rows = list(pool.map(request, [path]*args.requests))
        durations = sorted(duration for duration, status in rows)
        result[path] = {'requests': len(rows), 'errors': sum(status != 200 for _,status in rows),
                        'p50_seconds': statistics.median(durations),
                        'p95_seconds': durations[min(len(rows)-1, int(len(rows)*.95))],
                        'p99_seconds': durations[min(len(rows)-1, int(len(rows)*.99))]}
    print(json.dumps({'concurrency': args.concurrency, 'results': result}, indent=2))


if __name__ == '__main__':
    main()
