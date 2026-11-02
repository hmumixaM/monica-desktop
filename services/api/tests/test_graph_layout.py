import math

from app.graph_layout import cluster_radius, lay_out


def node(name, deg):
    return {"name": name, "deg": deg, "x": 0, "y": 0, "community": 0}


def test_communities_follow_edges_and_offsets_stay_inside_their_cluster():
    nodes = [node(f"a{n}", 3) for n in range(4)] + [node(f"b{n}", 3) for n in range(4)] + [node("loner", 0)]
    clique = lambda start: [{"a": start + i, "b": start + j, "combined": 5} for i in range(4) for j in range(i + 1, 4)]
    edges = clique(0) + clique(4) + [{"a": 0, "b": 4, "combined": 0}]

    lay_out(nodes, edges)

    assert len({n["community"] for n in nodes[:4]}) == 1 and len({n["community"] for n in nodes[4:8]}) == 1
    assert nodes[0]["community"] != nodes[4]["community"] != nodes[8]["community"]
    for group in (nodes[:4], nodes[4:8]):
        assert all(math.hypot(n["x"], n["y"]) <= cluster_radius(4) for n in group)


def test_layout_is_deterministic():
    build = lambda: [node(str(n), n % 3) for n in range(30)]
    edges = [{"a": n, "b": (n * 7) % 30, "combined": n % 4} for n in range(30) if n != (n * 7) % 30]
    first, second = build(), build()
    lay_out(first, edges)
    lay_out(second, edges)
    assert first == second
