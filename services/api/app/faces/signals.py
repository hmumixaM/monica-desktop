"""What faces say about people elsewhere in Monica."""

from sqlalchemy import text


def avatar_matches(db, media_ids: set[str]) -> dict[tuple[str, str], float]:
    """(avatar media, person) -> how alike the avatar's face is to that person; 1.0 once someone confirmed it."""
    if not media_ids:
        return {}
    rows = db.execute(text("""
      SELECT media_id::text AS media_id,
             (CASE WHEN status = 'confirmed' THEN profile_id ELSE suggested_profile_id END)::text AS profile_id,
             max(CASE WHEN status = 'confirmed' THEN 1.0 ELSE suggestion_score END) AS score
      FROM faces
      WHERE media_id = ANY(CAST(:media AS uuid[])) AND status <> 'ignored'
        AND (status = 'confirmed' OR suggested_profile_id IS NOT NULL)
      GROUP BY 1, 2"""), {"media": list(media_ids)}).mappings()
    return {(row["media_id"], row["profile_id"]): round(float(row["score"]), 2) for row in rows}


def co_appearances(db, min_photos: int = 1) -> list[dict]:
    """Pairs of people confirmed together in the same photo, with how many photos."""
    rows = db.execute(text("""
      SELECT a.profile_id::text AS source, b.profile_id::text AS target, count(DISTINCT a.media_id) AS photos
      FROM faces a JOIN faces b ON b.media_id = a.media_id AND b.profile_id > a.profile_id
      WHERE a.status = 'confirmed' AND b.status = 'confirmed'
      GROUP BY 1, 2 HAVING count(DISTINCT a.media_id) >= :min
      ORDER BY photos DESC"""), {"min": min_photos}).mappings()
    return [dict(row) for row in rows]
