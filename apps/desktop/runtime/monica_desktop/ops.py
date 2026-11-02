"""Short-lived database/model operations executed in the bundled Python."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta


def connection():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:"), connect_timeout=5)


def initialize():
    import psycopg
    from psycopg import sql
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:")
    admin = url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_database WHERE datname='crm'").fetchone():
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("crm")))
    from app.db import migrate
    from app.platform_store import initialize as initialize_platform
    migrate()
    for provider in ("wechat", "instagram", "linkedin"):
        initialize_platform(provider)
    import subprocess
    import sys
    subprocess.run([sys.executable, "-m", "app.desktop_init", "initialize"], check=True)
    if os.environ.get('CDC_ENABLED') == 'true':
        # History is optional to the live pipeline. Its errors remain visible
        # in consumer status/logs while intake and browsing can still start.
        subprocess.run([sys.executable, '-m', 'app.cdc', 'initialize'], check=False)
    token = os.environ["MONICA_FACES_TOKEN"]
    with connection() as db:
        if not db.execute("SELECT 1 FROM import_service_tokens WHERE token_hash=%s AND revoked_at IS NULL",
                          (hashlib.sha256(token.encode()).hexdigest(),)).fetchone():
            db.execute("INSERT INTO import_service_tokens(source,label,token_hash) VALUES('faces','Monica desktop',%s)",
                       (hashlib.sha256(token.encode()).hexdigest(),))
    return {"initialized": True}


def status():
    from psycopg.rows import dict_row
    with connection() as db:
        row = db.execute("""SELECT (SELECT count(*) FROM app_users),
            (SELECT count(*) FROM profiles WHERE archived_at IS NULL),
            (SELECT count(*) FROM jobs WHERE status IN ('queued','running')),
            (SELECT count(*) FROM social_posts),
            (SELECT coalesce(sum(projected_count),0) FROM message_source_cursors)""").fetchone()
        with db.cursor(row_factory=dict_row) as cursor:
            phases = cursor.execute("""SELECT source,phase,status,heartbeat_at,last_success_at,details
                FROM source_runtime ORDER BY source,phase""").fetchall()
            from app.pipeline_status import read
            pipeline = read(lambda query: cursor.execute(query).fetchall())
        return {"account_created": bool(row[0]), "profiles": row[1], "pending_jobs": row[2],
                "posts_imported": row[3], "messages_imported": int(row[4]),
                "import_phases": [public_phase(phase) for phase in phases], 'pipeline': pipeline}


def public_phase(row):
    """Only operational counters cross into the native settings UI."""
    details = row.get("details") or {}
    counters = {key: value for key, value in details.items()
                if key in {"processed", "total", "inserted", "changed", "records", "messages_inserted",
                           "source_rows_checked", "tables_checked", "profiles_created", "linked_media"}
                and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    state = row["status"]
    heartbeat = row.get("heartbeat_at")
    if state == "running" and heartbeat and (datetime.now(UTC) - heartbeat).total_seconds() > 90:
        state = "interrupted"
    stage = details.get("stage")
    return {"source": row["source"], "phase": row["phase"], "state": state,
            "stage": stage if stage in {"fingerprints", "projecting", "waiting_io", "writing"} else None,
            "last_success": row["last_success_at"].isoformat() if row.get("last_success_at") else None,
            "progress": counters}


def wake():
    from zoneinfo import ZoneInfo

    from app.ai.schedule import SCHEDULES, enqueue_due
    from app.config import get_settings
    from app.db import engine
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo(get_settings().crm_timezone))
    with engine.begin() as db:
        queued = enqueue_due(db, now)
        # A wake before :20 still catches the latest due hourly scan, once.
        for schedule in SCHEDULES:
            if schedule.hourly and local.minute < schedule.at.minute:
                due = (local - timedelta(hours=1)).replace(minute=schedule.at.minute, second=0, microsecond=0)
                queued.extend(enqueue_due_hourly(db, schedule, due))
    return {"queued": queued}


def enqueue_due_hourly(db, schedule, due):
    from app.config import get_settings
    from app.job_queue import enqueue
    from sqlalchemy import text
    if not get_settings().ai_enabled:
        return []
    period = schedule.period(due)
    claimed = db.execute(text("""INSERT INTO scheduled_runs(job_type,period) VALUES(:kind,:period)
        ON CONFLICT DO NOTHING RETURNING period"""), {"kind": schedule.job_type, "period": period}).scalar_one_or_none()
    if not claimed:
        return []
    job = enqueue(db, schedule.job_type, {"period": period})
    db.execute(text("UPDATE scheduled_runs SET job_id=:job WHERE job_type=:kind AND period=:period"),
               {"job": job, "kind": schedule.job_type, "period": period})
    return [schedule.job_type]


def test_ai():
    from openai import OpenAI
    from pydantic import BaseModel

    class Probe(BaseModel):
        ok: bool

    client = OpenAI(base_url=os.environ["LITELLM_BASE_URL"], api_key=os.environ["LITELLM_API_KEY"], timeout=30, max_retries=0)
    result = client.chat.completions.parse(model=os.environ["AI_BATCH_MODEL"],
        messages=[{"role": "user", "content": "Return ok=true. This is a connection test with no personal data."}], response_format=Probe)
    if not result.choices[0].message.parsed or not result.choices[0].message.parsed.ok:
        raise ValueError("模型未返回所需结构化结果")
    # A generated 1px image tests vision capability without sending user media.
    import base64
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 255, 255)).save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    vision = client.chat.completions.parse(model=os.environ["AI_VISION_MODEL"], messages=[{"role": "user", "content": [
        {"type": "text", "text": "Return ok=true if an image is attached."},
        {"type": "image_url", "image_url": {"url": data_url}}]}], response_format=Probe)
    if not vision.choices[0].message.parsed or not vision.choices[0].message.parsed.ok:
        raise ValueError("图片模型未返回所需结构化结果")
    return {"text": "ready", "vision": "ready"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("initialize", "status", "wake", "test-ai"))
    args = parser.parse_args()
    result = {"initialize": initialize, "status": status, "wake": wake, "test-ai": test_ai}[args.operation]()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
