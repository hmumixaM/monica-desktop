#!/usr/bin/env python3
"""Lower disk priority of explicitly selected NAS background containers and DB clients.

Run on the NAS as root. The default only reports the selected processes; --apply
uses best-effort level 7, and --restore-default restores the kernel default.
This affects existing sessions only. It never changes API clients, PostgreSQL
writers, durability settings, or database records. Re-run after reconnects.
"""

import argparse
import ctypes
import json
import os
import platform
import subprocess
from pathlib import Path


def docker(*args):
    return subprocess.check_output([ARGS.docker, *args], text=True).strip()


def metadata(name):
    # Do not inspect environment variables: they contain database passwords.
    return json.loads(docker('inspect', '--format',
        '{"id":{{json .Id}},"pid":{{.State.Pid}},"running":{{.State.Running}},'
        '"labels":{{json .Config.Labels}},"networks":{{json .NetworkSettings.Networks}}}', name))


def identity(pid):
    """Capture namespace PID and start time, so a recycled PID is never changed."""
    proc = Path('/proc') / str(pid)
    status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
    # comm can contain spaces/parentheses; fields after its last ')' start at 3.
    start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return (os.readlink(proc / 'ns/pid'), int(status['NSpid'].split()[-1]), start)


def members(name):
    result = {}
    for line in docker('top', name, '-eo', 'pid').splitlines()[1:]:
        pid = int(line.strip())
        try:
            result[pid] = identity(pid)
        except FileNotFoundError:
            pass
    return result


def run():
    if platform.machine() != 'x86_64' or platform.system() != 'Linux':
        raise SystemExit('This NAS utility supports Linux x86_64 only')
    postgres = metadata(ARGS.postgres)
    labels = postgres['labels']
    project = labels.get('com.docker.compose.project')
    if not postgres['running'] or not project or labels.get('com.docker.compose.service') != 'postgres':
        raise SystemExit('Expected a running Compose PostgreSQL container')
    targets, addresses = {}, {}
    for name in ARGS.imports:
        item = metadata(name)
        labels = item['labels']
        service = labels.get('com.docker.compose.service')
        background = service in {'linkedin-sync', 'archive-sync', 'worker'} or (
            service == 'api' and str(labels.get('com.docker.compose.oneoff')).lower() == 'true')
        if not item['running'] or labels.get('com.docker.compose.project') != project or not background:
            raise SystemExit('Only running background services or API one-offs in the same project may be selected')
        for network, settings in item['networks'].items():
            if network in postgres['networks'] and settings.get('IPAddress'):
                addresses[settings['IPAddress']] = name
        for pid, stamp in members(name).items():
            targets[pid] = (stamp, name, 'import')
    if not addresses:
        raise SystemExit('No import IPs share the PostgreSQL network')
    rows = json.loads(docker('exec', ARGS.postgres, 'psql', '-XAt', '-U', ARGS.user, '-d', ARGS.database,
        '-c', "SELECT coalesce(json_agg(json_build_object('pid',pid,'client',host(client_addr))), '[]') "
        "FROM pg_stat_activity WHERE backend_type='client backend' AND client_addr IS NOT NULL"))
    namespace = identity(postgres['pid'])[0]
    db_members = {stamp[1]: (pid, stamp) for pid, stamp in members(ARGS.postgres).items() if stamp[0] == namespace}
    for row in rows:
        if row['client'] in addresses and row['pid'] in db_members:
            pid, stamp = db_members[row['pid']]
            targets[pid] = (stamp, addresses[row['client']], 'database-client')
    libc = ctypes.CDLL(None, use_errno=True)
    def priority(number, pid, value=None):
        args = [ctypes.c_long(number), ctypes.c_int(1), ctypes.c_int(pid)]
        if value is not None:
            args.append(ctypes.c_int(value))
        result = libc.syscall(*args)
        if result < 0:
            raise OSError(ctypes.get_errno(), 'I/O priority syscall failed')
        return result
    for pid, (stamp, name, kind) in sorted(targets.items()):
        try:
            if identity(pid) != stamp:
                continue
            before = priority(252, pid)
            if ARGS.apply or ARGS.restore_default:
                priority(251, pid, 0 if ARGS.restore_default else (2 << 13) | 7)
            print(json.dumps({'container': name, 'kind': kind, 'pid': pid,
                'before': before, 'after': priority(252, pid), 'applied': ARGS.apply or ARGS.restore_default}), flush=True)
        except (FileNotFoundError, ProcessLookupError):
            continue


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker', default='/var/packages/ContainerManager/target/usr/bin/docker')
    parser.add_argument('--postgres', default='infra-postgres-1')
    parser.add_argument('--user', default='crm')
    parser.add_argument('--database', default='crm')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore-default', action='store_true')
    parser.add_argument('imports', nargs='+')
    ARGS = parser.parse_args()
    run()
