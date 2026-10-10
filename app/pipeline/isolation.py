"""Isolation analysis and cut-off settlement solver (Stage 7, ARCHITECTURE.md 4.7).

Builds an **undirected NetworkX graph** of the pre-event road network, removes the
segments the flood makes impassable, and classifies every settlement into one of
three honest classes:

1. ``newly_cut_off``       -- reachable pre-event, unreachable post-event
2. ``still_connected``     -- reachable both times, with ``extra_distance_km``
3. ``no_pre_event_access`` -- already unreachable pre-event (not flood-attributed)

Graph construction
------------------
Input is the pre-event ``highway`` GeoParquet extract (measured live: 3 565
LineStrings / 1 802 km, values ``path``/``track``/``unclassified``/``residential``/
``secondary``/``tertiary``/``primary``/``service``/``footway``/``living_street``).
Everything is computed in the UTM zone of the AOI centroid
(``geo.get_utm_epsg_for_bbox``, never hardcoded).

* Every vertex becomes a graph node, keyed on its coordinate rounded to 1 cm, so
  coincident endpoints of different ways share one node. The keying is vectorised
  (``numpy.unique`` over rounded coordinate pairs), which matters at 100 k+
  vertices.
* Each polyline segment becomes an edge. Sampling is **per segment**, not per way:
  a way crossing a flooded river must be severed at the crossing while the rest of
  the way stays passable -- that is exactly the bridge-over-a-flooded-river case.
  Every segment is sampled at least once (one middle sample for short segments,
  one sample per ``damage.road_sample_spacing_m`` for longer ones).
* **Degree-2 nodes are then contracted**, so the final nodes really are junctions
  and endpoints (ARCHITECTURE 4.7). Merged edges add their lengths and flooded
  lengths, take the maximum probability, and get the length-weighted mean
  probability, so a severed stretch inside a chain is not diluted away.
* Components that are pure cycles (every node of degree 2) have no junction to
  contract them onto; they are left out of the graph and counted, because a cycle
  with no junction cannot connect anything, so reachability is unaffected.

Severance
---------
An edge is dropped when its length-weighted mean flood probability reaches
``isolation.flood_threshold_edge`` -- a **mean**, not a maximum: a long way that
clips one flooded pixel stays usable. Multi-source Dijkstra (``length_m`` weight)
runs from all destinations on the pre-event graph and again on the post-event
graph; the difference is a still-connected settlement's ``extra_distance_km``.

Destinations and settlements
----------------------------
Destinations are the ``amenity`` features tagged ``isolation.facility_tags``
(hospital/clinic) plus place nodes tagged ``isolation.destination_place_tags``
("hospitals/clinics and towns", ARCHITECTURE 4.7). Settlements are the place nodes
tagged ``isolation.place_tags``. Both are snapped to their nearest road node
within ``isolation.settlement_snap_distance_m``.

If no destination exists -- or none of them reaches a road node -- the stage
returns the typed :data:`IsolationStatus.NO_DESTINATIONS` state with a limitation
note and classifies nothing, instead of guessing (ARCHITECTURE 4.7). A settlement
that cannot be snapped to any road node is reported ``no_pre_event_access`` with
``snapped=false`` and ``snap_distance_m=null``: it has no road access at all, and
the flood cannot be credited for it.

Priority
--------
``score = priority_buildings_weight * buildings_affected
+ priority_isolation_weight * severity_weights[town class]``, with both weights and
every class severity in config; ``priority_rank`` is the descending score order
with a deterministic (class, name) tie-break. ``buildings_affected`` counts the
``affected`` buildings of Stage 6 within ``isolation.settlement_radius_m`` of the
settlement point.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.pipeline import damage as damage_module
from app.settings import app_config

logger = logging.getLogger("dyotak.isolation")

#: Coordinate rounding used to merge coincident vertices (metres). 1 cm is far
#: below the precision of OSM geometry or a 20 m raster, and it turns "same point"
#: into "same node".
NODE_KEY_PRECISION_M = 0.01

#: Chunk size (query points) for the nearest-node search.
SNAP_CHUNK = 512

#: Report and tie-break order of the classes.
SETTLEMENT_CLASSES: Tuple[str, ...] = ("newly_cut_off", "still_connected", "no_pre_event_access")

#: The ohsome feature types this stage reads (roads, destinations, settlements).
ISOLATION_LAYERS: Tuple[str, ...] = ("highway", "amenity", "place")


class IsolationStatus(str, Enum):
    """Typed outcome of the isolation stage (ARCHITECTURE 4.7)."""

    OK = "ok"
    NO_DESTINATIONS = "no_destinations"
    NO_ROAD_GRAPH = "no_road_graph"


@dataclass
class RoadGraph:
    """Undirected road graph plus the node coordinates of its (relabelled) nodes."""

    graph: Any                              # networkx.Graph
    node_points_utm: np.ndarray             # (N, 2) float64, UTM metres, node order
    source_segments: int = 0                # polyline segments seen before contraction
    cycle_segments: int = 0                 # segments kept uncontracted as junction-free cycles
    total_length_km: float = 0.0

    def __len__(self) -> int:
        return int(self.node_points_utm.shape[0])


@dataclass
class IsolationResult:
    """Classified settlements plus the numeric facts for facts.json."""

    settlements: Dict[str, Any]                       # GeoJSON FeatureCollection
    records: List[Any]                                # contracts.schemas.CutoffSettlementRecord
    counts: Dict[str, Any]
    facts: List[Any]                                  # contracts.schemas.FactItem
    status: IsolationStatus
    timings: Dict[str, float]
    utm_epsg: int
    graph_summary: Dict[str, Any] = field(default_factory=dict)
    severance_threshold: float = 0.0
    limitations: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    crs: str = "EPSG:4326"

    @property
    def no_destinations(self) -> bool:
        return self.status is IsolationStatus.NO_DESTINATIONS


# ---------------------------------------------------------------------------
# Vertices, segments and sampling
# ---------------------------------------------------------------------------
def vertex_keys(coordinates_utm: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Map every vertex to a node id, merging coincident coordinates.

    Returns ``(node_id_per_vertex, unique_node_points_utm)``, keyed on coordinates
    rounded to :data:`NODE_KEY_PRECISION_M` in one vectorised pass.
    """
    if coordinates_utm.shape[0] == 0:
        return np.zeros(0, dtype="int64"), np.zeros((0, 2), dtype="float64")
    keys = np.round(coordinates_utm / NODE_KEY_PRECISION_M).astype("int64")
    unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    return np.asarray(inverse, dtype="int64").ravel(), unique.astype("float64") * NODE_KEY_PRECISION_M


