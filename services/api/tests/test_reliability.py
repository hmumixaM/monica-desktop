"""Regression tests for failure, ownership and bounded memory guarantees."""
import io
import json
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app import aggregate_cache, data_versions, job_queue, jobs
from app.faces.device import _tar
from app.sync_archives import read_media_import_queue


@pytest.fixture
def shared_engine(crm_db):
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    crm_db.commit()
    engine = create_engine(crm_db.bind.engine.url, connect_args={'options': f'-c search_path={schema},public'})
    try:
        yield engine
    finally:
        engine.dispose()


def test_uncommitted_and_out_of_order_versions(shared_engine):
    engine = shared_engine
    with engine.connect() as a, engine.connect() as b, engine.connect() as reader:
        before = data_versions.current(reader, ['profiles'])
        a.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','late')"))
        assert data_versions.current(reader, ['profiles']) == before
        b.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','early')"))
        b.commit()
        first = data_versions.current(reader, ['profiles'])
        assert first['profiles'] == before['profiles'] + 1
        with engine.begin() as reducer:
            data_versions.compact(reducer)
        assert data_versions.current(reader, ['profiles']) == first
        a.commit()
        assert data_versions.current(reader, ['profiles'])['profiles'] == first['profiles'] + 1
        a.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','rolled back')"))
        a.rollback()
        assert data_versions.current(reader, ['profiles'])['profiles'] == first['profiles'] + 1


def test_lease_takeover_fences_old_ack_and_result(shared_engine):
    engine = shared_engine
    job_queue.install_fence(engine)
    with engine.begin() as db:
        job_queue.enqueue(db, 'ai.profile', {'profile_id': str(uuid4())})
    a = job_queue.claim(engine, 'a', 'ai')
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET lease_expires_at=now()-interval '1 second'"))
    b = job_queue.claim(engine, 'b', 'ai')
    assert a['lease_token'] != b['lease_token']
    with pytest.raises(job_queue.LeaseLost), engine.begin() as db:
        job_queue.finish(db, a)
    with pytest.raises(job_queue.LeaseLost), job_queue.owned(a), engine.begin() as db:
        db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','stale result')"))
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM profiles WHERE display_name='stale result'")).scalar_one() == 0
        job_queue.finish(db, b)


@pytest.mark.parametrize('failure', ['locked', 'expired'])
def test_commit_fence_failure_rolls_back_before_connection_reuse(shared_engine, failure):
    from sqlalchemy.exc import OperationalError

    with shared_engine.connect() as db:
        schema = db.execute(text('SELECT current_schema()')).scalar_one()
    engine = create_engine(shared_engine.url, pool_size=1, max_overflow=0,
                           pool_pre_ping=True,
                           connect_args={'options': f'-c search_path={schema},public -c lock_timeout=100'})
    job_queue.install_fence(engine)
    locker = shared_engine.connect()
    try:
        with engine.begin() as db:
            job_queue.enqueue(db, 'ai.profile', {'profile_id': str(uuid4())})
        job = job_queue.claim(engine, 'commit-fence', 'ai')
        if failure == 'locked':
            job_queue.assert_owner(locker, job)
            expected = OperationalError
        else:
            with shared_engine.begin() as db:
                db.execute(text("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second'"))
            expected = job_queue.LeaseLost
        with pytest.raises(expected), job_queue.owned(job), engine.connect() as db:
            db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','failed commit fence')"))
            db.commit()
        # A one-connection pool with pre-ping exercises psycopg's autocommit
        # transition on checkout, including the INERROR failure seen on NAS.
        with engine.connect() as db:
            assert db.execute(text('SELECT 1')).scalar_one() == 1
            assert db.execute(text("SELECT count(*) FROM profiles WHERE display_name='failed commit fence'")).scalar_one() == 0
    finally:
        locker.rollback()
        locker.close()
        engine.dispose()


