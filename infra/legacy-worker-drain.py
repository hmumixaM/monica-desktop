"""One-time, bounded upgrade bridge for workers predating graceful leases.

Keep queued rows from being claimed, wait for running results to commit, then
hold a short table gate only while stopping the idle legacy container.
"""
import select
import subprocess
import time

GUARD = r'''
import sys,time
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.db import engine

def gate(db):
    nested=db.begin_nested()
    try:
        db.execute(text("SET LOCAL lock_timeout='2s'"))
        db.execute(text('LOCK TABLE jobs IN SHARE MODE'))
        if db.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one()==0:
            print('DRAINED',flush=True)
            assert sys.stdin.readline().strip()=='STOPPED'
            nested.commit()
            return True
    except OperationalError:
        pass
    nested.rollback()
    return False

deadline=time.monotonic()+960
report_at=0
with engine.connect() as db:
    while time.monotonic()<deadline:
        db.execute(text("SET LOCAL lock_timeout='2s'"))
        db.execute(text("SELECT count(*) FROM (SELECT id FROM jobs WHERE status='queued' FOR UPDATE SKIP LOCKED) AS queued")).scalar_one()
        active=db.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one()
        if not active:
            if gate(db):
                db.commit();print('RELEASED',flush=True);break
            # A periodic enqueue may need a queued row we locked. Let it
            # finish, then request the table gate ahead of the next claim.
            db.rollback()
            if gate(db):
                db.commit();print('RELEASED',flush=True);break
            db.rollback()
        if time.monotonic()>=report_at:
            print('drain: waiting for existing running work',flush=True)
            report_at=time.monotonic()+30
        time.sleep(.5)
    else:
        raise RuntimeError('Worker did not drain within 16 minutes; no container was stopped')
'''


def drain(release, api, worker):
    if not release.inspect(worker)['State']['Running']:
        return
    process = subprocess.Popen([release.docker, 'exec', '-i', api,
                                '/app/.venv/bin/python', '-u', '-c', GUARD],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic()+990
    try:
        while time.monotonic()<deadline:
            ready, _, _ = select.select([process.stdout], [], [], 2)
            if ready:
                line = process.stdout.readline()
                if line.strip() == 'DRAINED':
                    release.run([release.docker, 'stop', '--time', '30', worker])
                    assert not release.inspect(worker)['State']['Running']
                    process.stdin.write('STOPPED\n'); process.stdin.flush()
                    process.stdin.close()
                    # The database acknowledgement is authoritative. Docker's
                    # exec client can lag after the guard has committed/exited.
                    released = time.monotonic()+120
                    while time.monotonic() < released:
                        ready, _, _ = select.select([process.stdout], [], [], 2)
                        if not ready:
                            continue
                        acknowledgement = process.stdout.readline()
                        if acknowledgement.strip() == 'RELEASED':
                            print('worker drained with zero running jobs: '+worker, flush=True)
                            return
                        if not acknowledgement and process.poll() is not None:
                            break
                    raise RuntimeError('Drain guard did not acknowledge database release after stop')
                if line:
                    print(line.strip(), flush=True)
                if not line and process.poll() is not None:
                    break
        raise RuntimeError('Worker drain failed; deployment aborted')
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
