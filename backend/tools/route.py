"""The route a hiker walks on a trail: a loop as-is, or out-and-back on a linear trail.

A trail is a loop if its start and end are within LOOP_MAX_GAP_KM (500 m). A linear trail is walked out and
back: the full route is twice its length, or, when the hiker asked for less, out to target/2 along the trail and
back the same way. Climbing counts both directions: descending on the way out is climbing on the way back, so the
route's climb is the sum of the positive changes over the whole out-and-back profile.
"""

from backend.tools.trails import haversine

LOOP_MAX_GAP_KM = 0.5   # e.g. Rolling Hills MTB Trail (ends 0.30 km apart) and North Meadow Trail Loop (0.58 km: no)


def is_loop(coords):
    return len(coords) >= 2 and haversine(coords[0], coords[-1]) <= LOOP_MAX_GAP_KM


def is_partial(trail, target_km=None):
    """True when a linear trail is turned around before its end (the hiker asked for less than out-and-back)."""
    length = float(trail.get("trail_km") or trail.get("distance_km") or 0)
    if is_loop(trail.get("coords") or []):
        return False
    return bool(target_km) and 0 < float(target_km) < 2 * length


def route_km(trail, target_km=None):
    """Distance the hiker walks on this trail (used for ranking and timing)."""
    length = float(trail.get("trail_km") or trail.get("distance_km") or 0)
    if is_loop(trail.get("coords") or []):
        return length
    full = 2 * length
    return float(target_km) if target_km and 0 < float(target_km) < full else full


def _cumulative(coords):
    out = [0.0]
    for a, b in zip(coords, coords[1:]):
        out.append(out[-1] + haversine(a, b))
    return out


def _cut(coords, km):
    """coords up to km along the trail, ending exactly at km (interpolated)."""
    cum = _cumulative(coords)
    if km >= cum[-1]:
        return list(coords)
    out = [coords[0]]
    for i in range(1, len(coords)):
        if cum[i] < km:
            out.append(coords[i])
            continue
        span = cum[i] - cum[i - 1] or 1.0
        t = (km - cum[i - 1]) / span
        a, b = coords[i - 1], coords[i]
        out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
        break
    return out


def _heights_to(profile, km):
    """Heights from the start to km along the trail, the last one interpolated at km."""
    out = []
    for i, (k, h) in enumerate(profile):
        if k <= km:
            out.append(h)
            continue
        if i:
            k0, h0 = profile[i - 1]
            out.append(h0 + (h - h0) * ((km - k0) / ((k - k0) or 1.0)))
        break
    return out


def climb_m(elevation, kind, length_km, turn_km):
    """Total climbing on the route in m, or None when the elevation is unknown.

    elevation: a get_elevation_gain result (profile [[km, m], ...] when known, else gain/loss totals).
    """
    if not isinstance(elevation, dict) or elevation.get("available") is False:
        return None
    gain = elevation.get("elevation_gain_m")
    if gain is None:
        return None
    if kind == "loop":
        return round(gain)
    profile = elevation.get("profile")
    if profile and len(profile) >= 2:
        heights = _heights_to(profile, turn_km)
        steps = [b - a for a, b in zip(heights, heights[1:])]
        return round(sum(abs(s) for s in steps))  # out: the rises; back: the drops, climbed in reverse
    loss = elevation.get("elevation_loss_m", gain)  # older cache entries: assume the trail ends where it began
    fraction = min(1.0, turn_km / length_km) if length_km else 1.0
    return round((gain + loss) * fraction)


def trailhead_distance_km(trail, ref):
    """Distance from ref to the nearer end of a linear trail (its start for a loop)."""
    coords = trail.get("coords") or []
    if not coords:
        return None
    if is_loop(coords):
        return haversine(ref, coords[0])
    return min(haversine(ref, coords[0]), haversine(ref, coords[-1]))


def _orient(coords, elevation, ref):
    """A linear trail starts at the end nearer ref (OSM way direction is arbitrary): coords and profile."""
    if ref is None or len(coords) < 2 or haversine(ref, coords[-1]) >= haversine(ref, coords[0]):
        return coords, elevation
    if isinstance(elevation, dict) and elevation.get("profile"):
        length = elevation["profile"][-1][0]
        elevation = {**elevation, "profile": [[round(length - k, 3), h] for k, h in reversed(elevation["profile"])],
                     "elevation_gain_m": elevation.get("elevation_loss_m", elevation.get("elevation_gain_m")),
                     "elevation_loss_m": elevation.get("elevation_gain_m")}
    elif isinstance(elevation, dict) and "elevation_loss_m" in elevation:
        elevation = {**elevation, "elevation_gain_m": elevation["elevation_loss_m"],
                     "elevation_loss_m": elevation["elevation_gain_m"]}
    return coords[::-1], elevation


def make_route(trail, target_km=None, elevation=None, ref=None):
    """The plan's route for a trail: {kind, route_km, trail_km, turnaround_km, turnaround_index, coords, climb_m}.

    coords for out-and-back start at the end nearer ref, go out to the turnaround and back the same way (the map,
    the simulation and the narration follow them); turnaround_index is the coordinate index of the turnaround.
    """
    coords = [list(p) for p in trail.get("coords") or []]
    length = float(trail.get("trail_km") or trail.get("distance_km") or 0)
    if not is_loop(coords):
        coords, elevation = _orient(coords, elevation, ref)
    if is_loop(coords):
        return {"kind": "loop", "route_km": round(length, 3), "trail_km": round(length, 3), "turnaround_km": None,
                "turnaround_index": None, "coords": coords, "climb_m": climb_m(elevation, "loop", length, length)}
    total = route_km({**trail, "trail_km": length}, target_km)
    turn = min(length, total / 2)
    out = _cut(coords, turn) if turn < length else coords
    route = out + out[-2::-1]
    return {"kind": "out-and-back", "route_km": round(2 * turn, 3), "trail_km": round(length, 3),
            "turnaround_km": round(turn, 3), "turnaround_index": len(out) - 1, "coords": route,
            "climb_m": climb_m(elevation, "out-and-back", length, turn)}