@pytest.mark.parametrize('expires', [False, True])
def test_locked_media_result_does_not_kill_a_valid_lease_or_extend_an_expired_one(shared_engine, expires):
    import time

    with shared_engine.connect() as db:
        schema = db.execute(text('SELECT current_schema()')).scalar_one()
    heartbeat_engine = create_engine(shared_engine.url, pool_pre_ping=True,
                                    connect_args={'options': f'-c search_path={schema},public -c lock_timeout=100'})
    try:
        with shared_engine.begin() as db:
            job_queue.enqueue(db, 'media.download', {'entity_type': 'profile',
                'entity_id': str(uuid4()), 'role': 'avatar', 'url': 'https://example/media'})
        job = job_queue.claim(shared_engine, 'media-result', 'media')
        if expires:
            with shared_engine.begin() as db:
                db.execute(text("UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '300 milliseconds'"))
        with shared_engine.begin() as result:
            job_queue.assert_owner(result, job)
            expiry = result.execute(text('SELECT lease_expires_at FROM jobs WHERE id=:id'), job).scalar_one()
            if expires:
                time.sleep(.35)
            assert job_queue.heartbeat(heartbeat_engine, job) is not expires
            # A locked final result is observed, never renewed through a read.
            assert result.execute(text('SELECT lease_expires_at FROM jobs WHERE id=:id'), job).scalar_one() == expiry
            if not expires:
                job_queue.finish(result, job)
        if expires:
            replacement = job_queue.claim(shared_engine, 'replacement', 'media')
            assert replacement['lease_token'] != job['lease_token']
            with pytest.raises(job_queue.LeaseLost), shared_engine.begin() as db:
                job_queue.finish(db, job)
        else:
            assert not job_queue.heartbeat(heartbeat_engine, job)
    finally:
        heartbeat_engine.dispose()


def test_concurrent_enqueue_and_refresh_during_execution(shared_engine):
    engine = shared_engine
    payload = {'profile_id': str(uuid4())}
    def enqueue(_):
        with engine.begin() as db:
            return job_queue.enqueue(db, 'ai.profile', payload)
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(enqueue, range(4)))
    assert len(set(ids)) == 1
    job = job_queue.claim(engine, 'owner', 'ai')
    with engine.begin() as db:
        job_queue.enqueue(db, 'ai.profile', payload, refresh=True)
    with engine.begin() as db:
        job_queue.finish(db, job)
    assert job_queue.claim(engine, 'next', 'ai')['id'] == job['id']
    assert job_queue.claim(engine, 'unrelated', 'media') is None


def test_heavy_io_does_not_treat_other_threads_as_nested(tmp_path, monkeypatch):
    monkeypatch.setenv('STAGING_ROOT', str(tmp_path))
    entered = threading.Event()
    def second():
        with jobs.heavy_io('second'):
            entered.set()
    with jobs.heavy_io('first'):
        thread = threading.Thread(target=second)
        thread.start()
        assert not entered.wait(.05)
    thread.join(2)
    assert entered.is_set()


def test_media_queue_quarantines_bad_line_and_waits_for_partial(tmp_path):
    queue, state = tmp_path/'queue.jsonl', tmp_path/'state.json'
    valid = json.dumps({'version': 1, 'kind': 'photo', 'cache_name': 'a', 'tid': 1}).encode()+b'\n'
    queue.write_bytes(b'bad\n'+valid+b'{"version":')
    rows, offset = read_media_import_queue(queue, state)
    assert len(rows) == 1 and offset == 4+len(valid)
    assert json.loads((tmp_path/'media-queue-quarantine/0.json').read_text())['offset'] == 0
    state.write_text(json.dumps({'offset': offset}))
    assert read_media_import_queue(queue, state) == ([], offset)


def test_tar_opens_only_consumed_files_and_has_valid_content(tmp_path):
    path = tmp_path/'photo'; path.write_bytes(b'a'*(1024*1024))
    stream = _tar([('first', path), ('missing', tmp_path/'missing')])
    assert len(next(stream)) == 512
    assert len(next(stream)) == 256*1024
    stream.close()  # Does not try to open the second file.
    packed = b''.join(_tar([('first', path)]))
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
        assert archive.extractfile('first').read() == path.read_bytes()


