"""Bounded media queue runner: network outside transactions, reusable by NAS jobs."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError

from sqlalchemy import text

from .db import SessionLocal
from .remote_media import download_image, materialize_image, media_state, needs_download


def process(media_root, maximum):
    count=0
    while count<maximum:
        with SessionLocal() as db:
            job=db.execute(text("""UPDATE jobs SET status='running',locked_at=now(),attempts=attempts+1
                WHERE id=(SELECT id FROM jobs WHERE job_type='media.download' AND status='queued'
                AND available_at<=now() ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING id,payload""")).mappings().one_or_none()
            db.commit()
            if not job:break
            media_state(db,job['payload'],'downloading');db.commit()
            download_required=needs_download(db,job['payload'])
        try:
            downloaded=download_image(job['payload']['url'],media_root) if download_required else None
            with SessionLocal() as db:
                materialize_image(db,job['payload'],media_root,downloaded)
                db.execute(text("UPDATE jobs SET status='complete',completed_at=now(),locked_at=NULL,error=NULL WHERE id=:id"),{'id':job['id']});db.commit()
        except Exception as exc:  # noqa: BLE001 - record the failed job and continue
            with SessionLocal() as db:
                reason=f'HTTP {exc.code}' if isinstance(exc,HTTPError) else type(exc).__name__
                media_state(db,job['payload'],'expired' if isinstance(exc,HTTPError) and exc.code in (401,403,404,410) else 'failed',reason)
                db.execute(text("UPDATE jobs SET status='failed',error=:error,locked_at=NULL WHERE id=:id"),{'id':job['id'],'error':reason});db.commit()
        count+=1
    return count


def main():
    p=argparse.ArgumentParser();p.add_argument('--media-root',type=Path,required=True);p.add_argument('--workers',type=int,default=3);p.add_argument('--limit',type=int,default=1500);args=p.parse_args()
    workers=min(max(args.workers,1),4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        done=sum(pool.map(lambda _:process(args.media_root,args.limit//workers+1),range(workers)))
    print(json.dumps({'media_tasks_processed':done}))


if __name__=='__main__':main()
