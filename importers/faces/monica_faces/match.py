"""Who each face looks like, and which unknown faces look like each other.

Reference faces are single-face avatars, confirmed faces, and each poster's own face: the one that
recurs across most of the photos they posted. A face is suggested as the person whose closest
reference face is similar enough, skipping anyone it was already said not to be. Faces close to no
one are clustered, so a recurring stranger can be named once.
"""

from dataclasses import dataclass

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.decomposition import PCA

# Cosine similarity of ArcFace (w600k_r50) embeddings above which two faces are likely the same person.
SUGGEST = 0.40
NEAREST = 12
CLUSTER_QUALITY = 0.6
CLUSTER_LIMIT = 30000
CHUNK = 1024
# A poster's own face is in at least this many of their photos with a clear face,
OWNER_PHOTOS = 4
# in at least this share of them,
OWNER_SHARE = 0.25
# and in this many times as many photos as the next most frequent face, so a couple's shared account names no one.
OWNER_MARGIN = 1.5
OWNER_LIMIT = 3000


@dataclass(frozen=True)
class Suggestion:
    face_id: str
    cluster_key: str | None
    profile_id: str | None
    score: float | None


def suggest(ids: list[str], media: list[str], matrix: np.ndarray, quality: np.ndarray,
            anchors: dict, posts: dict[str, str]) -> list[Suggestion]:
    row = {face_id: i for i, face_id in enumerate(ids)}
    known = [(row[a["face_id"]], a["profile_id"]) for a in anchors["anchors"] if a["face_id"] in row]
    refused = {row[r["face_id"]]: r["profile_id"] for r in anchors["rejected"] if r["face_id"] in row}
    skip = {i for i, _ in known} | {row[f] for f in anchors["ignored"] if f in row}
    candidates = [i for i in range(len(ids)) if i not in skip]

    owned = posters(candidates, media, matrix, quality, posts, known, refused)
    matched = nearest([i for i in candidates if i not in owned], matrix,
                      known + [(i, person) for i, (person, _) in owned.items()], refused)
    matched.update(owned)
    clusters = cluster([i for i in candidates if i not in matched], ids, matrix, quality)
    return [Suggestion(ids[i], clusters.get(i), *matched.get(i, (None, None))) for i in candidates]


def nearest(rows: list[int], matrix: np.ndarray, references: list[tuple[int, str]],
            refused: dict[int, str]) -> dict[int, tuple[str, float]]:
    """Each row's person: the owner of its closest reference face, when that face is similar enough."""
    matched: dict[int, tuple[str, float]] = {}
    if not references or not rows:
        return matched
    people = [person for _, person in references]
    reference = matrix[np.array([i for i, _ in references])]
    k = min(NEAREST, len(people))
    candidates = np.array(rows, dtype=np.int64)
    for start in range(0, len(candidates), CHUNK):
        chunk = candidates[start:start + CHUNK]
        similarity = matrix[chunk] @ reference.T
        closest = np.argpartition(-similarity, k - 1, axis=1)[:, :k]
        for offset, face in enumerate(chunk):
            for anchor in closest[offset][np.argsort(-similarity[offset, closest[offset]])]:
                score = float(similarity[offset, anchor])
                if score < SUGGEST:
                    break
                if people[anchor] != refused.get(int(face)):
                    matched[int(face)] = (people[anchor], round(score, 4))
                    break
    return matched


def posters(rows: list[int], media: list[str], matrix: np.ndarray, quality: np.ndarray, posts: dict[str, str],
            known: list[tuple[int, str]], refused: dict[int, str]) -> dict[int, tuple[str, float]]:
    """Faces of the person who posted them: the face that dominates their photos, unless it is someone else known."""
    by_poster: dict[str, list[int]] = {}
    for i in rows:
        if poster := posts.get(media[i]):
            by_poster.setdefault(poster, []).append(i)
    known_people = [person for _, person in known]
    known_faces = matrix[np.array([i for i, _ in known], dtype=np.int64)]
    owned: dict[int, tuple[str, float]] = {}
    for poster, faces in by_poster.items():
        center = own_face(faces, media, matrix, quality)
        if center is None:
            continue
        if known_people:
            likeness = known_faces @ center
            closest = int(np.argmax(likeness))
            if likeness[closest] >= SUGGEST and known_people[closest] != poster:
                continue
        for face, score in zip(faces, matrix[faces] @ center, strict=True):
            if score >= SUGGEST and refused.get(face) != poster:
                owned[face] = (poster, round(float(score), 4))
    return owned


def own_face(faces: list[int], media: list[str], matrix: np.ndarray, quality: np.ndarray) -> np.ndarray | None:
    """The unit mean of the face that recurs across most of one poster's photos, if it clearly dominates."""
    clear = sorted((i for i in faces if quality[i] >= CLUSTER_QUALITY), key=lambda i: -quality[i])[:OWNER_LIMIT]
    if len({media[i] for i in clear}) < OWNER_PHOTOS:
        return None
    photo = np.unique([media[i] for i in clear], return_inverse=True)[1]
    photos = int(photo.max()) + 1
    vectors = matrix[clear]
    alike = (vectors @ vectors.T >= SUGGEST).astype(np.float32)
    in_photo = np.zeros((len(clear), photos), np.float32)
    in_photo[np.arange(len(clear)), photo] = 1
    seen_in = (alike @ in_photo > 0).sum(axis=1)
    group = alike[int(np.argmax(seen_in))] > 0
    center = vectors[group].mean(axis=0)
    center /= np.linalg.norm(center)
    own = vectors @ center >= SUGGEST
    own_photos = len(np.unique(photo[own]))
    rest = ~own
    runner_up = int((alike[rest][:, rest] @ in_photo[rest] > 0).sum(axis=1).max()) if rest.any() else 0
    if own_photos < max(OWNER_PHOTOS, OWNER_SHARE * photos) or own_photos < OWNER_MARGIN * runner_up:
        return None
    return center


def cluster(rows: list[int], ids: list[str], matrix: np.ndarray, quality: np.ndarray) -> dict[int, str]:
    """Each clustered row's key: the smallest face id in its cluster, which stays put as the cluster grows."""
    clear = sorted((i for i in rows if quality[i] >= CLUSTER_QUALITY), key=lambda i: -quality[i])[:CLUSTER_LIMIT]
    if len(clear) < 10:
        return {}
    vectors = matrix[clear]
    reduced = PCA(n_components=min(64, len(clear) - 1), random_state=0).fit_transform(vectors)
    reduced /= np.linalg.norm(reduced, axis=1, keepdims=True)
    labels = HDBSCAN(min_cluster_size=3, min_samples=2, cluster_selection_method="leaf", copy=True).fit_predict(reduced)
    members: dict[int, list[int]] = {}
    for i, label in zip(clear, labels, strict=True):
        if label >= 0:
            members.setdefault(int(label), []).append(i)
    return {i: min(ids[j] for j in group) for group in members.values() for i in group}
