import numpy as np

from monica_faces.match import SUGGEST, suggest


def unit(rng, center, spread, n):
    vectors = center + rng.normal(0, spread, (n, center.size))
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def test_nearest_anchor_suggests_skips_rejections_and_clusters_strangers():
    rng = np.random.default_rng(0)
    alice, bob, stranger, other, noise = (rng.normal(size=512) for _ in range(5))
    groups = [unit(rng, alice, 0.03, 6), unit(rng, bob, 0.03, 3), unit(rng, stranger, 0.03, 12),
              unit(rng, noise, 2.0, 1), unit(rng, other, 0.03, 8)]
    matrix = np.vstack(groups).astype(np.float32)
    ids = [f"f{i:02d}" for i in range(len(matrix))]
    quality = np.full(len(ids), 0.9, np.float32)
    anchors = {"anchors": [{"face_id": "f00", "profile_id": "alice", "kind": "avatar"},
                           {"face_id": "f06", "profile_id": "bob", "kind": "confirmed"}],
               "rejected": [{"face_id": "f07", "profile_id": "bob"}], "ignored": ["f05"]}

    by_face = {s.face_id: s for s in suggest(ids, [f"m{i}" for i in range(len(ids))], matrix, quality, anchors, {})}

    assert {"f00", "f06", "f05"}.isdisjoint(by_face)
    assert [by_face[f"f{i:02d}"].profile_id for i in range(1, 5)] == ["alice"] * 4
    assert all(by_face[f"f{i:02d}"].score >= SUGGEST for i in range(1, 5))
    assert by_face["f07"].profile_id is None and by_face["f08"].profile_id == "bob"
    strangers = {by_face[f"f{i:02d}"].cluster_key for i in range(9, 21)}
    others = {by_face[f"f{i:02d}"].cluster_key for i in range(22, 30)}
    assert len(strangers) == len(others) == 1 and strangers != others and None not in strangers | others
    assert by_face["f21"].profile_id is None


def test_the_face_that_dominates_someones_posts_is_theirs():
    rng = np.random.default_rng(1)
    carol, dave, erin, frank, kid = (rng.normal(size=512) for _ in range(5))
    faces, media, posts = [], [], {}

    def photo(name, poster, *people):
        posts[name] = poster
        for person in people:
            faces.append(unit(rng, person, 0.03, 1)[0])
            media.append(name)

    for n in range(6):
        photo(f"carol{n}", "carol", carol, *([dave] if n < 2 else []))
    photo("carol-view", "carol", rng.normal(size=512))
    for n in range(4):
        photo(f"couple{n}", "erin", erin, frank)
    for n in range(5):
        photo(f"gina{n}", "gina", kid)
    photo("party", "someone", carol, rng.normal(size=512))
    faces.append(unit(rng, kid, 0.03, 1)[0])
    media.append("kid-avatar")

    matrix = np.vstack(faces).astype(np.float32)
    ids = [f"f{i:02d}" for i in range(len(matrix))]
    quality = np.full(len(ids), 0.9, np.float32)
    rejected_carol = ids[media.index("carol5")]
    anchors = {"anchors": [{"face_id": ids[-1], "profile_id": "kid", "kind": "avatar"}],
               "rejected": [{"face_id": rejected_carol, "profile_id": "carol"}], "ignored": []}

    by_face = {s.face_id: s for s in suggest(ids, media, matrix, quality, anchors, posts)}
    people = {}
    for i, name in enumerate(media[:-1]):
        people.setdefault(name, []).append(by_face[ids[i]].profile_id)

    assert [people[f"carol{n}"][0] for n in range(5)] == ["carol"] * 5
    assert by_face[rejected_carol].profile_id is None
    assert people["carol0"][1] is None and people["carol1"][1] is None
    assert people["party"][0] == "carol"
    assert all(p is None for n in range(4) for p in people[f"couple{n}"])
    assert [people[f"gina{n}"][0] for n in range(5)] == ["kid"] * 5