def test_cache_singleflight_and_byte_budget(monkeypatch):
    monkeypatch.setattr(aggregate_cache, 'current', lambda *_: {'profiles': 1})
    entered, release = threading.Event(), threading.Event()
    calls = []
    def compute(_):
        calls.append(1); entered.set(); release.wait(2)
        return {'value': 'v'*1000}
    with ThreadPoolExecutor(6) as pool:
        futures = [pool.submit(aggregate_cache.cached_aggregate, None, 'single', ['profiles'], 1, compute) for _ in range(6)]
        assert entered.wait(1)
        release.set()
        assert all(f.result() == {'value': 'v'*1000} for f in futures)
    assert len(calls) == 1
    monkeypatch.setattr(aggregate_cache, 'MEMORY_BYTES', 1800)
    for i in range(10):
        aggregate_cache._remember(str(i), {'value': 'v'*1000})
    assert sum(aggregate_cache._memory_sizes.values()) <= 1800


def test_complete_oversized_jsonl_is_quarantined_but_partial_waits(tmp_path):
    queue, state = tmp_path/'queue', tmp_path/'state'
    oversized = b'x' * (1024*1024+8)
    queue.write_bytes(oversized)
    assert read_media_import_queue(queue, state) == ([], 0)
    queue.write_bytes(oversized+b'\n')
    assert read_media_import_queue(queue, state) == ([], len(oversized)+1)
    assert (tmp_path/'media-queue-quarantine/0.bin').read_bytes() == oversized+b'\n'


def test_migration_lock_and_autocommit_path(shared_engine, tmp_path):
    from app.db import run_migrations
    (tmp_path/'001.sql').write_text('CREATE TABLE migration_probe(id int)')
    (tmp_path/'002.sql').write_text('-- monica: autocommit\nCREATE INDEX CONCURRENTLY IF NOT EXISTS probe_idx ON migration_probe(id)')
    with shared_engine.connect() as lock, shared_engine.connect() as runner:
        lock.execute(text('SELECT pg_advisory_lock(11803351)')); lock.commit()
        with pytest.raises(RuntimeError, match='Another deployment'):
            run_migrations(runner, tmp_path)
        lock.execute(text('SELECT pg_advisory_unlock(11803351)')); lock.commit()
        run_migrations(runner, tmp_path)
        run_migrations(runner, tmp_path)
        assert runner.execute(text('SELECT count(*) FROM schema_migrations WHERE version IN (\'001.sql\',\'002.sql\')')).scalar_one() == 2


def test_tar_peak_allocation_is_bounded_for_large_batch(tmp_path):
    import tracemalloc
    path = tmp_path/'large'
    with path.open('wb') as stream:
        stream.truncate(32*1024*1024)
    tracemalloc.start()
    try:
        total = sum(len(chunk) for chunk in _tar([(str(i), path) for i in range(4)]))
        _, peak = tracemalloc.get_traced_memory()
        assert total > 128*1024*1024
        assert peak < 2*1024*1024
    finally:
        tracemalloc.stop()


def test_source_phase_records_failure_and_success_independently(shared_engine, monkeypatch):
    from app.runtime_status import phase
    monkeypatch.setattr('app.db.engine', shared_engine)
    with pytest.raises(ValueError), phase('wechat', 'prefetch'):
        raise ValueError('network unavailable')
    with phase('instagram', 'import') as report:
        report['state'] = 'unchanged'
    with shared_engine.connect() as db:
        rows = {r.source: r for r in db.execute(text('SELECT * FROM source_runtime'))}
        assert rows['wechat'].status == 'failed' and rows['wechat'].next_retry_at is not None
        assert rows['instagram'].status == 'unchanged' and rows['instagram'].last_success_at is not None


