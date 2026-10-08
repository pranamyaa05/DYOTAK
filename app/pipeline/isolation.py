"""Isolation analysis and cut-off settlement graph solver (Stage 7).

Computes 3 honest classes:
1. newly_cut_off
2. still_connected
3. no_pre_event_access
"""

from typing import Any, Dict, List
import networkx as nx
from contracts.schemas import SettlementClass, CutoffSettlementRecord


def analyze_isolation(
    road_graph: nx.Graph,
    flood_prob_per_edge: Dict[Any, float],
    destinations: List[Any],
    settlement_nodes: List[Dict[str, Any]],
    severance_threshold: float = 0.5
) -> List[CutoffSettlementRecord]:
    """Run isolation graph reachability analysis."""
    if not destinations or not settlement_nodes:
        return []

    # Pre-event reachability
    pre_reachable = set()
    for dest in destinations:
        if dest in road_graph:
            pre_reachable.update(nx.node_connected_component(road_graph, dest))

    # Post-event: remove flooded edges
    post_graph = road_graph.copy()
    for edge, prob in flood_prob_per_edge.items():
        if prob >= severance_threshold and post_graph.has_edge(*edge):
            post_graph.remove_edge(*edge)

    post_reachable = set()
    for dest in destinations:
        if dest in post_graph:
            post_reachable.update(nx.node_connected_component(post_graph, dest))

    results = []
    for rank, s in enumerate(settlement_nodes, start=1):
        node = s["node"]
        name = s["name"]
        buildings = s.get("buildings_affected", 0)

        if node not in pre_reachable:
            cls = SettlementClass.NO_PRE_EVENT_ACCESS
            extra_dist = None
        elif node not in post_reachable:
            cls = SettlementClass.NEWLY_CUT_OFF
            extra_dist = None
        else:
            cls = SettlementClass.STILL_CONNECTED
            extra_dist = 0.0

        results.append(
            CutoffSettlementRecord(
                name=name,
                settlement_class=cls,
                buildings_affected=buildings,
                extra_distance_km=extra_dist,
                priority_rank=rank
            )
        )

    return results
