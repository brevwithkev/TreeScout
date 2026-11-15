"""Score candidate trails 0-1 against the hiker's constraints."""

from backend.tools.trails import haversine

WEIGHTS = {"distance": 0.5, "loop": 0.2, "center": 0.2, "elevation": 0.1}
LOOP_MAX_GAP_KM = 0.2
CENTER_RADIUS_KM = 15.0   # matches the default /plan search radius
HILLY_M_PER_KM = 30.0     # gain per km treated as "fully hilly"


def _is_loop(coords):
    return len(coords) >= 2 and haversine(coords[0], coords[-1]) <= LOOP_MAX_GAP_KM


def _parts(trail, target_km, prefs, center):
    coords = trail.get("coords") or []
    distance = float(trail.get("distance_km") or 0)
    parts = {}
    if target_km and target_km > 0:
        parts["distance"] = max(0.0, 1 - abs(distance - target_km) / target_km)
    parts["loop"] = 1.0 if _is_loop(coords) else 0.0
    if coords:
        trailhead_km = haversine(center, coords[0])
        parts["center"] = max(0.0, 1 - trailhead_km / CENTER_RADIUS_KM)
    gain = trail.get("elevation_gain_m")
    wants = {"easy", "hilly"} & set(prefs or [])
    if gain is not None and distance > 0 and len(wants) == 1:
        steepness = min(1.0, float(gain) / distance / HILLY_M_PER_KM)
        parts["elevation"] = steepness if "hilly" in wants else 1 - steepness
    return parts


def rank_trails(trails, constraints, center_lat, center_lon):
    """Return copies of all trails sorted best-first, each with "score" (0-1) and "score_parts".

    Weights: distance closeness 0.5, loop 0.2, closeness to center 0.2, elevation preference 0.1.
    A component that cannot be computed (no target distance, unknown elevation, no easy/hilly
    preference) is left out and the remaining weights are renormalized, so scores stay 0-1.
    """
    constraints = constraints or {}
    target = constraints.get("distance_km")
    target = float(target) if target not in (None, "") else None
    prefs = [str(p).lower() for p in constraints.get("prefs") or []]
    ranked = []
    for trail in trails:
        parts = _parts(trail, target, prefs, [float(center_lat), float(center_lon)])
        weight = sum(WEIGHTS[k] for k in parts)
        score = sum(WEIGHTS[k] * v for k, v in parts.items()) / weight if weight else 0.0
        ranked.append({**trail, "score": round(score, 3),
                       "score_parts": {k: round(v, 3) for k, v in parts.items()}})
    ranked.sort(key=lambda t: (-t["score"], t.get("osm_id", 0)))
    return ranked
