#!/usr/bin/env python3
"""Check the actual Mach-O inputs against APIs named in Monica's App Review.

Undefined symbols identify linked calls; strings also identify dynamic lookups
and Objective-C classes. Ordinary lowercase Python/Cython names are not APIs.
Apple's own analysis remains authoritative and may identify additional APIs.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess


def scan(app):
    names = Path(__file__).with_name('appstore_rejected_apis.txt').read_text().splitlines()
    lookup = {n.lstrip('_'): n for n in names if n}
    def check(path):
        if path.is_symlink() or not path.is_file():
            return None
        with path.open('rb') as handle:
            if handle.read(4) not in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf'):
                return None
        undefined = subprocess.check_output(['nm', '-u', '-j', str(path)], stderr=subprocess.DEVNULL)
        hits = set()
        for line in undefined.splitlines():
            token = line.strip().split(b'$')[-1].lstrip(b'_').decode('utf8', 'replace')
            if token in lookup:
                hits.add(lookup[token])
        literals = subprocess.check_output(['strings', '-a', str(path)], stderr=subprocess.DEVNULL)
        for line in literals.splitlines():
            raw = line.strip().split(b'$')[-1].decode('utf8', 'replace')
            token = raw.lstrip('_')
            if token in lookup and (raw.startswith('_') or token[:1].isupper() or token.startswith(('sandbox_', 'responsibility_'))):
                hits.add(lookup[token])
        return {'path': str(path.relative_to(app)), 'apis': sorted(hits)} if hits else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [r for r in pool.map(check, app.rglob('*')) if r]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = scan(args.app.resolve())
    report = {'rejected_api_references': result, 'passed': not result}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(result))


if __name__ == '__main__':
    main()
