"""monica-faces scan | match | watch"""

import argparse
import json
import logging
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import httpx

from .client import Monica
from .config import settings
from .detect import Detector
from .match import suggest
from .store import Store

log = logging.getLogger("monica-faces")
UPLOAD = 5000
MATCH_EVERY = 3600
IDLE = 600
# A batch whose first images each time out on their own means the NAS is stuck, not one file: stop and let
# launchd restart.
STALLED = 3


def upload_scans(store: Store, monica: Monica) -> int:
    """Send scans Monica has not acknowledged yet; face ids are fixed, so a resend changes nothing."""
    pending = store.unsent_scans()
    for start in range(0, len(pending), 200):
        page = pending[start:start + 200]
        scans = [{"media_id": scan["media_id"], "model": scan["model"],
                  "status": ("retryable_error" if scan["model"].rsplit(":", 1)[-1]
                             in {"timeout", "missing", "unreadable"} else "success"),
                  "faces": [{"id": f["id"], "box": json.loads(f["box"]), "quality": f["quality"]}
                            for f in store.faces_of(scan["media_id"])]} for scan in page]
        monica.upload({"model": page[0]["model"], "scans": scans})
        store.mark_uploaded([scan["media_id"] for scan in page])
    return len(pending)


def fetch(monica: Monica, items: list[dict]) -> tuple[dict[str, bytes], set[str]]:
    """A batch's images, and the ids that kept timing out; a stalled batch is retried one image at a time."""
    ids = [item["media_id"] for item in items]
    try:
        return monica.images(ids), set()
    except httpx.ReadTimeout:
        if len(ids) == 1:
            return {}, set(ids)
    images, stalled = {}, set()
    for media_id in ids:
        try:
            images.update(monica.images([media_id]))
        except httpx.ReadTimeout:
            stalled.add(media_id)
            if len(stalled) == STALLED and not images:
                raise
    return images, stalled


def scan(store: Store, monica: Monica, detector: Detector, deadline: float, limit: int | None) -> int:
    """Detect while the next batches download, so neither the NAS nor this Mac waits on the other."""
    config = settings()
    config.crops.mkdir(parents=True, exist_ok=True)
    done = 0
    with ThreadPoolExecutor(config.fetchers) as pool:
        while time.monotonic() < deadline and (limit is None or done < limit):
            items = monica.work(min(config.page, limit - done) if limit else config.page)
            if not items:
                break
            batches = [items[i:i + config.batch] for i in range(0, len(items), config.batch)]
            fetches = deque(pool.submit(fetch, monica, batch) for batch in batches[:config.fetchers])
            for n, batch in enumerate(batches):
                images, stalled = fetches.popleft().result()
                if n + config.fetchers < len(batches):
                    fetches.append(pool.submit(fetch, monica, batches[n + config.fetchers]))
                for item in batch:
                    record(store, detector, item, images.get(item["media_id"]), item["media_id"] in stalled)
                done += len(batch)
            upload_scans(store, monica)
            log.info("scanned %d images", done)
    return done


def record(store: Store, detector: Detector, item: dict, data: bytes | None, stalled: bool) -> None:
    config = settings()
    if stalled:
        log.warning("skipping %s: it kept timing out", item["media_id"])
        store.add_scan(item, f"{config.model}:timeout", [])
        return
    found = None if data is None else detector.faces(data)
    model = config.model if found is not None else f"{config.model}:{'unreadable' if data else 'missing'}"
    faces = []
    for face in found or []:
        face_id = str(uuid.uuid4())
        (config.crops / f"{face_id}.jpg").write_bytes(face.crop)
        faces.append((face_id, json.dumps(face.box), face.quality, face.embedding))
    store.add_scan(item, model, faces)


def watch(store: Store, monica: Monica, detector: Detector) -> None:
    """Scan new images as they arrive, matching after new faces or at least hourly, so decisions take effect."""
    matched_at = None
    while True:
        done = scan(store, monica, detector, time.monotonic() + MATCH_EVERY, None)
        if done or matched_at is None or time.monotonic() - matched_at >= MATCH_EVERY:
            match(store, monica)
            matched_at = time.monotonic()
        if not done:
            time.sleep(IDLE)


def match(store: Store, monica: Monica) -> int:
    ids, media, matrix, quality = store.embeddings()
    suggestions = suggest(ids, media, matrix, quality, monica.anchors(), store.posters())
    sent = store.sent()
    unsent = (None, None, None)
    changed = [s for s in suggestions if sent.get(s.face_id, unsent) != (s.cluster_key, s.profile_id, s.score)]
    for start in range(0, len(changed), UPLOAD):
        page = changed[start:start + UPLOAD]
        monica.upload({"model": settings().model, "suggestions": [
            {"face_id": s.face_id, "cluster_key": s.cluster_key, "profile_id": s.profile_id, "score": s.score}
            for s in page]})
        store.record_sent([(s.face_id, s.cluster_key, s.profile_id, s.score) for s in page])
    log.info("%d faces, %d suggested, %d clustered, %d changed", len(suggestions),
             sum(s.profile_id is not None for s in suggestions), sum(s.cluster_key is not None for s in suggestions),
             len(changed))
    return len(changed)


def main() -> None:
    parser = argparse.ArgumentParser(prog="monica-faces")
    parser.add_argument("command", choices=["scan", "match", "watch"])
    parser.add_argument("--hours", type=float, default=5.0, help="stop scanning after this long")
    parser.add_argument("--limit", type=int, help="stop scanning after this many images")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = settings()
    store, monica = Store(config.database), Monica(config)
    detector = Detector(config.model, config.providers, config.det_size)
    try:
        upload_scans(store, monica)
        if args.command == "scan":
            scan(store, monica, detector, time.monotonic() + args.hours * 3600, args.limit)
        elif args.command == "match":
            match(store, monica)
        else:
            watch(store, monica, detector)
    finally:
        store.close()


if __name__ == "__main__":
    main()
