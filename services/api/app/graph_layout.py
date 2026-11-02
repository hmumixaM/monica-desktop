"""Communities and starting positions for the interaction graph.

The canvas script packs communities itself and spreads each one by its members' offsets from the community
centroid, so offsets are in the script's units: a community of n people spans about 0.13 + 0.035·√n.
"""

import math

import networkx as nx

SEED = 7
GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))


def cluster_radius(size: int) -> float:
    return 0.13 + 0.035 * math.sqrt(size)


def lay_out(nodes: list[dict], edges: list[dict]) -> None:
    """Sets `community`, `x` and `y` on each node: Louvain communities, each a sunflower with its busiest people inside."""
    graph = nx.Graph()
    graph.add_nodes_from(range(len(nodes)))
    graph.add_weighted_edges_from((edge["a"], edge["b"], max(edge["combined"], 0.5)) for edge in edges)
    found = nx.community.louvain_communities(graph, weight="weight", seed=SEED)
    for community, members in enumerate(sorted(found, key=lambda members: (-len(members), min(members)))):
        ordered = sorted(members, key=lambda index: (-nodes[index]["deg"], nodes[index]["name"] or "", index))
        radius = cluster_radius(len(ordered))
        for rank, index in enumerate(ordered):
            distance = radius * math.sqrt((rank + 0.5) / len(ordered))
            nodes[index].update(community=community, x=distance * math.cos(rank * GOLDEN_ANGLE), y=distance * math.sin(rank * GOLDEN_ANGLE))