def test_ai_claims_reserve_turns_for_intelligence_and_visual_work(shared_engine):
    with shared_engine.begin() as db:
        for _ in range(12):
            job_queue.enqueue(db, 'ai.story_media_one', {'story_id': str(uuid4())})
        db.execute(text("UPDATE jobs SET priority=10,available_at=now()-interval '4 days'"))
        for _ in range(4):
            job_queue.enqueue(db, 'ai.profile', {'profile_id': str(uuid4())})
    kinds = []
    for turn in range(8):
        job = job_queue.claim(shared_engine, 'fair-ai', 'ai', prefer_ai_intelligence=turn % 4 != 3)
        kinds.append(job['job_type'])
        with shared_engine.begin() as db:
            job_queue.finish(db, job)
    assert kinds[:4] == ['ai.profile'] * 3 + ['ai.story_media_one']
    assert kinds[4:] == ['ai.profile'] + ['ai.story_media_one'] * 3


def test_reconnect_backfill_does_not_starve_profiles_or_visual_assembly(shared_engine):
    with shared_engine.begin() as db:
        for _ in range(12):
            job_queue.enqueue(db, 'ai.reconnect_one', {'profile_id': str(uuid4())})
            job_queue.enqueue(db, 'ai.story_media_one', {'story_id': str(uuid4())})
        db.execute(text("UPDATE jobs SET priority=20,available_at=now()-interval '4 days'"))
        for _ in range(4):
            job_queue.enqueue(db, 'ai.profile', {'profile_id': str(uuid4())})
    kinds = []
    for turn in range(8):
        slot=turn % 4
        job=job_queue.claim(shared_engine,'fair-ai','ai',prefer_ai_intelligence=slot!=3,
                            prefer_ai_profiles=(slot<2) if slot!=3 else None)
        kinds.append(job['job_type'])
        with shared_engine.begin() as db:
            job_queue.finish(db,job)
    assert kinds == ['ai.profile','ai.profile','ai.reconnect_one','ai.story_media_one'] * 2


@pytest.mark.parametrize('result_arrived', [False, True])
def test_visual_dependency_wait_does_not_consume_attempts_or_lose_wakeup(shared_engine, result_arrived):
    with shared_engine.begin() as db:
        job_queue.enqueue(db, 'ai.post_media_one', {'post_id': str(uuid4())})
    job = job_queue.claim(shared_engine, 'waiting', 'ai')
    if result_arrived:
        with shared_engine.begin() as db:
            db.execute(text('UPDATE jobs SET rerun_requested=true WHERE id=:id'), job)
    with job_queue.owned(job), shared_engine.begin() as db:
        job_queue.defer(db, job, 'Waiting for combined image/OCR reading')
    with shared_engine.connect() as db:
        row = db.execute(text('SELECT *,available_at<=now() AS due FROM jobs WHERE id=:id'), job).mappings().one()
        assert row['status'] == 'queued' and row['attempts'] == 0
        assert row['completed_at'] is None and row['lease_token'] is None
        assert row['due'] is result_arrived
    assert bool(job_queue.claim(shared_engine, 'next', 'ai')) is result_arrived


def test_ai_runner_defers_missing_ocr_instead_of_acknowledging_success(shared_engine, monkeypatch):
    from app.ai import jobs as ai_jobs
    from app.ai import post_media
    from app.config import get_settings

    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    try:
        job_queue.install_fence(shared_engine)
        with shared_engine.begin() as db:
            job_queue.enqueue(db, 'ai.post_media_one', {'post_id': str(uuid4())})
        job = job_queue.claim(shared_engine, 'dependency', 'ai')
        monkeypatch.setattr(post_media, 'read_post', lambda *_: {'waiting_for_image_reading': True})
        with job_queue.owned(job):
            ai_jobs.run_job(shared_engine, job)
        with shared_engine.connect() as db:
            row = db.execute(text('SELECT status,attempts,completed_at FROM jobs WHERE id=:id'), job).one()
            assert row.status == 'queued' and row.attempts == 0 and row.completed_at is None
    finally:
        get_settings.cache_clear()


