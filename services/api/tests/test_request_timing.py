import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.request_timing import current_timing, request_timing


def test_timing_follows_sync_queries_and_isolates_concurrent_requests():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required')
    engine = create_engine(url)
    app = FastAPI()
    app.middleware('http')(request_timing)

    @app.get('/query')
    def query(count: int):
        with engine.connect() as db:
            for _ in range(count):
                db.execute(text('SELECT pg_sleep(0.02)'))
        return {'ok': True}

    @app.get('/no-query')
    async def no_query():
        await asyncio.sleep(0.01)
        return {'ok': True}

    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as threads:
            futures = [threads.submit(client.get, path) for path in
                       ('/query?count=1', '/query?count=3', '/no-query')]
            responses = [f.result() for f in futures]
        for response, count in zip(responses, (1, 3, 0)):
            assert response.status_code == 200
            header = response.headers['server-timing']
            assert f'queries;desc="{count}"' in header
            sql_ms = float(re.search(r'sql;dur=([\d.]+)', header)[1])
            app_ms = float(re.search(r'app;dur=([\d.]+)', header)[1])
            assert sql_ms >= count * 18
            assert app_ms >= sql_ms
            assert 'pg_sleep' not in header and 'count=' not in header
        assert current_timing.get() is None
    finally:
        engine.dispose()
