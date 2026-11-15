"""Elevation profile smoothing: SRTM's 30 m pixels on canyon switchbacks jump between cliff top and bottom, which
inflates climb and descent. Before summing: resample to points at least MIN_SPACING_KM apart along the route,
smooth with a 3-point moving average, then count climbs/descents only past a HYSTERESIS_M threshold.

A profile is [[km along the trail, m], ...] (backend/tools/elevation.py stores the raw one).
"""

MIN_SPACING_KM = 0.05  # 50 m
HYSTERESIS_M = 10.0


def resample(profile, min_km=MIN_SPACING_KM):
    """Keep points at least min_km apart (always the first and the last)."""
    if len(profile) < 3:
        return [list(p) for p in profile]
    out = [list(profile[0])]
    for k, h in profile[1:-1]:
        if k - out[-1][0] >= min_km:
            out.append([k, h])
    last = list(profile[-1])
    if last[0] - out[-1][0] < min_km and len(out) > 1:
        out[-1] = last           # too close to the last kept point: the true end wins
    else:
        out.append(last)
    return out


def moving_average(profile):
    """3-point moving average of the heights; the end points keep their own height."""
    if len(profile) < 3:
        return [list(p) for p in profile]
    heights = [h for _, h in profile]
    smooth = [heights[0]] + [(a + b + c) / 3 for a, b, c in zip(heights, heights[1:], heights[2:])] + [heights[-1]]
    return [[k, h] for (k, _), h in zip(profile, smooth)]


def smooth(profile):
    return moving_average(resample(profile))


def climb_descent(heights, threshold=HYSTERESIS_M):
    """(climb, descent) in m, counting a change only once it reaches threshold from the last counted level."""
    if not heights:
        return 0.0, 0.0
    climb = descent = 0.0
    level = heights[0]
    for h in heights[1:]:
        if h - level >= threshold:
            climb += h - level
            level = h
        elif level - h >= threshold:
            descent += level - h
            level = h
    # the remainder below the threshold at the end still counts toward the true net change
    tail = heights[-1] - level
    if tail > 0:
        climb += tail
    else:
        descent -= tail
    return climb, descent


def height_at(profile, km):
    """Interpolated height at km along the profile (clamped to its ends)."""
    if not profile:
        return None
    if km <= profile[0][0]:
        return profile[0][1]
    for (k0, h0), (k1, h1) in zip(profile, profile[1:]):
        if k0 <= km <= k1:
            return h0 + (h1 - h0) * ((km - k0) / ((k1 - k0) or 1.0))
    return profile[-1][1]


def upto(profile, km):
    """The profile from the start to km (the last point interpolated at km)."""
    out = [list(p) for p in profile if p[0] < km]
    if not out:
        out = [list(profile[0])]
    out.append([km, height_at(profile, km)])
    return out


def stats(profile):
    """Smoothed {elevation_gain_m, elevation_loss_m, min_m, max_m} for a whole raw profile."""
    s = smooth(profile)
    heights = [h for _, h in s]
    climb, descent = climb_descent(heights)
    return {"elevation_gain_m": round(climb), "elevation_loss_m": round(descent),
            "min_m": round(min(heights)), "max_m": round(max(heights))}