def polyline_segments(
    flat_lines: Sequence[Any],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, List[int]]:
    """Flatten polylines into vertex coordinates plus a segment table.

    Returns ``(coordinates, counts, segment_start, segment_delta, counts_list)``
    where ``coordinates`` holds every vertex of every line in order, ``counts`` the
    vertex count per line, and ``segment_start``/``segment_delta`` one row per
    polyline segment (consecutive vertex pair), all in the same order.
    """
    import shapely

    lines = np.asarray(flat_lines, dtype=object)
    counts = [int(c) for c in shapely.get_num_coordinates(lines)]
    coordinates = shapely.get_coordinates(lines)
    starts: List[np.ndarray] = []
    deltas: List[np.ndarray] = []
    offset = 0
    for count in counts:
        if count >= 2:
            block = coordinates[offset: offset + count]
            starts.append(block[:-1])
            deltas.append(block[1:] - block[:-1])
        offset += count
    if starts:
        return coordinates, counts, np.concatenate(starts), np.concatenate(deltas), counts
    return coordinates, counts, np.zeros((0, 2)), np.zeros((0, 2)), counts


def sample_segments(
    segment_start: np.ndarray, segment_delta: np.ndarray, spacing_m: float
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample every segment at least once, returning UTM points and weights.

    Returns ``(xs, ys, segment_owner, weights)``: ``segment_owner`` indexes the
    segment table (identical order to the edge tables built alongside it) and
    ``weights`` is the length each sample represents, so an unweighted mean over a
    segment's samples is its length mean.
    """
    if segment_start.shape[0] == 0:
        empty = np.zeros(0, dtype="float64")
        return empty, empty, np.zeros(0, dtype="int64"), empty
    spacing = float(spacing_m)
    lengths = np.hypot(segment_delta[:, 0], segment_delta[:, 1])
    samples = np.maximum(1, np.ceil(lengths / spacing).astype("int64"))
    total = int(samples.sum())
    segment_index = np.repeat(np.arange(lengths.shape[0], dtype="int64"), samples)
    within = np.arange(total, dtype="int64") - np.repeat(
        np.concatenate([[0], np.cumsum(samples)[:-1]]), samples
    )
    per_sample = samples[segment_index].astype("float64")
    fraction = (within + 0.5) / per_sample
    start = segment_start[segment_index]
    delta = segment_delta[segment_index]
    xs = start[:, 0] + fraction * delta[:, 0]
    ys = start[:, 1] + fraction * delta[:, 1]
    weights = lengths[segment_index] / per_sample
    return xs, ys, segment_index, weights


def nearest_nodes(
    points_utm: np.ndarray, node_points_utm: np.ndarray, max_distance_m: float
) -> Tuple[np.ndarray, np.ndarray]:
    """Nearest graph node for each point, chunked (no scipy dependency).

    Returns ``(node_index, distance_m)``; a point farther away than
    ``max_distance_m`` gets ``node_index = -1`` and ``distance_m = inf`` (not
    snapped -- reporting the raw distance would read as a snap distance).
    """
    points = np.asarray(points_utm, dtype="float64").reshape(-1, 2)
    nodes = np.asarray(node_points_utm, dtype="float64").reshape(-1, 2)
    index = np.full(points.shape[0], -1, dtype="int64")
    distance = np.full(points.shape[0], np.inf, dtype="float64")
    if points.shape[0] == 0 or nodes.shape[0] == 0:
        return index, distance
    for start in range(0, points.shape[0], SNAP_CHUNK):
        stop = min(start + SNAP_CHUNK, points.shape[0])
        chunk = points[start:stop]
        squared = (
            (chunk[:, 0, None] - nodes[None, :, 0]) ** 2
            + (chunk[:, 1, None] - nodes[None, :, 1]) ** 2
        )
        best = np.argmin(squared, axis=1)
        best_distance = np.sqrt(squared[np.arange(chunk.shape[0]), best])
        index[start:stop] = best
        distance[start:stop] = best_distance
    unsnapped = distance > float(max_distance_m)
    index = np.where(unsnapped, -1, index)
    distance = np.where(unsnapped, np.inf, distance)
    return index, distance


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def line_geometries(geometries_utm: Sequence[Any]) -> np.ndarray:
    """Flatten LineString / MultiLineString geometries into a LineString array."""
    import shapely

    flat: List[Any] = []
    for geometry in np.asarray(geometries_utm, dtype=object):
        type_id = int(shapely.get_type_id(geometry))
        if type_id == 1:                                  # LineString
            flat.append(geometry)
        elif type_id == 5:                                # MultiLineString
            flat.extend(list(shapely.get_parts(geometry)))
    return np.asarray(flat, dtype=object)


def build_road_graph(
    geometries_utm: Sequence[Any],
    spacing_m: float,
    sample_probability: Optional[Callable[[np.ndarray, np.ndarray], np.ndarray]] = None,
    flooded_threshold: float = 0.5,
) -> RoadGraph:
    """Undirected road graph whose nodes are junctions and endpoints.

    ``sample_probability(x_utm, y_utm) -> probability`` is called once for all
    segment samples (NaN outside the raster); without it every edge keeps
    ``flood_probability = 0``, which is what the pure-graph tests use.
    """
    import networkx as nx

    lines = line_geometries(geometries_utm)
    if lines.shape[0] == 0:
        return RoadGraph(graph=nx.Graph(), node_points_utm=np.zeros((0, 2), dtype="float64"))

    coordinates, line_counts, segment_start, segment_delta, _ = polyline_segments(lines)
    node_ids, node_points = vertex_keys(coordinates)
    if segment_start.shape[0] == 0:
        return RoadGraph(graph=nx.Graph(), node_points_utm=node_points)

    # Segment endpoints as node ids (line boundaries are the vertex offsets).
    ends: List[np.ndarray] = []
    offset = 0
    for count in line_counts:
        if count >= 2:
            block = node_ids[offset: offset + count]
            ends.append(block[:-1])
            ends.append(block[1:])
        offset += count
    segment_u = np.concatenate(ends[0::2])
    segment_v = np.concatenate(ends[1::2])
    del ends

    xs, ys, owner, weights = sample_segments(segment_start, segment_delta, spacing_m)
    segments = segment_start.shape[0]
    probability = np.zeros(segments, dtype="float64")
    flooded_weight = np.zeros(segments, dtype="float64")
    maximum = np.zeros(segments, dtype="float64")
    if sample_probability is not None and xs.shape[0]:
        values = np.asarray(sample_probability(xs, ys), dtype="float64")
        usable = np.isfinite(values)
        values = np.where(usable, values, 0.0)
        # Only pixels inside the flood raster carry evidence; samples outside it
        # are dropped from the mean instead of being read as "dry".
        effective = weights * usable
        weighted = np.bincount(owner, weights=values * effective, minlength=segments)
        totals = np.bincount(owner, weights=effective, minlength=segments)
        present = totals > 0
        probability[present] = weighted[present] / np.maximum(totals[present], 1e-12)
        np.maximum.at(maximum, owner, values)
        flooded_weight = np.bincount(
            owner, weights=effective * (values >= float(flooded_threshold)), minlength=segments
        )

    segment_length = np.hypot(segment_delta[:, 0], segment_delta[:, 1])
    graph = nx.Graph()
    graph.add_nodes_from(range(int(node_points.shape[0])))
    usable_segment = segment_u != segment_v                    # self-loops add no connectivity
    edges = np.column_stack(
        [
            np.minimum(segment_u[usable_segment], segment_v[usable_segment]),
            np.maximum(segment_u[usable_segment], segment_v[usable_segment]),
        ]
    )
    if edges.shape[0]:
        unique_edges, inverse = np.unique(edges, axis=0, return_inverse=True)
        inverse = np.asarray(inverse, dtype="int64").ravel()
        count = unique_edges.shape[0]
        kept_length = segment_length[usable_segment]
        summed = np.bincount(inverse, weights=kept_length, minlength=count)
        flooded = np.bincount(inverse, weights=flooded_weight[usable_segment], minlength=count)
        weighted = np.bincount(
            inverse, weights=(probability * segment_length)[usable_segment], minlength=count
        )
        maxima = np.zeros(count, dtype="float64")
        np.maximum.at(maxima, inverse, maximum[usable_segment])
        merged_probability = np.where(summed > 0, weighted / np.maximum(summed, 1e-12), 0.0)
        for index in range(count):
            graph.add_edge(
                int(unique_edges[index, 0]),
                int(unique_edges[index, 1]),
                length_m=float(summed[index]),
                flood_probability=float(merged_probability[index]),
                max_probability=float(maxima[index]),
                flooded_length_m=float(flooded[index]),
            )

    contracted, original_ids, cycle_segments = contract_degree_two(graph)
    total_length_km = sum(
        float(data.get("length_m", 0.0)) for _, _, data in contracted.edges(data=True)
    ) / 1000.0
    return RoadGraph(
        graph=contracted,
        node_points_utm=node_points[np.asarray(original_ids, dtype="int64")],
        source_segments=int(segment_start.shape[0]),
        cycle_segments=cycle_segments,
        total_length_km=total_length_km,
    )


def contract_degree_two(graph, max_passes: int = 6) -> Tuple[Any, List[int], int]:
    """Contract chains of degree-2 nodes so the nodes are junctions and endpoints.

    Returns ``(graph, original_node_ids, cycle_segments)`` where
    ``original_node_ids[i]`` is the index (into the node point array) of contracted
    node ``i``.

    Two chains between the same pair of junctions are **merged** (lengths and
    flooded lengths added, maximum and length-weighted mean probability), never
    overwritten: on the live AOI 162 nodes carried such parallel chains, and
    dropping one of them would silently delete road length -- and could keep a
    severely flooded chain usable. Merging can leave a node with only two
    distinct neighbours, so the passes repeat until nothing changes.

    A chain that returns to the junction it started from (a loop) and a component
    whose every node has degree 2 (a ring road) have no junction to contract them
    onto. Those are **kept uncontracted and counted** in ``cycle_segments`` rather
    than dropped: dropping them would delete real roads and would make
    settlements on the loop unsnappable, i.e. a false ``no_pre_event_access``.
    """
    import networkx as nx

    if graph.number_of_nodes() == 0:
        return nx.Graph(), [], 0

    anchors = {node for node, degree in graph.degree() if degree != 2 or graph.has_edge(node, node)}
    for component in nx.connected_components(graph):
        if not (component & anchors):
            anchors.add(min(component))      # anchor-less (ring) component: keep it whole

    current = graph
    keep = set(anchors)
    cycle_segments = 0
    for _ in range(int(max_passes)):
        contracted, closed_nodes, closed_edges = _contract_pass(current, keep)
        cycle_segments += closed_edges
        keep = set(contracted.nodes) | closed_nodes
        unchanged = (
            contracted.number_of_nodes() == current.number_of_nodes()
            and contracted.number_of_edges() == current.number_of_edges()
        )
        current = contracted
        if unchanged:
            break

    surviving = sorted(int(node) for node in current.nodes)
    final = nx.relabel_nodes(current, {node: index for index, node in enumerate(surviving)})
    return final, surviving, cycle_segments


def _contract_pass(graph, anchors) -> Tuple[Any, set, int]:
    """One contraction pass over ``graph`` rooted at ``anchors``.

    Returns ``(graph, closed_chain_nodes, closed_chain_edges)``: chains between two
    junctions become single merged edges, while a chain that returns to its own
    junction is copied verbatim (its nodes are returned so the next pass treats
    them as junctions and keeps them).
    """
    import networkx as nx

    active = {node for node in anchors if node in graph}
    contracted = nx.Graph()
    contracted.add_nodes_from(active)
    visited: set = set()
    closed_nodes: set = set()
    closed_edges = 0

    for anchor in sorted(active):
        for neighbour in list(graph.neighbors(anchor)):
            if (anchor, neighbour) in visited:
                continue
            length_m = 0.0
            flooded_m = 0.0
            max_probability = 0.0
            probability_length = 0.0
            chain_edges: List[Tuple[int, int, Dict[str, Any]]] = []
            previous, current = anchor, neighbour
            while True:
                data = graph.get_edge_data(previous, current) or {}
                segment_length = float(data.get("length_m", 0.0))
                length_m += segment_length
                flooded_m += float(data.get("flooded_length_m", 0.0))
                max_probability = max(max_probability, float(data.get("max_probability", 0.0)))
                probability_length += float(data.get("flood_probability", 0.0)) * segment_length
                visited.add((previous, current))
                visited.add((current, previous))
                chain_edges.append((previous, current, dict(data)))
                if current in anchors:
                    break
                onward = [node for node in graph.neighbors(current) if node != previous]
                if not onward:
                    break
                previous, current = current, onward[0]

            if current == anchor:
                # A loop back to its own junction: keep the walked road as it is.
                closed_nodes.update({node for edge in chain_edges for node in edge[:2]})
                for start, end, data in chain_edges:
                    contracted.add_edge(start, end, **data)
                    closed_edges += 1
                continue

            existing = contracted.get_edge_data(anchor, current)
            if existing is None:
                contracted.add_edge(
                    anchor,
                    current,
                    length_m=length_m,
                    flooded_length_m=flooded_m,
                    max_probability=max_probability,
                    flood_probability=(probability_length / length_m) if length_m > 0 else 0.0,
                )
            else:
                total = float(existing["length_m"]) + length_m
                merged = contracted[anchor][current]
                merged["length_m"] = total
                merged["flooded_length_m"] = float(existing["flooded_length_m"]) + flooded_m
                merged["max_probability"] = max(float(existing["max_probability"]), max_probability)
                merged["flood_probability"] = (
                    (float(existing["flood_probability"]) * float(existing["length_m"])
                     + probability_length) / total
                    if total > 0
                    else 0.0
                )
    return contracted, closed_nodes, closed_edges


# ---------------------------------------------------------------------------
# Destinations and settlements
# ---------------------------------------------------------------------------
def _point_of(geometry) -> Optional[Tuple[float, float]]:
    """Representative (lon, lat) of an OSM feature, or None when it has none."""
    import shapely

    if geometry is None or not shapely.is_valid_input(geometry) or shapely.is_empty(geometry):
        return None
    if int(shapely.get_type_id(geometry)) == 0:               # Point
        return float(shapely.get_x(geometry)), float(shapely.get_y(geometry))
    point = shapely.point_on_surface(geometry)
    return float(shapely.get_x(point)), float(shapely.get_y(point))


def destination_features(
    layers: Dict[str, damage_module.OSMLayer], config: Any = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Destinations in the AOI, plus notes about layers that were missing."""
    cfg = config or app_config
    found: List[Dict[str, Any]] = []
    notes: List[str] = []

    amenity = layers.get("amenity")
    if amenity is None:
        notes.append("no amenity extract: no hospital/clinic destination")
    else:
        for index in amenity.matching_tags(["amenity"], cfg.isolation.facility_tags):
            point = _point_of(amenity.geometries[index])
            if point is not None:
                found.append(
                    {
                        "kind": "facility",
                        "tag": amenity.tag_value(index, "amenity"),
                        "name": amenity.tag_value(index, "name")
                        or f"amenity/{amenity.tag_value(index, 'amenity')}",
                        "lon": point[0],
                        "lat": point[1],
                    }
                )

    place = layers.get("place")
    if place is None:
        notes.append("no place extract: no town destination")
    else:
        for index in place.matching_tags(["place"], cfg.isolation.destination_place_tags):
            point = _point_of(place.geometries[index])
            if point is not None:
                found.append(
                    {
                        "kind": "place",
                        "tag": place.tag_value(index, "place"),
                        "name": place.tag_value(index, "name")
                        or f"place/{place.tag_value(index, 'place')}",
                        "lon": point[0],
                        "lat": point[1],
                    }
                )
    return found, notes


def settlement_features(
    place_layer: Optional[damage_module.OSMLayer], config: Any = None
) -> List[Dict[str, Any]]:
    """Settlements to classify: the place nodes tagged ``isolation.place_tags``."""
    cfg = config or app_config
    if place_layer is None:
        return []
    settlements: List[Dict[str, Any]] = []
    for index in place_layer.matching_tags(["place"], cfg.isolation.place_tags):
        point = _point_of(place_layer.geometries[index])
        if point is None:
            continue
        settlements.append(
            {
                "osm_id": place_layer.osm_id[index],
                "name": place_layer.tag_value(index, "name")
                or f"place/{place_layer.tag_value(index, 'place')}",
                "place": place_layer.tag_value(index, "place"),
                "lon": point[0],
                "lat": point[1],
            }
        )
    return settlements


def points_to_utm(points_lon_lat: np.ndarray, to_utm) -> np.ndarray:
    """Project an ``(N, 2)`` (lon, lat) array to UTM metres."""
    points = np.asarray(points_lon_lat, dtype="float64").reshape(-1, 2)
    if points.shape[0] == 0:
        return np.zeros((0, 2), dtype="float64")
    x, y = to_utm(points[:, 0], points[:, 1])
    return np.column_stack([np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64")])


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------
def solve_isolation(
    road_graph: RoadGraph,
    destinations_utm: np.ndarray,
    settlement_points_utm: np.ndarray,
    buildings_affected: Sequence[int],
    *,
    severance_threshold: float,
    snap_distance_m: float,
    severity_weights: Optional[Dict[str, float]] = None,
    buildings_weight: float = 1.0,
    isolation_weight: float = 1.0,
) -> Dict[str, Any]:
    """Classify settlements by pre/post-event reachability (pure, no OSM access).

    Destinations and settlement points are in UTM metres. Returns a dict with the
    typed status, the per-settlement class, distances and priority scores.
    """
    import networkx as nx

    graph = road_graph.graph
    destinations_utm = np.asarray(destinations_utm, dtype="float64").reshape(-1, 2)
    settlement_points_utm = np.asarray(settlement_points_utm, dtype="float64").reshape(-1, 2)
    severity = dict(severity_weights or {})

    result: Dict[str, Any] = {
        "status": IsolationStatus.OK,
        "destinations_total": int(destinations_utm.shape[0]),
        "destinations_snapped": 0,
        "destination_names": [],
        "settlement_node": [],
        "settlement_snap_distance_m": [],
        "classes": [],
        "extra_distance_km": [],
        "pre_distance_m": [],
        "post_distance_m": [],
        "severed_edges": 0,
        "kept_edges": 0,
    }
    if graph.number_of_edges() == 0:
        result["status"] = IsolationStatus.NO_ROAD_GRAPH
        return result

    destination_nodes, _ = nearest_nodes(
        destinations_utm, road_graph.node_points_utm, snap_distance_m
    )
    sources = sorted({int(node) for node in destination_nodes if int(node) >= 0})
    result["destinations_snapped"] = len(sources)
    if not sources:
        result["status"] = IsolationStatus.NO_DESTINATIONS
        return result

    kept = [
        (u, v)
        for u, v, data in graph.edges(data=True)
        if float(data.get("flood_probability", 0.0)) < float(severance_threshold)
    ]
    result["severed_edges"] = graph.number_of_edges() - len(kept)
    result["kept_edges"] = len(kept)
    post_graph = graph.edge_subgraph(kept) if kept else nx.Graph()
    post_sources = [node for node in sources if node in post_graph]

    pre_distance = nx.multi_source_dijkstra_path_length(graph, sources, weight="length_m")
    post_distance: Dict[int, float] = (
        nx.multi_source_dijkstra_path_length(post_graph, post_sources, weight="length_m")
        if post_sources
        else {}
    )

    settlement_nodes, snap_distance = nearest_nodes(
        settlement_points_utm, road_graph.node_points_utm, snap_distance_m
    )
    classes: List[str] = []
    extra: List[Optional[float]] = []
    pre_values: List[Optional[float]] = []
    post_values: List[Optional[float]] = []
    for raw_node in settlement_nodes:
        node = int(raw_node)
        if node < 0 or node not in pre_distance:
            classes.append("no_pre_event_access")
            extra.append(None)
            pre_values.append(None)
            post_values.append(None)
            continue
        pre_value = float(pre_distance[node])
        pre_values.append(pre_value)
        if node not in post_distance:
            classes.append("newly_cut_off")
            extra.append(None)
            post_values.append(None)
            continue
        post_value = float(post_distance[node])
        classes.append("still_connected")
        extra.append(round(max(0.0, post_value - pre_value) / 1000.0, 6))
        post_values.append(post_value)

    result["settlement_node"] = [int(node) for node in settlement_nodes]
    result["settlement_snap_distance_m"] = [float(d) for d in np.atleast_1d(snap_distance)]
    result["classes"] = classes
    result["extra_distance_km"] = extra
    result["pre_distance_m"] = pre_values
    result["post_distance_m"] = post_values
    result["scores"] = [
        float(buildings_weight) * float(count) + float(isolation_weight) * float(severity.get(cls, 0.0))
        for count, cls in zip(buildings_affected, classes)
    ]
    return result


def priority_order(
    classes: Sequence[str], scores: Sequence[float], names: Sequence[str]
) -> List[int]:
    """Indices sorted by descending priority score, with a deterministic tie-break."""
    return sorted(
        range(len(classes)),
        key=lambda index: (
            -float(scores[index]),
            SETTLEMENT_CLASSES.index(classes[index]) if classes[index] in SETTLEMENT_CLASSES else 99,
            names[index] or "",
        ),
    )


def buildings_affected_per_settlement(
    damage_result: Optional[damage_module.DamageResult],
    settlement_points_utm: np.ndarray,
    to_utm,
    radius_m: float,
) -> np.ndarray:
    """Count Stage 6 ``affected`` buildings within ``radius_m`` of each settlement."""
    settlements = np.asarray(settlement_points_utm, dtype="float64").reshape(-1, 2)
    if settlements.shape[0] == 0:
        return np.zeros(0, dtype="int64")
    if damage_result is None or damage_result.building_points is None:
        return np.zeros(settlements.shape[0], dtype="int64")
    affected = damage_result.affected_building_points()
    if affected.shape[0] == 0:
        return np.zeros(settlements.shape[0], dtype="int64")
    affected_utm = points_to_utm(affected, to_utm)
    radius_squared = float(radius_m) ** 2
    counts = np.zeros(settlements.shape[0], dtype="int64")
    for index, point in enumerate(settlements):
        squared = (affected_utm[:, 0] - point[0]) ** 2 + (affected_utm[:, 1] - point[1]) ** 2
        counts[index] = int((squared <= radius_squared).sum())
    return counts


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------
def compute_isolation(
    flood,
    parquet_paths: Dict[str, str],
    damage_result: Optional[damage_module.DamageResult] = None,
    config: Any = None,
) -> IsolationResult:
    """Classify settlement isolation for one AOI.

    ``flood`` is the ``flood_baseline.FloodMapResult``, ``parquet_paths`` the
    ohsome extract map, ``damage_result`` the Stage 6 result used for the damaged
    building count per settlement (optional: without it every settlement reports 0
    damaged buildings and a limitation is recorded).
    """
    from contracts.schemas import CutoffSettlementRecord, FactItem, PipelineStage, SettlementClass

    cfg = config or app_config
    started = time.time()
    timings: Dict[str, float] = {}
    limitations: List[str] = []

    probability, transform = damage_module.flood_grid(flood)
    bbox = damage_module.flood_bbox(flood, transform, probability.shape)
    utm_epsg, to_utm, to_wgs = damage_module.project_to_utm(bbox)
    severance_threshold = float(cfg.isolation.flood_threshold_edge)
    snap_distance_m = float(cfg.isolation.settlement_snap_distance_m)
    flooded_threshold = float(cfg.flood.probability_threshold)

    mark = time.time()
    # Only the three layers this stage uses are read: the 60 k building rows would
    # otherwise be decoded a second time (Stage 6 already holds them).
    needed = {name: path for name, path in (parquet_paths or {}).items() if name in ISOLATION_LAYERS}
    layers = damage_module.read_osm_layers(needed)
    layer_rows = {name: len(layer) for name, layer in layers.items()}
    timings["read_parquet_s"] = round(time.time() - mark, 3)
    highway_layer = layers.get("highway")
    if highway_layer is None or len(highway_layer) == 0:
        limitations.append("no highway extract: settlement isolation was not computed")
        timings["total_s"] = round(time.time() - started, 3)
        return _empty_result(
            IsolationStatus.NO_ROAD_GRAPH,
            timings,
            utm_epsg,
            severance_threshold,
            limitations,
            {"bbox": bbox, "layer_rows": layer_rows},
        )

    mark = time.time()
    highway_utm = damage_module.project_linestrings(highway_layer.geometries, to_utm)

    def sample_probability(x_utm: np.ndarray, y_utm: np.ndarray) -> np.ndarray:
        """Flood probability at UTM sample points (NaN outside the raster)."""
        lon, lat = to_wgs(x_utm, y_utm)
        return damage_module.sample_raster_points(probability, transform, lon, lat)

    road_graph = build_road_graph(
        highway_utm,
        float(cfg.damage.road_sample_spacing_m),
        sample_probability=sample_probability,
        flooded_threshold=flooded_threshold,
    )
    timings["graph_s"] = round(time.time() - mark, 3)

    mark = time.time()
    destinations, destination_notes = destination_features(layers, cfg)
    limitations.extend(destination_notes)
    settlements = settlement_features(layers.get("place"), cfg)
    if not settlements:
        limitations.append(
            "no place node matched isolation.place_tags: no settlement was classified"
        )
    settlement_points = points_to_utm(
        np.asarray([[s["lon"], s["lat"]] for s in settlements], dtype="float64"), to_utm
    )
    destination_points = points_to_utm(
        np.asarray([[d["lon"], d["lat"]] for d in destinations], dtype="float64"), to_utm
    )
    damaged_counts = buildings_affected_per_settlement(
        damage_result, settlement_points, to_utm, float(cfg.isolation.settlement_radius_m)
    )
    timings["snap_s"] = round(time.time() - mark, 3)

    mark = time.time()
    solution = solve_isolation(
        road_graph,
        destination_points,
        settlement_points,
        damaged_counts,
        severance_threshold=severance_threshold,
        snap_distance_m=snap_distance_m,
        severity_weights=dict(cfg.isolation.severity_weights),
        buildings_weight=float(cfg.isolation.priority_buildings_weight),
        isolation_weight=float(cfg.isolation.priority_isolation_weight),
    )
    timings["solve_s"] = round(time.time() - mark, 3)
    timings["total_s"] = round(time.time() - started, 3)

    graph_summary = {
        "nodes": int(road_graph.graph.number_of_nodes()),
        "edges": int(road_graph.graph.number_of_edges()),
        "source_segments": int(road_graph.source_segments),
        "total_length_km": round(road_graph.total_length_km, 3),
        "severed_edges": int(solution["severed_edges"]),
        "kept_edges": int(solution["kept_edges"]),
        "cycle_segments": int(road_graph.cycle_segments),
        "destinations_total": int(solution["destinations_total"]),
        "destinations_snapped": int(solution["destinations_snapped"]),
    }
    provenance: Dict[str, Any] = {
        "bbox": bbox,
        "layer_rows": layer_rows,
        "severance_rule": (
            "edge removed when its length-weighted mean flood probability >= "
            "isolation.flood_threshold_edge"
        ),
        "destination_rule": (
            "amenity tagged isolation.facility_tags plus place nodes tagged "
            "isolation.destination_place_tags"
        ),
        "settlement_rule": "place nodes tagged isolation.place_tags",
        "priority_formula": (
            "priority_buildings_weight * buildings_affected + priority_isolation_weight * "
            "severity_weights[class]"
        ),
        "severity_weights": dict(cfg.isolation.severity_weights),
        "priority_buildings_weight": float(cfg.isolation.priority_buildings_weight),
        "priority_isolation_weight": float(cfg.isolation.priority_isolation_weight),
        "snap_distance_m": snap_distance_m,
        "settlement_radius_m": float(cfg.isolation.settlement_radius_m),
        "destinations": [d["name"] for d in destinations],
    }
    if road_graph.cycle_segments:
        limitations.append(
            f"{road_graph.cycle_segments} road segments form cycles without a junction or "
            "endpoint and are kept uncontracted rather than merged"
        )

    if solution["status"] is not IsolationStatus.OK:
        limitations.append(
            "no hospital/clinic or town destination inside the AOI within the snap distance of a "
            "road node: settlement classes were not derived (no guessing)"
        )
        provenance["destinations"] = []
        return _empty_result(
            solution["status"],
            timings,
            utm_epsg,
            severance_threshold,
            limitations,
            provenance,
            graph_summary=graph_summary,
        )

    classes = set(solution["classes"])
    provenance["classes"] = {name: sum(1 for c in solution["classes"] if c == name) for name in SETTLEMENT_CLASSES}
    assert classes <= set(SETTLEMENT_CLASSES), f"unexpected class in {classes}"

    names = [settlement["name"] for settlement in settlements]
    order = priority_order(solution["classes"], solution["scores"], names)
    rank_of = {index: rank for rank, index in enumerate(order, start=1)}
    records = [
        CutoffSettlementRecord(
            name=names[index],
            settlement_class=SettlementClass(solution["classes"][index]),
            buildings_affected=int(damaged_counts[index]),
            extra_distance_km=solution["extra_distance_km"][index],
            priority_rank=rank_of[index],
        )
        for index in order
    ]
    features = []
    for index, settlement in enumerate(settlements):
        node = int(solution["settlement_node"][index])
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": settlement["osm_id"],
                    "name": settlement["name"],
                    "place": settlement["place"],
                    "class": solution["classes"][index],
                    "priority_rank": rank_of[index],
                    "priority_score": round(float(solution["scores"][index]), 4),
                    "buildings_affected": int(damaged_counts[index]),
                    "extra_distance_km": solution["extra_distance_km"][index],
                    "snapped": node >= 0,
                    "snap_distance_m": round(float(solution["settlement_snap_distance_m"][index]), 2)
                    if node >= 0
                    else None,
                    "road_node": node,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [settlement["lon"], settlement["lat"]],
                },
            }
        )
    severed_km = (
        sum(
            float(data["length_m"])
            for _, _, data in road_graph.graph.edges(data=True)
            if float(data["flood_probability"]) >= severance_threshold
        )
        / 1000.0
    )
    counts = {
        "settlements_total": len(settlements),
        "settlements_newly_cut_off": sum(1 for c in solution["classes"] if c == "newly_cut_off"),
        "settlements_still_connected": sum(1 for c in solution["classes"] if c == "still_connected"),
        "settlements_no_pre_event_access": sum(
            1 for c in solution["classes"] if c == "no_pre_event_access"
        ),
        "settlements_unsnapped": sum(1 for node in solution["settlement_node"] if int(node) < 0),
        "buildings_affected_in_settlements": int(sum(damaged_counts)),
        "destinations_total": int(solution["destinations_total"]),
        "destinations_snapped": int(solution["destinations_snapped"]),
        "roads_severed_edges": int(solution["severed_edges"]),
        "road_km_severed": round(severed_km, 4),
        "max_extra_distance_km": round(
            max([value for value in solution["extra_distance_km"] if value is not None] or [0.0]), 4
        ),
    }
    facts = [
        FactItem(id="settlements_total", value=counts["settlements_total"], unit="count", source_stage=PipelineStage.ISOLATION),
        FactItem(id="settlements_newly_cut_off", value=counts["settlements_newly_cut_off"], unit="count", source_stage=PipelineStage.ISOLATION),
        FactItem(id="settlements_still_connected", value=counts["settlements_still_connected"], unit="count", source_stage=PipelineStage.ISOLATION),
        FactItem(id="settlements_no_pre_event_access", value=counts["settlements_no_pre_event_access"], unit="count", source_stage=PipelineStage.ISOLATION),
        FactItem(id="road_km_severed", value=counts["road_km_severed"], unit="km", source_stage=PipelineStage.ISOLATION),
        FactItem(id="buildings_affected_in_settlements", value=counts["buildings_affected_in_settlements"], unit="count", source_stage=PipelineStage.ISOLATION),
    ]
    if damage_result is None:
        limitations.append("no Stage 6 result supplied: buildings_affected per settlement is 0")

    return IsolationResult(
        settlements={
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
            "features": features,
        },
        records=records,
        counts=counts,
        facts=facts,
        status=IsolationStatus.OK,
        timings=timings,
        utm_epsg=utm_epsg,
        graph_summary=graph_summary,
        severance_threshold=severance_threshold,
        limitations=limitations,
        provenance=provenance,
    )


