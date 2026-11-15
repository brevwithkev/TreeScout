"""Score candidate trails 0-1 against the hiker's constraints."""

from backend.tools.route import LOOP_MAX_GAP_KM, is_loop, route_km, trailhead_distance_km

# Distance dominates: loop + center + elevation together are worth at most 0.25, so a trail whose
# distance part is more than 1/3 higher (i.e. much closer to the requested distance) always ranks first.
WEIGHTS = {"distance": 0.75, "loop": 0.1, "center": 0.1, "elevation": 0.05}
ELEVATION_UNKNOWN = 0.5   # neutral elevation part when easy/hilly was asked but the gain is unknown
CENTER_RADIUS_KM = 15.0   # matches the default /plan search radius
HILLY_M_PER_KM = 30.0     # gain per km treated as "fully hilly"


_is_loop = is_loop  # a loop: start and end within LOOP_MAX_GAP_KM (200 m)


def _parts(trail, target_km, prefs, center):
    coords = trail.get("coords") or []
    distance = float(trail.get("distance_km") or 0)
    walked = route_km(trail, target_km)  # loop: its length; linear: out-and-back (to target/2 if shorter)
    parts = {}
    if target_km and target_km > 0:
        parts["distance"] = max(0.0, 1 - abs(walked - target_km) / target_km)
    parts["loop"] = 1.0 if _is_loop(coords) else 0.0
    if coords:
        trailhead_km = trailhead_distance_km(trail, center)
        parts["center"] = max(0.0, 1 - trailhead_km / CENTER_RADIUS_KM)
    gain = trail.get("elevation_gain_m")
    wants = {"easy", "hilly"} & set(prefs or [])
    if len(wants) == 1:  # same parts for every trail, so scores compare fairly
        if gain is not None and distance > 0:
            steepness = min(1.0, float(gain) / distance / HILLY_M_PER_KM)
            parts["elevation"] = steepness if "hilly" in wants else 1 - steepness
        else:
            parts["elevation"] = ELEVATION_UNKNOWN
    return parts


def rank_trails(trails, constraints, center_lat, center_lon):
    """Return copies of all trails sorted best-first, each with "score" (0-1) and "score_parts".

    Weights: distance closeness 0.75, loop 0.1, closeness to center 0.1, elevation preference 0.05. Distance is
    the route the hiker walks (backend/tools/route.py): a loop's length, or a linear trail out and back.
    Every trail gets the same parts: distance only when a target distance is given, elevation only for
    an easy/hilly preference (neutral 0.5 when a trail's gain is unknown). Weights are renormalized over
    those parts, so scores stay 0-1 and a trail whose distance part is more than 1/3 higher always wins.
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