def test_source_health_detects_stalled_progress_despite_live_heartbeat(shared_engine):
    from sqlalchemy.orm import Session

    from app.ai.router import task_status

    with shared_engine.begin() as db:
        db.execute(text("""INSERT INTO source_runtime(source,phase,status,started_at,heartbeat_at,details)
            VALUES ('archives','moments','running',now()-interval '2 hours',now(),'{}'),
                   ('archives','contacts','running',now()-interval '2 hours',now(),
                    jsonb_build_object('last_progress_at',now(),'processed',500))"""))
    with Session(shared_engine) as session:
        status = task_status(session, {})
    rows = {r['phase']: r for r in status['sources']}
    assert rows['moments']['stale'] and rows['moments']['progress_stalled']
    assert not rows['contacts']['stale'] and not rows['contacts']['progress_stalled']


def test_real_worker_process_commits_results_and_acknowledgement(shared_engine, tmp_path):
    import os
    import signal
    import subprocess
    import time
    from pathlib import Path
    schema = shared_engine.url.query.get('options')
    with shared_engine.connect() as db:
        schema = db.execute(text('SELECT current_schema()')).scalar_one()
    with shared_engine.begin() as db:
        job_id = job_queue.enqueue(db, 'metrics.refresh', {'source': 'wechat'})
    root = Path(__file__).resolve().parents[3]
    url = shared_engine.url.update_query_dict({'options': f'-c search_path={schema},public'}).render_as_string(hide_password=False)
    env = {**os.environ, 'DATABASE_URL': url, 'WORKER_RESOURCE': 'db', 'PYTHONPATH': str(root/'services/api'),
           'STAGING_ROOT': str(tmp_path/'staging')}
    with (tmp_path/'worker.log').open('w+') as output:
        process = subprocess.Popen([str(root/'services/api/.venv/bin/python'), str(root/'services/worker/worker.py')],
                                   env=env, cwd=root, stdout=output, stderr=output)
        try:
            deadline = time.monotonic()+20
            while time.monotonic() < deadline:
                with shared_engine.connect() as db:
                    state = db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one()
                if state == 'complete':
                    break
                assert process.poll() is None
                time.sleep(.1)
            assert state == 'complete'
            with shared_engine.connect() as db:
                assert db.execute(text('SELECT count(*) FROM global_metric_snapshots')).scalar_one() > 0
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


def test_database_worker_allows_heartbeat_during_result_transaction(shared_engine, monkeypatch):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import event

    path = Path(__file__).resolve().parents[3] / 'services/worker/worker.py'
    spec = importlib.util.spec_from_file_location('heartbeat_worker', path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker.engine.dispose()
    monkeypatch.setattr(worker, 'engine', shared_engine)
    with shared_engine.begin() as db:
        job_queue.enqueue(db, 'metrics.refresh', {'source': 'wechat'})
    job = job_queue.claim(shared_engine, 'heartbeat-test', 'db')
    entered, release = threading.Event(), threading.Event()

    def pause_metrics(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT 1 FROM global_metric_snapshots'):
            entered.set()
            assert release.wait(5), 'test did not release the result transaction'

    event.listen(shared_engine, 'before_cursor_execute', pause_metrics)
    try:
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(worker.execute_job, job)
            try:
                assert entered.wait(5)
                with shared_engine.begin() as db:
                    db.execute(text("SET LOCAL lock_timeout='200ms'"))
                    renewed = db.execute(text("""UPDATE jobs SET heartbeat_at=now(),
                        lease_expires_at=now()+interval '2 minutes'
                        WHERE id=:id AND lease_token=:lease_token AND status='running'
                          AND lease_expires_at>clock_timestamp()"""), job).rowcount
                    assert renewed == 1
            finally:
                release.set()
            result.result(timeout=5)
        with shared_engine.connect() as db:
            assert db.execute(text('SELECT status FROM jobs WHERE id=:id'), job).scalar_one() == 'complete'
    finally:
        release.set()
        event.remove(shared_engine, 'before_cursor_execute', pause_metrics)


def test_ai_final_result_rolls_back_when_acknowledgement_fails(shared_engine, monkeypatch):
    from app.ai import jobs as ai_jobs
    from app.ai import post_media
    from app.config import get_settings
    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    job_queue.install_fence(shared_engine)
    with shared_engine.begin() as db:
        job_queue.enqueue(db, 'ai.post_media_one', {'post_id': str(uuid4())})
    job = job_queue.claim(shared_engine, 'ai', 'ai')
    def result(db, _payload):
        db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','atomic model result')"))
        job_queue.commit_result(db)
        return {}
    monkeypatch.setattr(post_media, 'read_post', result)
    def failed_ack(*_):
        raise RuntimeError('ack connection failed')
    monkeypatch.setattr(ai_jobs, 'finish', failed_ack)
    with pytest.raises(RuntimeError, match='ack connection'), job_queue.owned(job):
        ai_jobs.run_job(shared_engine, job)
    with shared_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM profiles WHERE display_name='atomic model result'")).scalar_one() == 0
        assert db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job['id']}).scalar_one() == 'running'
    get_settings.cache_clear()


