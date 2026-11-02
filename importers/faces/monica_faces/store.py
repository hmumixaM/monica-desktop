"""The local SQLite file: every face's embedding, and what has been sent to Monica."""

import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  media_id TEXT PRIMARY KEY,
  avatar INTEGER NOT NULL,
  owner_profile_id TEXT,
  model TEXT NOT NULL,
  faces INTEGER NOT NULL,
  scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
  uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS faces (
  id TEXT PRIMARY KEY,
  media_id TEXT NOT NULL REFERENCES scans(media_id),
  box TEXT NOT NULL,
  quality REAL NOT NULL,
  embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS faces_media ON faces(media_id);
-- The suggestion Monica last received for each face, so unchanged ones are not resent.
CREATE TABLE IF NOT EXISTS sent (
  face_id TEXT PRIMARY KEY,
  cluster_key TEXT,
  profile_id TEXT,
  score REAL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_scan(self, item: dict, model: str, faces: list[tuple[str, str, float, np.ndarray]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM faces WHERE media_id = ?", (item["media_id"],))
            self.db.execute(
                "INSERT OR REPLACE INTO scans(media_id, avatar, owner_profile_id, model, faces) VALUES (?, ?, ?, ?, ?)",
                (item["media_id"], int(item["avatar"]), item["owner_profile_id"], model, len(faces)))
            self.db.executemany(
                "INSERT OR REPLACE INTO faces(id, media_id, box, quality, embedding) VALUES (?, ?, ?, ?, ?)",
                [(face_id, item["media_id"], box, quality, vector.tobytes())
                 for face_id, box, quality, vector in faces])

    def unsent_scans(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT media_id, model FROM scans WHERE uploaded = 0 ORDER BY scanned_at").fetchall()

    def faces_of(self, media_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id, box, quality FROM faces WHERE media_id = ?", (media_id,)).fetchall()

    def mark_uploaded(self, media_ids: list[str]) -> None:
        with self.db:
            self.db.executemany("UPDATE scans SET uploaded = 1 WHERE media_id = ?", [(m,) for m in media_ids])

    def embeddings(self) -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
        """Face ids, the image each is in, an n x 512 matrix, and each face's quality."""
        with closing(self.db.execute("SELECT id, media_id, quality, embedding FROM faces ORDER BY id")) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return [], [], np.zeros((0, 512), np.float32), np.zeros(0, np.float32)
        matrix = np.stack([np.frombuffer(r["embedding"], np.float32) for r in rows])
        return ([r["id"] for r in rows], [r["media_id"] for r in rows], matrix,
                np.array([r["quality"] for r in rows], np.float32))

    def posters(self) -> dict[str, str]:
        """Who posted each scanned post photo, as Monica said when it handed the photo out."""
        return dict(self.db.execute(
            "SELECT media_id, owner_profile_id FROM scans WHERE avatar = 0 AND owner_profile_id IS NOT NULL"
        ).fetchall())

    def sent(self) -> dict[str, tuple]:
        return {r["face_id"]: (r["cluster_key"], r["profile_id"], r["score"])
                for r in self.db.execute("SELECT * FROM sent")}

    def record_sent(self, rows: list[tuple[str, str | None, str | None, float | None]]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO sent(face_id, cluster_key, profile_id, score) VALUES (?, ?, ?, ?)", rows)
