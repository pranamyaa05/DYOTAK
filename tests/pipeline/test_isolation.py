"""Tests for the isolation solver (app/pipeline/isolation.py, Stage 7).

Covers the graph itself (coincident vertices merged, junction/endpoint nodes,
length-preserving contraction, parallel chains merged), per-segment flood
probability, the severance rule, the three classes and their extra distances,
the priority score from config, and the typed outcomes (no destinations, no road
graph).

Edge cases required by ARCHITECTURE 4.7 each get their own test: disconnected
pre-event graph, no facilities, all edges flooded, a settlement far from any
road, and a bridge over a flooded river (with and without a detour).    The synthetic road layout is the same in both fixtures::

    n1 ---- west ---- n2 -- bridge -- n3 -- east -- n4
             |                      |
           stub_n2               stub_n3
    n2 == detour (only in the detour variant) == n4
                              |
                         stub_n4 (detour variant only)

Every node is a junction (degree 3+) or an endpoint (degree 1), which is what
the contraction must preserve: without the stubs ``n2``/``n3`` would be shape
points and the bridge would be swallowed by the chain, and with the detour
``n4`` needs its own stub to stay a junction.

All assertions are structural: no published flood, damage or damage map is
consulted anywhere.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

import osm_fixtures as fx
from app.pipeline import damage as dmg
from app.pipeline import flood_baseline as fb
from app.pipeline import isolation as iso
from app.settings import app_config, settings

BBOX = [85.0, 27.0, 85.1, 27.1]
REPO_ROOT = Path(__file__).resolve().parents[2]

EPSG, TO_UTM, TO_WGS = dmg.project_to_utm(BBOX)

#: Reachable road nodes of the synthetic layout (lon, lat).
N1 = (85.005, 27.065)
N2 = (85.045, 27.065)
N3 = (85.055, 27.065)
N4 = (85.095, 27.065)
N_STUB2 = (85.045, 27.055)
N_STUB3 = (85.055, 27.075)
N_STUB4 = (85.095, 27.055)      # only present in the detour variant


def _utm(lines):
    return dmg.project_linestrings(np.asarray(lines, dtype=object), TO_UTM)


def _graph(lines, spacing_m: float = 20.0) -> iso.RoadGraph:
    return iso.build_road_graph(_utm(lines), spacing_m)


def _point(lon: float, lat: float) -> np.ndarray:
    x, y = TO_UTM(np.asarray([lon]), np.asarray([lat]))
    return np.column_stack([x, y])


def _roads(with_detour: bool = False, with_stubs: bool = True):
    """The synthetic layout described in the module docstring."""
    roads = [
        fx.line([N1, N2]),
        fx.line([N2, N3]),                    # the bridge over the flooded reach
        fx.line([N3, N4]),
    ]
    if with_stubs:
        roads.append(fx.line([N2, N_STUB2]))
        roads.append(fx.line([N3, N_STUB3]))
    if with_detour:
        roads.append(
            fx.line([N2, (85.045, 27.095), (85.095, 27.095), N4])
        )
        # n4 is a junction only because of the detour; this stub keeps it one.
        roads.append(fx.line([N4, N_STUB4]))
    return roads


def _set_probability(graph, predicate):
    """Give every edge a probability from its chord midpoint (lon, lat)."""
    points = graph.node_points_utm
    for u, v, data in graph.graph.edges(data=True):
        midpoint = (points[u] + points[v]) / 2.0
        lon, lat = TO_WGS(np.asarray([midpoint[0]]), np.asarray([midpoint[1]]))
        data["flood_probability"] = 1.0 if predicate(float(lon[0]), float(lat[0])) else 0.0


def _probability_near(graph, lon, lat, tolerance: float = 1e-4) -> float:
    """Probability of the single edge whose midpoint is (lon, lat).

    The tolerance is generous (~11 m) on purpose: the midpoint is taken in UTM
    and projected back, and a straight UTM chord between two WGS84 points does
    not back-project exactly onto their equidistant midpoint.
    """
    points = graph.node_points_utm
    for u, v, data in graph.graph.edges(data=True):
        midpoint = (points[u] + points[v]) / 2.0
        x, y = TO_WGS(np.asarray([midpoint[0]]), np.asarray([midpoint[1]]))
        if abs(float(x[0]) - lon) <= tolerance and abs(float(y[0]) - lat) <= tolerance:
            return float(data["flood_probability"])
    raise AssertionError(f"no edge with midpoint ({lon}, {lat})")


def _solve(graph, destinations, settlements, buildings=None, threshold=0.5, snap=500.0, **kw):
    return iso.solve_isolation(
        graph,
        destinations,
        settlements,
        buildings if buildings is not None else [0] * len(settlements),
        severance_threshold=threshold,
        snap_distance_m=snap,
        severity_weights=kw.pop("severity_weights", dict(app_config.isolation.severity_weights)),
        buildings_weight=kw.pop("buildings_weight", app_config.isolation.priority_buildings_weight),
        isolation_weight=kw.pop("isolation_weight", app_config.isolation.priority_isolation_weight),
        **kw,
    )


def _classes(result):
    return {
        feature["properties"]["name"]: feature["properties"]["class"]
        for feature in result.settlements["features"]
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def test_coincident_vertices_share_one_node():
    first = fx.line([(85.0, 27.0), (85.01, 27.0)])
    second = fx.line([(85.01, 27.0), (85.02, 27.0)])   # starts where the first ends
    graph = _graph([first, second])

    # The shared vertex joins the ways into one road: its degree is 2, so the
    # contraction turns it into a shape point and the two endpoints remain.
    assert graph.graph.number_of_nodes() == 2
    assert graph.graph.number_of_edges() == 1
    assert graph.total_length_km * 1000 == pytest.approx(
        _utm([first])[0].length + _utm([second])[0].length, rel=1e-9
    )
    # A shared vertex that is a junction (a third way ends there) is kept.
    stem = fx.line([(85.01, 27.0), (85.01, 27.015)])
    junction_graph = _graph([first, second, stem])
    assert junction_graph.graph.number_of_nodes() == 4
    assert sorted(dict(junction_graph.graph.degree()).values()) == [1, 1, 1, 3]
    node_ids, _ = iso.vertex_keys(np.asarray([[1.0, 2.0], [1.0 + 1e-9, 2.0]]))
    assert node_ids.tolist() == [0, 0], "1 cm rounding merges coincident vertices"


def test_nodes_are_junctions_and_endpoints_not_shape_points():
    # A way with shape points on a straight line collapses to a single edge.
    shapey = fx.line([(85.0, 27.0), (85.01, 27.0), (85.02, 27.0), (85.03, 27.0)])
    # A T junction whose stem ends on the horizontal way's vertex.
    stem = fx.line([(85.01, 27.0), (85.01, 27.02)])
    graph = _graph([shapey, stem])

    assert graph.graph.number_of_nodes() == 4          # 2 line ends + junction + stem end
    assert sorted(dict(graph.graph.degree()).values()) == [1, 1, 1, 3]
    expected = sum(line.length for line in _utm([shapey, stem]))
    assert graph.total_length_km * 1000 == pytest.approx(expected, rel=1e-9)
    assert graph.source_segments > graph.graph.number_of_edges()


def test_parallel_chains_are_merged_not_overwritten():
    direct = fx.line([(85.0, 27.0), (85.03, 27.0)])
    detour = fx.line([(85.0, 27.0), (85.015, 27.01), (85.03, 27.0)])
    # Stubs turn both ends into junctions, so the two chains between them merge.
    stub_a = fx.line([(85.0, 27.0), (85.0, 27.01)])
    stub_b = fx.line([(85.03, 27.0), (85.03, 27.01)])
    graph = _graph([direct, detour, stub_a, stub_b])

    assert graph.graph.number_of_nodes() == 4
    assert graph.graph.number_of_edges() == 3
    _, _, data = max(graph.graph.edges(data=True), key=lambda edge: edge[2]["length_m"])
    direct_length = _utm([direct])[0].length
    detour_length = _utm([detour])[0].length
    assert data["length_m"] == pytest.approx(direct_length + detour_length, rel=1e-9)


def test_a_cycle_without_any_junction_is_kept_not_dropped():
    """A ring road has no junctions; dropping it would delete real road."""
    direct = fx.line([(85.0, 27.0), (85.03, 27.0)])
    detour = fx.line([(85.0, 27.0), (85.015, 27.01), (85.03, 27.0)])
    graph = _graph([direct, detour])

    assert graph.graph.number_of_nodes() == 3          # both ends + the detour's corner
    assert graph.graph.number_of_edges() == 3
    assert graph.cycle_segments > 0, "the kept cycle must be disclosed"
    assert graph.total_length_km * 1000 == pytest.approx(
        _utm([direct])[0].length + _utm([detour])[0].length, rel=1e-9
    ), "no road length may be lost"


def test_parallel_chains_keep_the_flooded_chain_in_the_statistics():
    """Merging must add flooded length, not overwrite it with the dry chain."""
    flooded_chain = fx.line([(85.0, 27.0), (85.015, 27.01), (85.03, 27.0)])
    dry_chain = fx.line([(85.0, 27.0), (85.03, 27.0)])
    stub_a = fx.line([(85.0, 27.0), (85.0, 27.015)])
    stub_b = fx.line([(85.03, 27.0), (85.03, 27.015)])
    # Flood the detour and nothing else. The straight chain sits on a parallel,
    # which sags ~47 m in UTM over 0.03 deg of longitude here, so the threshold
    # must clear the whole chain, not just its midpoint.
    _, y_direct = TO_UTM(np.asarray([85.0, 85.03]), np.asarray([27.0, 27.0]))
    _, y_detour = TO_UTM(np.asarray([85.015]), np.asarray([27.01]))
    threshold = float(np.max(y_direct)) + 10.0

    graph = iso.build_road_graph(
        _utm([flooded_chain, dry_chain, stub_a, stub_b]),
        20.0,
        sample_probability=lambda x_utm, y_utm: np.where(
            np.asarray(y_utm) > threshold, 1.0, 0.0
        ),
    )
    assert graph.graph.number_of_nodes() == 4
    assert graph.graph.number_of_edges() == 3
    _, _, data = max(graph.graph.edges(data=True), key=lambda edge: edge[2]["length_m"])

    assert data["length_m"] == pytest.approx(
        _utm([flooded_chain])[0].length + _utm([dry_chain])[0].length, rel=1e-9
    )
    detour_length = _utm([flooded_chain])[0].length
    assert 0.95 * detour_length < data["flooded_length_m"] <= detour_length + 1e-6, (
        "the flooded chain's length must be kept, not overwritten by the dry one"
    )
    assert 0.0 < data["flood_probability"] < 1.0
    assert data["max_probability"] == 1.0
    assert y_detour[0] > threshold, "sanity: the detour's apex is above the threshold"


def test_segment_sampling_covers_every_segment_and_sums_to_its_length():
    import shapely

    line = fx.line([(85.0, 27.0), (85.01, 27.0), (85.01, 27.02)])
    lines = _utm([line])
    _, _, segment_start, segment_delta, _ = iso.polyline_segments(lines)
    xs, ys, owner, weights = iso.sample_segments(segment_start, segment_delta, 5.0)

    assert segment_start.shape[0] == 2
    assert xs.shape == ys.shape == owner.shape == weights.shape
    assert sorted(np.unique(owner).tolist()) == [0, 1]
    for segment in (0, 1):
        assert np.isclose(
            weights[owner == segment].sum(), np.hypot(*segment_delta[segment]), atol=1e-6
        )
    assert int(shapely.get_num_coordinates(lines)[0]) == 3


def test_nearest_nodes_respects_the_snap_distance():
    nodes = np.asarray([[0.0, 0.0], [1000.0, 0.0]])
    points = np.asarray([[10.0, 0.0], [5000.0, 0.0]])
    index, distance = iso.nearest_nodes(points, nodes, max_distance_m=500.0)

    assert index[0] == 0
    assert distance[0] == pytest.approx(10.0)
    assert index[1] == -1, "a point beyond the snap distance must not be snapped"
    assert np.isinf(distance[1])


def test_empty_geometry_input_gives_an_empty_graph():
    graph = iso.build_road_graph(np.zeros(0, dtype=object), 20.0)
    assert len(graph) == 0
    assert graph.graph.number_of_edges() == 0
    assert graph.total_length_km == 0.0


# ---------------------------------------------------------------------------
# The three classes
# ---------------------------------------------------------------------------
def test_all_links_intact_keeps_both_settlements_connected():
    graph = _graph(_roads())
    solution = _solve(
        graph,
        _point(*N1),
        np.vstack([_point(*N2), _point(*N4)]),
    )
    assert solution["status"] is iso.IsolationStatus.OK
    assert solution["classes"] == ["still_connected", "still_connected"]
    assert solution["extra_distance_km"][0] == pytest.approx(0.0)
    assert solution["extra_distance_km"][1] == pytest.approx(0.0, abs=1e-9)
    assert solution["severed_edges"] == 0


def test_bridge_over_a_flooded_river_cuts_off_the_far_settlement():
    graph = _graph(_roads(with_detour=False))
    # Only the bridge is flooded: a tight box around its chord midpoint.
    _set_probability(graph, lambda lon, lat: 85.045 <= lon <= 85.055 and 27.064 <= lat <= 27.066)
    assert _probability_near(graph, 85.05, 27.065) == 1.0
    assert _probability_near(graph, 85.025, 27.065) == 0.0

    solution = _solve(graph, _point(*N1), np.vstack([_point(*N2), _point(*N4)]))
    assert solution["classes"] == ["still_connected", "newly_cut_off"]
    assert solution["extra_distance_km"][1] is None
    assert solution["severed_edges"] == 1
    assert solution["kept_edges"] == graph.graph.number_of_edges() - 1


def test_detour_gives_a_positive_extra_distance_instead_of_cut_off():
    graph = _graph(_roads(with_detour=True))
    _set_probability(graph, lambda lon, lat: 85.04 <= lon <= 85.06)
    # Only the bridge is severed; the detour is outside the flooded box.
    assert _probability_near(graph, 85.05, 27.065) == 1.0
    assert _probability_near(graph, 85.07, 27.065) == 0.0

    solution = _solve(graph, _point(*N1), _point(*N4))
    assert solution["classes"] == ["still_connected"]
    extra = solution["extra_distance_km"][0]
    assert extra is not None and extra > 0.0, "the detour must cost distance"
    assert solution["post_distance_m"][0] > solution["pre_distance_m"][0]


def test_disconnecting_every_edge_makes_every_connected_settlement_cut_off():
    graph = _graph(_roads(with_detour=True))
    for _, _, data in graph.graph.edges(data=True):
        data["flood_probability"] = 1.0

    solution = _solve(graph, _point(*N1), np.vstack([_point(*N2), _point(*N4)]))
    assert solution["severed_edges"] == graph.graph.number_of_edges()
    assert solution["kept_edges"] == 0
    assert solution["classes"] == ["newly_cut_off", "newly_cut_off"]
    assert all(value is None for value in solution["extra_distance_km"])


def test_disconnected_pre_event_component_is_not_attributed_to_the_flood():
    island = fx.line([(85.0, 27.02), (85.02, 27.02)])
    mainland = fx.line([(85.0, 27.0), (85.02, 27.0)])
    graph = _graph([island, mainland])
    destinations = _point(85.005, 27.0)                  # on the mainland only
    settlements = np.vstack([_point(85.015, 27.0), _point(85.015, 27.02)])

    solution = _solve(graph, destinations, settlements)
    assert solution["classes"] == ["still_connected", "no_pre_event_access"]
    assert solution["pre_distance_m"][1] is None
    assert solution["extra_distance_km"][1] is None
    assert solution["severed_edges"] == 0, "nothing was flooded: no flood attribution"


def test_settlement_far_from_any_road_is_not_snapped():
    graph = _graph([fx.line([N1, N2])])
    solution = _solve(graph, _point(*N1), _point(85.09, 27.09), snap=500.0)
    assert solution["classes"] == ["no_pre_event_access"]
    assert solution["settlement_node"][0] == -1
    assert np.isinf(solution["settlement_snap_distance_m"][0])


def test_no_destinations_returns_the_typed_state_and_classifies_nothing():
    graph = _graph(_roads(with_detour=True))
    solution = _solve(graph, np.zeros((0, 2)), _point(*N4))
    assert solution["status"] is iso.IsolationStatus.NO_DESTINATIONS
    assert solution["classes"] == []
    assert solution["settlement_node"] == []
    assert solution["destinations_total"] == 0


def test_destination_beyond_the_snap_distance_is_not_a_destination():
    graph = _graph([fx.line([N1, N2])])
    solution = _solve(graph, _point(85.09, 27.09), _point(*N1), snap=500.0)
    assert solution["status"] is iso.IsolationStatus.NO_DESTINATIONS
    assert solution["destinations_snapped"] == 0


def test_graph_without_edges_is_the_typed_no_road_graph_state():
    empty = iso.build_road_graph(np.zeros(0, dtype=object), 20.0)
    solution = _solve(empty, _point(*N1), _point(*N4))
    assert solution["status"] is iso.IsolationStatus.NO_ROAD_GRAPH


# ---------------------------------------------------------------------------
# Priority rank (weights from config)
# ---------------------------------------------------------------------------
def test_priority_orders_by_score_then_deterministically():
    classes = ["still_connected", "newly_cut_off", "no_pre_event_access"]
    scores = [2.0, 10.0, 1.5]
    assert iso.priority_order(classes, scores, ["b", "a", "c"]) == [1, 0, 2], (
        "the priority score (buildings + severity, ARCHITECTURE 4.7) decides"
    )

    tied = iso.priority_order(["still_connected", "still_connected"], [1.0, 1.0], ["beta", "alpha"])
    assert tied == [1, 0]
    assert tied == iso.priority_order(
        ["still_connected", "still_connected"], [1.0, 1.0], ["beta", "alpha"]
    ), "ties must be reproducible"

    by_class = iso.priority_order(classes, [1.0, 1.0, 1.0], ["b", "a", "c"])
    assert by_class == [1, 0, 2], "equal scores fall back to class severity, then name"


def test_priority_score_uses_the_configured_weights():
    graph = _graph(_roads(with_detour=False))
    _set_probability(graph, lambda lon, lat: 85.045 <= lon <= 85.055 and 27.064 <= lat <= 27.066)
    destinations = _point(*N1)
    settlements = np.vstack([_point(*N2), _point(*N4)])

    solution = _solve(graph, destinations, settlements, buildings=[0, 0])
    weights = app_config.isolation.severity_weights
    assert solution["scores"] == pytest.approx(
        [
            app_config.isolation.priority_buildings_weight * 0
            + app_config.isolation.priority_isolation_weight * weights["still_connected"],
            app_config.isolation.priority_buildings_weight * 0
            + app_config.isolation.priority_isolation_weight * weights["newly_cut_off"],
        ]
    )
    assert solution["scores"][1] > solution["scores"][0]
    assert iso.priority_order(solution["classes"], solution["scores"], ["a", "b"]) == [1, 0]

    with_buildings = _solve(graph, destinations, settlements, buildings=[0, 5])
    delta = app_config.isolation.priority_buildings_weight * 5
    assert with_buildings["scores"][1] - with_buildings["scores"][0] == pytest.approx(
        (solution["scores"][1] - solution["scores"][0]) + delta
    ), "five buildings add exactly priority_buildings_weight to the score difference"


# ---------------------------------------------------------------------------
# Destinations / settlements read from the real schema
# ---------------------------------------------------------------------------
def _destination_and_settlement_layers():
    """Amenity and place rows (written by the caller) for the synthetic layout."""
    amenity = [
        fx.feature(1, fx.square_box(*N1, 0.002), {"amenity": "hospital", "name": "hospital"}, osm_type="node"),
        fx.feature(2, fx.square_box(*N2, 0.002), {"amenity": "clinic", "name": "clinic"}, osm_type="node"),
        fx.feature(3, fx.square_box(85.05, 27.09, 0.002), {"amenity": "pharmacy"}, osm_type="node"),
    ]
    place = [
        fx.feature(10, fx.square_box(*N2, 0.002), {"place": "village", "name": "west"}, osm_type="node"),
        fx.feature(11, fx.square_box(*N4, 0.002), {"place": "hamlet", "name": "east"}, osm_type="node"),
        fx.feature(12, fx.square_box(85.09, 27.01, 0.002), {"place": "hamlet", "name": "far"}, osm_type="node"),
        fx.feature(13, fx.square_box(85.03, 27.09, 0.002), {"place": "suburb", "name": "ignored"}, osm_type="node"),
        fx.feature(14, fx.square_box(*N_STUB3, 0.002), {"place": "village", "name": "east2"}, osm_type="node"),
    ]
    return {"amenity": amenity, "place": place}


def test_destinations_and_settlements_follow_the_config_tags(tmp_path):
    rows = _destination_and_settlement_layers()
    layers = dmg.read_osm_layers(
        fx.write_extract_map(tmp_path, rows)
    )

    destinations, notes = iso.destination_features(layers)
    # pharmacy is not a facility tag; no place node carries destination_place_tags.
    assert sorted(d["name"] for d in destinations) == ["clinic", "hospital"]
    assert {d["kind"] for d in destinations} == {"facility"}
    assert notes == []

    settlements = iso.settlement_features(layers["place"])
    assert sorted(s["name"] for s in settlements) == ["east", "east2", "far", "west"]
    assert all(s["place"] in app_config.isolation.place_tags for s in settlements)
    assert all(np.isfinite(s["lon"]) and np.isfinite(s["lat"]) for s in settlements)


def test_destination_place_tags_add_a_town_destination(tmp_path):
    rows = _destination_and_settlement_layers()
    layers = dmg.read_osm_layers(fx.write_extract_map(tmp_path, rows))
    original = list(app_config.isolation.destination_place_tags)
    try:
        app_config.isolation.destination_place_tags.clear()
        app_config.isolation.destination_place_tags.append("hamlet")
        destinations, _ = iso.destination_features(layers)
    finally:
        app_config.isolation.destination_place_tags.clear()
        app_config.isolation.destination_place_tags.extend(original)
    # hospital + clinic + the two hamlets now tagged as destinations ("towns").
    assert sorted(d["name"] for d in destinations) == ["clinic", "east", "far", "hospital"]
    assert {d["kind"] for d in destinations} == {"facility", "place"}


def test_missing_layers_are_reported_as_notes():
    destinations, notes = iso.destination_features({})
    assert destinations == []
    assert any("amenity" in note for note in notes)
    assert any("place" in note for note in notes)
    assert iso.settlement_features(None) == []


# ---------------------------------------------------------------------------
# Full stage on synthetic extracts
# ---------------------------------------------------------------------------
def _bridge_fixture(tmp_path, patch_cols=(4, 5), with_detour=True):
    """Flood stub + real-schema extracts for the west / bridge / east scenario."""
    flood, _ = fx.flood_stub(patch_rows=(2, 3, 4), patch_cols=patch_cols)
    tags_and_lines = [
        (1, (N1, N2), {"highway": "secondary"}),
        (2, (N2, N3), {"bridge": "yes", "highway": "secondary"}),
        (3, (N3, N4), {"highway": "secondary"}),
        (4, (N2, N_STUB2), {"highway": "service"}),
        (5, (N3, N_STUB3), {"highway": "service"}),
    ]
    rows = _destination_and_settlement_layers()
    roads = [fx.feature(osm_id, fx.line(points), tags) for osm_id, points, tags in tags_and_lines]
    if with_detour:
        roads.append(
            fx.feature(6, fx.line([N2, (85.045, 27.095), (85.095, 27.095), N4]), {"highway": "track"})
        )
        roads.append(fx.feature(7, fx.line([N4, N_STUB4]), {"highway": "service"}))
    buildings = [
        fx.feature(100, fx.square_box(*N2, 0.002), {"building": "yes"}),           # flooded
        fx.feature(101, fx.square_box(85.09, 27.01, 0.002), {"building": "yes"}),  # dry
    ]
    paths = fx.write_extract_map(
        tmp_path,
        {
            "building": buildings,
            "highway": roads,
            "amenity": rows["amenity"],
            "place": rows["place"],
        },
    )
    return flood, paths


def test_compute_isolation_severs_the_bridge_and_keeps_the_detour(tmp_path):
    flood, paths = _bridge_fixture(tmp_path, patch_cols=(4, 5), with_detour=True)
    result = iso.compute_isolation(flood, paths)

    assert result.status is iso.IsolationStatus.OK
    classes = _classes(result)
    assert classes["west"] == "still_connected"
    assert classes["east"] == "still_connected", "the detour keeps it reachable"
    assert classes["far"] == "no_pre_event_access", "far from any road, pre-event"

    assert result.counts["settlements_total"] == 4        # suburb is not a settlement
    assert result.counts["settlements_no_pre_event_access"] == 1
    assert result.counts["destinations_total"] == 2
    assert result.counts["destinations_snapped"] == 2
    assert result.counts["settlements_unsnapped"] == 1
    assert result.counts["roads_severed_edges"] >= 1, "the flooded crossing must be severed"
    assert result.counts["max_extra_distance_km"] > 0.0, "the detour costs distance"

    records = {record.name: record for record in result.records}
    assert set(records) == {"west", "east", "east2", "far"}
    assert all(
        record.settlement_class.value in iso.SETTLEMENT_CLASSES for record in records.values()
    )
    ranks = sorted(record.priority_rank for record in result.records)
    assert ranks == [1, 2, 3, 4], "priority ranks are a contiguous ranking"
    assert records["far"].settlement_class.value == "no_pre_event_access"
    assert records["far"].extra_distance_km is None

    feature = next(f for f in result.settlements["features"] if f["properties"]["name"] == "east")
    assert set(feature["properties"]) >= {
        "name", "class", "priority_rank", "priority_score", "buildings_affected",
        "extra_distance_km", "snapped", "snap_distance_m", "road_node",
    }
    assert feature["properties"]["snapped"] is True
    assert feature["geometry"]["type"] == "Point"
    assert feature["geometry"]["coordinates"] == pytest.approx([N4[0], N4[1]], abs=1e-9)
    assert result.utm_epsg == 32645
    assert result.graph_summary["nodes"] >= 6
    assert result.graph_summary["severed_edges"] + result.graph_summary["kept_edges"] == (
        result.graph_summary["edges"]
    )


def test_compute_isolation_without_a_detour_reports_newly_cut_off(tmp_path):
    flood, paths = _bridge_fixture(tmp_path, patch_cols=(4, 5), with_detour=False)
    result = iso.compute_isolation(flood, paths)
    classes = _classes(result)

    assert classes["east"] == "newly_cut_off"
    assert classes["west"] == "still_connected"
    assert classes["far"] == "no_pre_event_access"
    # east and east2 (on the stub) are both reached only across the bridge.
    assert result.counts["settlements_newly_cut_off"] == 2
    facts = {fact.id: fact for fact in result.facts}
    assert facts["settlements_newly_cut_off"].value == 2
    assert facts["settlements_newly_cut_off"].unit == "count"
    assert facts["settlements_newly_cut_off"].source_stage.value == "isolation"
    assert facts["road_km_severed"].unit == "km"
    assert facts["road_km_severed"].value > 0
    assert result.counts["settlements_still_connected"] >= 1


def test_no_destinations_state_from_a_real_extract(tmp_path):
    flood, paths = _bridge_fixture(tmp_path)
    paths.pop("amenity", None)
    place_rows = [
        fx.feature(10, fx.square_box(*N2, 0.002), {"place": "village", "name": "west"}, osm_type="node"),
    ]
    paths["place"] = str(fx.write_geoparquet(tmp_path / "place.parquet", place_rows))

    original = list(app_config.isolation.destination_place_tags)
    try:
        app_config.isolation.destination_place_tags.clear()
        result = iso.compute_isolation(flood, paths)
    finally:
        app_config.isolation.destination_place_tags.clear()
        app_config.isolation.destination_place_tags.extend(original)

    assert result.status is iso.IsolationStatus.NO_DESTINATIONS
    assert result.no_destinations is True
    assert result.records == []
    assert result.settlements["features"] == []
    assert result.facts == []
    assert result.counts["settlements_newly_cut_off"] == 0
    assert result.counts["settlements_total"] == 0
    assert any("destination" in note for note in result.limitations)


def test_missing_highway_layer_returns_the_typed_no_road_graph_state(tmp_path):
    flood, paths = _bridge_fixture(tmp_path)
    paths.pop("highway", None)
    result = iso.compute_isolation(flood, paths)
    assert result.status is iso.IsolationStatus.NO_ROAD_GRAPH
    assert result.records == []
    assert any("highway" in note for note in result.limitations)


def test_buildings_affected_counts_only_the_affected_footprints_in_the_radius():
    settlement = _point(*N2)                       # UTM, as Stage 7 holds it
    # Stage 6 keeps building points as WGS84 lon/lat.
    affected = np.asarray(
        [
            [85.046, 27.066],   # ~150 m away
            [85.044, 27.064],   # ~150 m away
            [85.09, 27.09],     # far outside the radius
        ],
        dtype="float64",
    )
    result = dmg.DamageResult(
        buildings={}, roads={}, bridges={}, counts={}, facts=[], timings={}, utm_epsg=EPSG,
        building_points=affected,
        building_status_codes=np.asarray([2, 2, 2], dtype="int8"),
    )
    assert iso.buildings_affected_per_settlement(result, settlement, TO_UTM, 500.0).tolist() == [2]
    assert iso.buildings_affected_per_settlement(result, settlement, TO_UTM, 50.0).tolist() == [0]
    assert iso.buildings_affected_per_settlement(None, settlement, TO_UTM, 500.0).tolist() == [0]
    assert iso.buildings_affected_per_settlement(
        result, np.zeros((0, 2)), TO_UTM, 500.0
    ).tolist() == []


def test_stage_uses_the_damage_result_for_the_buildings_count(tmp_path):
    flood, paths = _bridge_fixture(tmp_path, with_detour=True)
    damage_result = dmg.classify_damage(flood, paths)
    # Put an affected building right next to the far settlement instead of the road.
    far_point = _point(85.09, 27.01)
    damage_result.building_points = np.asarray(
        [[85.09, 27.01], [85.045, 27.066]], dtype="float64"   # WGS84, as Stage 6 keeps them
    )
    damage_result.building_status_codes = np.asarray([2, 2], dtype="int8")

    without = iso.compute_isolation(flood, paths, None)
    with_result = iso.compute_isolation(flood, paths, damage_result)
    assert without.counts["buildings_affected_in_settlements"] == 0
    assert any("Stage 6" in note for note in without.limitations)
    assert with_result.counts["buildings_affected_in_settlements"] >= 1
    far_record = next(r for r in with_result.records if r.name == "far")
    assert far_record.buildings_affected == 1


def test_write_isolation_artifacts_round_trips(tmp_path):
    flood, paths = _bridge_fixture(tmp_path)
    result = iso.compute_isolation(flood, paths)
    written = iso.write_isolation_artifacts(result, tmp_path / "out")

    assert set(written) == {"settlements", "counts"}
    layer = json.loads(Path(written["settlements"]).read_text(encoding="utf-8"))
    assert layer["type"] == "FeatureCollection"
    assert layer["crs"]["properties"]["name"].endswith("EPSG::4326")
    summary = json.loads(Path(written["counts"]).read_text(encoding="utf-8"))
    assert summary["status"] == result.status.value
    assert summary["counts"] == result.counts
    assert len(summary["records"]) == len(result.records)
    assert summary["graph"]["nodes"] > 0
    assert "priority_formula" in summary["provenance"]
    assert "severance_rule" in summary["provenance"]


def test_counts_report_renders_the_spike_line(tmp_path):
    flood, paths = _bridge_fixture(tmp_path)
    result = iso.compute_isolation(flood, paths)
    line = iso.counts_report(result)
    assert "settlements_total=" in line
    assert "roads_severed_edges=" in line
    assert "max_extra_distance_km=" in line


# ---------------------------------------------------------------------------
# Integration: live Trishuli extracts, structural properties only
# ---------------------------------------------------------------------------
needs_cdse = pytest.mark.skipif(
    not (settings.cdse_client_id and settings.cdse_client_secret),
    reason="CDSE credentials required for the live baseline flood map",
)
needs_ohsome = pytest.mark.skipif(
    not settings.ohsome_api_key,
    reason="ohsome API key required for the pre-event extract",
)


def _trishuli_preset():
    with open(REPO_ROOT / "config" / "presets" / "trishuli_emsr927.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.integration
@needs_cdse
@needs_ohsome
def test_trishuli_isolation_structure():
    """Live run: structural assertions only, never a reference map comparison."""
    from datetime import date, timedelta

    from app.pipeline import cdse, ohsome

    preset = _trishuli_preset()
    bbox = [float(c) for c in preset["bbox"]]
    event_date = str(preset["event_date"])
    search_start = (
        date.fromisoformat(event_date) - timedelta(days=app_config.pairing.search_window_days)
    ).isoformat()
    search_end = (date.fromisoformat(event_date) + timedelta(days=7)).isoformat()

    scenes = cdse.search_sentinel1_scenes(bbox, search_start, search_end, instrument_mode="IW")
    posts = [s for s in scenes if str(s["acquisition_time"])[:10] >= event_date]
    pres = [s for s in scenes if str(s["acquisition_time"])[:10] < event_date]
    flood = fb.fetch_and_build_flood_map(min(posts, key=lambda s: s["acquisition_time"]), pres, bbox=bbox)

    extract = ohsome.fetch_preevent_osm_elements(
        bbox, str(preset.get("osm_snapshot_date")), event_date
    )
    damage_result = dmg.classify_damage(flood, extract["parquet_paths"])
    result = iso.compute_isolation(flood, extract["parquet_paths"], damage_result)

    assert result.status is iso.IsolationStatus.OK
    assert result.counts["settlements_total"] > 0
    assert result.counts["destinations_snapped"] >= 1

    total = (
        result.counts["settlements_newly_cut_off"]
        + result.counts["settlements_still_connected"]
        + result.counts["settlements_no_pre_event_access"]
    )
    assert total == result.counts["settlements_total"], "every settlement gets exactly one class"
    assert set(result.provenance["classes"]) == set(iso.SETTLEMENT_CLASSES)

    ranks = sorted(record.priority_rank for record in result.records)
    assert ranks == list(range(1, len(result.records) + 1)), "priority ranks are a full ranking"
    assert all(
        record.extra_distance_km is None or record.extra_distance_km >= 0
        for record in result.records
    )

    graph = result.graph_summary
    assert graph["nodes"] > 0 and graph["edges"] > 0
    assert graph["severed_edges"] + graph["kept_edges"] == graph["edges"]
    assert graph["total_length_km"] > 0
    assert result.utm_epsg == 32645

    for feature in result.settlements["features"]:
        properties = feature["properties"]
        assert properties["class"] in iso.SETTLEMENT_CLASSES
        assert isinstance(properties["snapped"], bool)
        assert properties["priority_rank"] >= 1
        assert isinstance(properties["buildings_affected"], int)