def test_two_media_workers_serialize_only_the_same_attachment(shared_engine):
    slot = {'entity_type': 'profile', 'entity_id': str(uuid4()), 'role': 'avatar'}
    with shared_engine.begin() as db:
        first = job_queue.enqueue(db, 'media.download', {**slot, 'url': 'https://example/old'})
        second = job_queue.enqueue(db, 'media.download', {**slot, 'url': 'https://example/new'})
        unrelated = job_queue.enqueue(db, 'media.download', {**slot, 'entity_id': str(uuid4()), 'url': 'https://example/other'})
        db.execute(text("UPDATE jobs SET available_at=now()-interval '1 hour' WHERE id=:id"), {'id': first})
    old = job_queue.claim(shared_engine, 'first', 'media')
    other = job_queue.claim(shared_engine, 'second', 'media')
    assert old['id'] == first and other['id'] == unrelated
    assert job_queue.claim(shared_engine, 'third', 'media') is None
    with shared_engine.begin() as db:
        job_queue.finish(db, old)
    assert job_queue.claim(shared_engine, 'next', 'media')['id'] == second


def test_actual_model_call_reservation_uses_task_spacing(shared_engine, monkeypatch):
    from app.ai.client import reserve_call
    monkeypatch.setattr('app.db.engine', shared_engine)
    sleeps = []
    monkeypatch.setattr('app.ai.client.interruptible_wait', sleeps.append)
    with job_queue.owned({'job_type': 'ai.profile'}):
        reserve_call('writer'); reserve_call('writer')
    assert sleeps[0] == 0 and 7 < sleeps[1] <= 12
    with shared_engine.connect() as db:
        assert not db.execute(text("SELECT 1 FROM pg_stat_activity WHERE state='idle in transaction' AND query LIKE '%model_call_slots%'" )).first()


@pytest.mark.parametrize('shared_person', [False, True])
def test_scoped_account_links_isolate_sources_and_serialize_shared_person(shared_engine, monkeypatch, shared_person):
    from app.account_links import set_link
    monkeypatch.setenv('MONICA_SCOPED_OWNERSHIP_LOCKS', '1')
    accounts = [uuid4(), uuid4()]
    people = [uuid4(), uuid4()]
    with shared_engine.begin() as db:
        for person in people:
            db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Concurrent')"), {'id': person})
        for account, provider in zip(accounts, ['instagram', 'linkedin'], strict=True):
            db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,migrated_at) VALUES(:id,:provider,:external,'person','Source',now())"),
                       {'id': account, 'provider': provider, 'external': str(account)})
    ready, release, finished = threading.Event(), threading.Event(), threading.Event()
    def first():
        with shared_engine.begin() as db:
            set_link(db, accounts[0], people[0]); ready.set(); release.wait(5)
    def second():
        with shared_engine.begin() as db:
            set_link(db, accounts[1], people[0] if shared_person else people[1])
        finished.set()
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(first)
        assert ready.wait(2)
        b = pool.submit(second)
        try:
            if shared_person:
                assert not finished.wait(.1)
            else:
                assert finished.wait(2)
        finally:
            release.set()
        a.result(); b.result()
    with shared_engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM source_account_links')).scalar_one() == 2


