"""Bounded, cached previews for the profile gallery; originals remain immutable."""

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db

router = APIRouter()
_decoders = threading.BoundedSemaphore(2)


@router.get('/api/v1/media/{media_id}/preview')
def preview(media_id: UUID, request: Request, db: Session = Depends(get_db), _user=Depends(current_user),
            face_id: UUID | None = None):
    asset = db.execute(text('SELECT sha256, object_path, media_type, byte_length FROM media_assets WHERE id=:id'),
                       {'id': media_id}).mappings().one_or_none()
    box = None
    if face_id:
        box = db.execute(text('SELECT box FROM faces WHERE id=:face AND media_id=:media'),
                         {'face': face_id, 'media': media_id}).scalar_one_or_none()
    db.close()
    if not asset or not asset['media_type'].startswith('image/'):
        raise HTTPException(404, 'Photo unavailable')
    if face_id and (not box or len(box) != 4 or not all(math.isfinite(v) and -1 <= v <= 2 for v in box)
                    or box[2] <= 0 or box[3] <= 0):
        raise HTTPException(404, 'Face preview unavailable')
    digest = asset['sha256'].strip()
    # Geometry is saved annotation data, not a detection or identity lookup.
    variant = '-face-' + hashlib.sha256(json.dumps(box).encode()).hexdigest()[:16] if box else ''
    etag = f'"preview-v1-{digest}{variant}"'
    headers = {'Cache-Control': 'private, max-age=31536000, immutable', 'ETag': etag,
               'X-Content-Type-Options': 'nosniff'}
    if etag in request.headers.get('if-none-match', ''):
        return Response(status_code=304, headers=headers)
    target = Path(os.environ.get('STAGING_ROOT', '/data/staging')) / 'photo-previews-v1' / digest[:2] / f'{digest}{variant}.webp'
    if not target.is_file():
        if not _decoders.acquire(timeout=1):
            raise HTTPException(503, 'Photo preview busy; retry', headers={'Retry-After': '2'})
        temporary = None
        try:
            if not target.is_file():
                if asset['byte_length'] > 40 * 1024 * 1024:
                    raise HTTPException(413, 'Photo too large for preview')
                with Image.open(asset['object_path']) as image:
                    if image.width * image.height > 40_000_000:
                        raise HTTPException(413, 'Photo too large for preview')
                    if not box:
                        image.draft('RGB', (480, 480))
                    image = ImageOps.exif_transpose(image)
                    if box:
                        left, top, width, height = box
                        side = max(width * image.width, height * image.height) * 1.5
                        cx, cy = (left + width / 2) * image.width, (top + height / 2) * image.height
                        region = (max(0, math.floor(cx - side / 2)), max(0, math.floor(cy - side / 2)),
                                  min(image.width, math.ceil(cx + side / 2)), min(image.height, math.ceil(cy + side / 2)))
                        if region[0] >= region[2] or region[1] >= region[3]:
                            raise HTTPException(404, 'Face preview unavailable')
                        image = image.crop(region)
                        image = ImageOps.pad(image, (192, 192), method=Image.Resampling.LANCZOS)
                    else:
                        image.thumbnail((480, 480))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.webp', delete=False) as stream:
                        temporary = Path(stream.name)
                    image.convert('RGB').save(temporary, 'WEBP', quality=78)
                    temporary.replace(target)
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise HTTPException(404, 'Photo preview unavailable') from None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            _decoders.release()
    return FileResponse(target, media_type='image/webp', headers=headers)
