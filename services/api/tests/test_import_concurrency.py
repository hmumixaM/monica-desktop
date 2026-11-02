import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from test_media_integration import db as db  # noqa: PLC0414


def test_signed_import_does_not_block_browsing_and_rejects_nonce_replay(db,monkeypatch):
    from app import main
    from app.security import create_import_signature_for_source

    started=threading.Event();release=threading.Event()
    from app.routers import imports_api
    project=imports_api.project_batch
    def slow_project(session,payload):
        started.set()
        if not release.wait(3):raise RuntimeError('Import blocked the event loop')
        return project(session,payload)
    monkeypatch.setattr(imports_api,'project_batch',slow_project)
    main.app.dependency_overrides[main.get_db]=lambda:db
    async def exercise():
        body=json.dumps({'source':'wechat','stream':'profiles','batch_id':'concurrent','idempotency_key':'concurrent',
                         'observed_at':datetime.now(UTC).isoformat(),'records':[]}).encode()
        stamp=str(int(time.time()));nonce=uuid4().hex
        headers={'Content-Type':'application/json','X-Import-Timestamp':stamp,'X-Import-Nonce':nonce,
                 'X-Import-Signature':create_import_signature_for_source(stamp,nonce,body,'wechat')}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
            pending=asyncio.create_task(client.post('/api/v1/imports/batches',content=body,headers=headers))
            try:
                assert await asyncio.to_thread(started.wait,2)
                assert not pending.done()
                response=await asyncio.wait_for(client.get('/health'),1)
                assert response.status_code==200
            finally:
                release.set()
                response=await pending
            assert response.status_code==201
            assert (await client.post('/api/v1/imports/batches',content=body,headers=headers)).status_code==409
            headers['X-Import-Signature']='invalid'
            assert (await client.post('/api/v1/imports/batches',content=body,headers=headers)).status_code==401
    try:asyncio.run(exercise())
    finally:main.app.dependency_overrides.pop(main.get_db,None)
