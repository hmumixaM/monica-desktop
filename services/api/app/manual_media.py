"""User-owned attachment links; imported source links are never edited here."""
import json
import mimetypes

from fastapi import HTTPException
from sqlalchemy import text

IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/webm'}
# ISO-BMFF brands of still images (HEIC, AVIF); every other `ftyp` file is a video.
STILL_IMAGE_BRANDS = {b'heic', b'heix', b'heim', b'heis', b'hevc', b'mif1', b'msf1', b'avif'}


def validate_media(db, ids, images_only=False):
    if not ids:return
    rows=db.execute(text('SELECT id,media_type FROM media_assets WHERE id=ANY(:ids)'),{'ids':ids}).mappings().all()
    if len(rows)!=len(set(ids)) or (images_only and any(row['media_type'] not in IMAGE_TYPES for row in rows)):
        raise HTTPException(status_code=422,detail='Select existing uploaded photos or files')


def attachments(db, entity_type, ids, role=None, visual_only=False):
    """Linked uploads by entity; `visual_only` leaves out documents for photo streams."""
    if not ids:return {}
    roles=[role] if isinstance(role,str) else role
    rows=db.execute(text('''SELECT l.entity_id,m.id,m.media_type,m.width,m.height,m.byte_length,m.file_name
        FROM media_links l JOIN media_assets m ON m.id=l.media_id
        WHERE l.entity_type=:type AND l.entity_id=ANY(:ids)
          AND (CAST(:roles AS text[]) IS NULL OR l.role=ANY(:roles))
          AND (NOT :visual OR m.media_type=ANY(:visual_types)) ORDER BY l.role,m.created_at,m.id'''),
        {'type':entity_type,'ids':ids,'roles':roles,'visual':visual_only,'visual_types':sorted(IMAGE_TYPES|VIDEO_TYPES)}).mappings()
    result={}
    for row in rows:result.setdefault(str(row['entity_id']),[]).append(dict(row))
    return result


def set_activity_media(db, activity_id, ids, actor):
    validate_media(db,ids)
    db.execute(text("DELETE FROM media_links WHERE entity_type='activity' AND entity_id=:id AND role LIKE 'manual-%'"),{'id':activity_id})
    for index,media in enumerate(dict.fromkeys(ids)):
        db.execute(text("INSERT INTO media_links(entity_type,entity_id,media_id,role) VALUES('activity',:id,:media,:role) ON CONFLICT DO NOTHING"),{'id':activity_id,'media':media,'role':f'manual-{index:03d}'})
    db.execute(text("INSERT INTO audit_events(user_id,action,entity_type,entity_id,metadata) VALUES(:actor,'activity.media.updated','activity',:id,CAST(:data AS jsonb))"),{'actor':actor,'id':activity_id,'data':json.dumps([str(i) for i in ids])})


def _sniff(path):
    """The photo or video type the bytes prove, or None for any other file."""
    from PIL import Image, UnidentifiedImageError
    with path.open('rb') as source:head=source.read(32)
    if head[4:8]==b'ftyp' and head[8:12] not in STILL_IMAGE_BRANDS:
        return ('video/quicktime' if head[8:12]==b'qt  ' else 'video/mp4'),(None,None)
    if head[:4]==b'\x1aE\xdf\xa3':return 'video/webm',(None,None)
    try:
        with Image.open(path) as photo:
            media_type=Image.MIME.get(photo.format)
            if media_type not in IMAGE_TYPES:return None
            dimensions=photo.size;photo.verify()
    except UnidentifiedImageError:
        return None
    return media_type,dimensions


def inspect_upload(path, file_name=''):
    """Photos and videos must really be what they are; any other file is kept as a document."""
    from PIL import Image
    try:
        sniffed=_sniff(path)
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=422,detail='Upload a valid photo or video') from exc
    if sniffed:return sniffed
    claimed=mimetypes.guess_type(file_name or '')[0]
    if claimed in IMAGE_TYPES|VIDEO_TYPES:
        raise HTTPException(status_code=422,detail='The file is named like a photo or video but is not one')
    return claimed or 'application/octet-stream',(None,None)