def test_busy_database_returns_retryable_http_status(shared_engine, monkeypatch):
    from contextlib import contextmanager

    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.db import get_db
    monkeypatch.setattr('app.db.ApiSessionLocal', sessionmaker(shared_engine))
    with shared_engine.begin() as db:
        person = db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Locked') RETURNING id")).scalar_one()
    with shared_engine.begin() as locker:
        locker.execute(text('SELECT id FROM profiles WHERE id=:id FOR UPDATE'), {'id': person})
        with pytest.raises(HTTPException) as error, contextmanager(get_db)() as request:
            request.execute(text("SET LOCAL lock_timeout='20ms'"))
            request.execute(text("UPDATE profiles SET display_name='Waiting' WHERE id=:id"), {'id': person})
        assert error.value.status_code == 503 and error.value.headers['Retry-After'] == '2'


def test_migration_lock_conflict_cannot_apply_schema_or_leak_timeout(shared_engine, tmp_path):
    from app.db import run_migrations
    (tmp_path/'001_probe.sql').write_text('CREATE TABLE deployment_probe(id integer)')
    with shared_engine.connect() as owner, shared_engine.connect() as candidate:
        owner.execute(text('SELECT pg_advisory_lock(11803351)')); owner.commit()
        before = candidate.execute(text('SHOW statement_timeout')).scalar_one(); candidate.commit()
        try:
            with pytest.raises(RuntimeError, match='Another deployment'):
                run_migrations(candidate, tmp_path)
            assert candidate.execute(text('SHOW statement_timeout')).scalar_one() == before
            assert candidate.execute(text("SELECT to_regclass('deployment_probe')")).scalar_one() is None
            candidate.commit()
        finally:
            assert owner.execute(text('SELECT pg_advisory_unlock(11803351)')).scalar_one()
            owner.commit()
        run_migrations(candidate, tmp_path)
        assert candidate.execute(text("SELECT to_regclass('deployment_probe')")).scalar_one() is not None


def test_online_index_migration_runs_outside_transaction_and_replays_safely(shared_engine, tmp_path):
    from app.db import run_migrations
    (tmp_path/'001_online.sql').write_text('-- monica: autocommit\nCREATE INDEX CONCURRENTLY IF NOT EXISTS deployment_profile_name ON profiles(display_name)')
    with shared_engine.connect() as candidate:
        run_migrations(candidate, tmp_path)
        run_migrations(candidate, tmp_path)
        assert candidate.execute(text("SELECT indisvalid FROM pg_index WHERE indexrelid='deployment_profile_name'::regclass")).scalar_one()
        assert candidate.execute(text("SELECT count(*) FROM schema_migrations WHERE version='001_online.sql'")).scalar_one() == 1


