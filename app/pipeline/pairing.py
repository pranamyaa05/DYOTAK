"""Sentinel-1 Orbit Pairing Rules (Stage 1).

Enforces .cursorrules Rule 6:
Compare Sentinel-1 only from the same relative orbit AND direction.
If no valid pair exists, raise NO_VALID_ORBIT_PAIR. Never produce a map.
"""

from typing import Any, Dict, List
from app.common.errors import NoValidOrbitPairError
from contracts.schemas import OrbitDirection, SceneMetadata, ScenePairingProvenance


def validate_and_pair_s1_scenes(
    post_scene: Dict[str, Any],
    candidate_pre_scenes: List[Dict[str, Any]],
    max_pre_scenes: int = 3
) -> ScenePairingProvenance:
    """Validate same-relative-orbit and same-direction Sentinel-1 pairing.

    Pure function.
    Raises NoValidOrbitPairError if no valid pre scene matches.
    """
    post_rel_orbit = post_scene.get("relative_orbit")
    post_direction = post_scene.get("orbit_direction")
    post_time = post_scene.get("acquisition_time")

    if post_rel_orbit is None or post_direction is None:
        raise NoValidOrbitPairError(
            relative_orbit=post_rel_orbit,
            direction=post_direction,
            reason="Post-event scene metadata missing relative orbit or direction"
        )

    # Filter candidate pre-scenes strictly matching relative orbit, direction, and strictly before post_time
    valid_pre_scenes = []
    for s in candidate_pre_scenes:
        if (
            s.get("relative_orbit") == post_rel_orbit
            and s.get("orbit_direction") == post_direction
            and s.get("acquisition_time", "") < post_time
        ):
            valid_pre_scenes.append(s)

    if not valid_pre_scenes:
        raise NoValidOrbitPairError(
            relative_orbit=post_rel_orbit,
            direction=post_direction,
            reason="No pre-event scene found with matching relative orbit and pass direction"
        )

    # Sort pre-scenes by acquisition time descending (most recent first) up to max_pre_scenes
    valid_pre_scenes.sort(key=lambda x: x.get("acquisition_time", ""), reverse=True)
    selected_pre = valid_pre_scenes[:max_pre_scenes]

    return ScenePairingProvenance(
        post=SceneMetadata(
            scene_id=post_scene["scene_id"],
            acquisition_time=post_time,
            orbit_direction=OrbitDirection(post_direction),
            relative_orbit=post_rel_orbit
        ),
        pre=[
            SceneMetadata(
                scene_id=p["scene_id"],
                acquisition_time=p["acquisition_time"],
                orbit_direction=OrbitDirection(p["orbit_direction"]),
                relative_orbit=p["relative_orbit"]
            )
            for p in selected_pre
        ],
        relative_orbit=post_rel_orbit,
        direction=OrbitDirection(post_direction)
    )
