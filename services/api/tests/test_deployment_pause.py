import threading
from uuid import uuid4

import pytest
from sqlalchemy import text

from app import job_queue


def test_pause_keeps_checkpoint_and_does_not_spend_attempt(crm_db):
    db = crm_db
    token, identifier = uuid4(), uuid4()
    db.execute(text("""INSERT INTO jobs(id,job_type,payload,status,attempts,lease_token,lease_expires_at,locked_by)
        VALUES(:id,'ai.profile','{}','running',3,:token,now()+interval '2 minutes','old')"""), {'id': identifier, 'token': token})
    job = {'id': identifier, 'lease_token': token}
    job_queue.relinquish(db.connection(), job)
    row = db.execute(text('SELECT * FROM jobs WHERE id=:id'), job).mappings().one()
    assert row['status'] == 'queued' and row['attempts'] == 2
    assert row['lease_token'] is None and row['locked_by'] is None
    with pytest.raises(job_queue.LeaseLost):
        job_queue.relinquish(db.connection(), job)


def test_pause_interrupts_rate_limit_wait_without_catching_handler_errors():
    stop = threading.Event()
    stop.set()
    with job_queue.owned({'id': 'test'}, stop):
        with pytest.raises(job_queue.JobPaused):
            job_queue.interruptible_wait(600)
        with pytest.raises(job_queue.JobPaused):
            job_queue.checkpoint()
    job_queue.checkpoint()
    assert not issubclass(job_queue.JobPaused, Exception)


def test_stale_worker_cannot_release_new_owner(crm_db):
    identifier, token = uuid4(), uuid4()
    crm_db.execute(text("""INSERT INTO jobs(id,job_type,payload,status,attempts,lease_token,lease_expires_at)
        VALUES(:id,'ai.profile','{}','running',2,:token,now()+interval '2 minutes')"""), {'id': identifier, 'token': token})
    with pytest.raises(job_queue.LeaseLost):
        job_queue.relinquish(crm_db.connection(), {'id': identifier, 'lease_token': uuid4()})
    assert crm_db.execute(text('SELECT lease_token FROM jobs WHERE id=:id'), {'id': identifier}).scalar_one() == token