def _empty_result(
    status: IsolationStatus,
    timings: Dict[str, float],
    utm_epsg: int,
    severance_threshold: float,
    limitations: List[str],
    provenance: Dict[str, Any],
    graph_summary: Optional[Dict[str, Any]] = None,
) -> IsolationResult:
    """Typed empty outcome: no class is invented when the stage cannot be run."""
    return IsolationResult(
        settlements={
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
            "features": [],
        },
        records=[],
        counts={
            "settlements_total": 0,
            "settlements_newly_cut_off": 0,
            "settlements_still_connected": 0,
            "settlements_no_pre_event_access": 0,
            "settlements_unsnapped": 0,
            "buildings_affected_in_settlements": 0,
            "destinations_total": int((graph_summary or {}).get("destinations_total", 0)),
            "destinations_snapped": int((graph_summary or {}).get("destinations_snapped", 0)),
            "roads_severed_edges": int((graph_summary or {}).get("severed_edges", 0)),
            "road_km_severed": 0.0,
            "max_extra_distance_km": 0.0,
        },
        facts=[],
        status=status,
        timings=timings,
        utm_epsg=utm_epsg,
        graph_summary=graph_summary or {},
        severance_threshold=severance_threshold,
        limitations=limitations,
        provenance=provenance,
    )


def counts_report(result: IsolationResult) -> str:
    """One-line count summary used by the spike row."""
    order = (
        "settlements_total",
        "settlements_newly_cut_off",
        "settlements_still_connected",
        "settlements_no_pre_event_access",
        "destinations_snapped",
        "roads_severed_edges",
        "max_extra_distance_km",
    )
    return " ".join(f"{key}={result.counts.get(key)}" for key in order)


def write_isolation_artifacts(
    result: IsolationResult, out_dir: Path, stem: str = "isolation"
) -> Dict[str, str]:
    """Write the settlement layer plus the counts/graph record."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    layer_path = out_dir / f"{stem}_settlements.geojson"
    layer_path.write_text(json.dumps(result.settlements), encoding="utf-8")
    summary_path = out_dir / f"{stem}_counts.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": result.status.value,
                "counts": result.counts,
                "timings": result.timings,
                "graph": result.graph_summary,
                "records": [record.model_dump(by_alias=True) for record in result.records],
                "limitations": result.limitations,
                "provenance": result.provenance,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    return {"settlements": str(layer_path), "counts": str(summary_path)}