def test_lease_bootstrap_preserves_terminal_history_and_reclassifies_reactivation():
    """A live upgrade must not rewrite all historical payloads to start workers."""
    import os
    from pathlib import Path
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required')
    engine = create_engine(url)
    schema = 'bootstrap_' + uuid4().hex
    migrations = Path(__file__).parents[3]/'migrations'
    with engine.connect() as db:
        try:
            db.execute(text(f'CREATE SCHEMA {schema}'))
            db.execute(text(f'SET search_path TO {schema},public'))
            for path in sorted(migrations.glob('*.sql')):
                if path.name < '067_job_leases.sql':
                    db.exec_driver_sql(path.read_text())
            subject = str(uuid4())
            active = db.execute(text("""INSERT INTO jobs(job_type,payload)
                VALUES('ai.post_media_one',jsonb_build_object('post_id',CAST(:id AS text))) RETURNING id"""), {'id': subject}).scalar_one()
            terminal = db.execute(text("""INSERT INTO jobs(job_type,payload,status)
                VALUES('media.download',jsonb_build_object('entity_type','social_post','entity_id',CAST(:id AS text),
                    'role','media-1','url','https://media.licdn.com/legacy'),'complete') RETURNING id"""), {'id': subject}).scalar_one()
            failed = db.execute(text("""INSERT INTO jobs(job_type,payload,status)
                VALUES('ai.post_media_one',jsonb_build_object('post_id',CAST(:id AS text)),'failed') RETURNING id"""), {'id': str(uuid4())}).scalar_one()
            db.exec_driver_sql("""CREATE FUNCTION reject_history_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF OLD.status='complete' THEN RAISE EXCEPTION 'Historical payload rewritten'; END IF;
                RETURN NEW; END $$;
                CREATE TRIGGER protect_history BEFORE UPDATE ON jobs
                FOR EACH ROW EXECUTE FUNCTION reject_history_rewrite();""")
            db.commit()
            for path in sorted(migrations.glob('*.sql')):
                if path.name >= '067_job_leases.sql':
                    db.exec_driver_sql(path.read_text())
            db.commit()
            assert db.execute(text('SELECT dedupe_key,resource_class FROM jobs WHERE id=:id'), {'id': active}).one() == (subject, 'ai')
            assert db.execute(text('SELECT resource_class FROM jobs WHERE id=:id'), {'id': failed}).scalar_one() == 'ai'
            assert db.execute(text('SELECT dedupe_key,resource_class FROM jobs WHERE id=:id'), {'id': terminal}).one() == (None, None)
            from app.ai.router import task_status
            with Session(bind=db) as session:
                status = task_status(session, {})
            completed = next(row for row in status['queues'] if row['job_type']=='media.download' and row['status']=='complete')
            assert completed['resource_class'] == 'media'
            db.exec_driver_sql('DROP TRIGGER protect_history ON jobs')
            db.execute(text("UPDATE jobs SET status='queued' WHERE id=:id"), {'id': terminal})
            restored = db.execute(text('SELECT dedupe_key,resource_class FROM jobs WHERE id=:id'), {'id': terminal}).one()
            assert restored.dedupe_key is not None and restored.resource_class == 'media'
        finally:
            db.rollback()
            db.exec_driver_sql('SET search_path TO public')
            db.exec_driver_sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
            db.commit()
            engine.dispose()


def test_identity_upgrade_supports_already_applied_original_lease_schema(crm_db):
    from pathlib import Path
    db = crm_db
    # Reproduce the original schema's NOT NULL/default and payload-only trigger.
    db.execute(text('DROP TRIGGER job_identity ON jobs'))
    db.execute(text("ALTER TABLE jobs ALTER COLUMN resource_class SET DEFAULT 'db'"))
    db.execute(text('ALTER TABLE jobs ALTER COLUMN resource_class SET NOT NULL'))
    db.execute(text("""CREATE OR REPLACE FUNCTION populate_job_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.dedupe_key:=job_subject(NEW.job_type,NEW.payload);
        NEW.resource_class:=CASE WHEN left(NEW.job_type,3)='ai.' THEN 'ai'
            WHEN NEW.job_type='media.download' THEN 'media' ELSE 'db' END; RETURN NEW; END $$;
        CREATE TRIGGER job_identity BEFORE INSERT OR UPDATE OF job_type,payload ON jobs
        FOR EACH ROW EXECUTE FUNCTION populate_job_identity();
        DROP FUNCTION job_resource(text);"""))
    legacy = db.execute(text("""INSERT INTO jobs(job_type,payload,status)
        VALUES('ai.profile',jsonb_build_object('profile_id',CAST(:id AS text)),'complete') RETURNING id"""), {'id': str(uuid4())}).scalar_one()
    path = Path(__file__).parents[3]/'migrations/071_legacy_job_identity_compat.sql'
    db.execute(text(path.read_text()))
    db.execute(text('UPDATE jobs SET resource_class=NULL WHERE id=:id'), {'id': legacy})
    db.execute(text("UPDATE jobs SET status='queued' WHERE id=:id"), {'id': legacy})
    assert db.execute(text('SELECT resource_class FROM jobs WHERE id=:id'), {'id': legacy}).scalar_one() == 'ai'
