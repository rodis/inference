"""Geospatial primitives shared by the location engines.

Extracted from `engines/geofence.py` when a second consumer appeared (`stay_window`):
distance and the plausibility guard are properties of *location data*, not of either
strategy, and both engines must agree on them. Pure functions over floats — no state, no
transport, nothing engine-specific.
"""

import math

EARTH_RADIUS_M = 6_371_000.0

# Above this implied speed between consecutive fixes, treat the newer one as a bad fix
# rather than as movement. Chosen well above any ground travel (a car at 68 km/h was the
# fastest real reading; a train or motorway run is still far below) but low enough to catch
# a wifi-positioning snap. Real case that motivates it (2026-07-25 09:32): consecutive
# points 1s apart and 700m apart — implying ~2.5M km/h — where the newer fix claimed
# `acc: 5` while sitting on the phone's HOME coordinates as the car drove away. Reported
# accuracy is NOT a safety net, so containment and clustering both need this check.
DEFAULT_MAX_SPEED_KMH = 400.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def implied_speed_kmh(dist_m: float, dt_s: float) -> float:
    """Speed implied by covering `dist_m` in `dt_s`. Zero/negative dt -> infinite (any
    jump with no time between fixes is impossible), except a true zero-distance repeat.
    """
    if dist_m <= 0:
        return 0.0
    if dt_s <= 0:
        return float("inf")
    return dist_m / dt_s * 3.6


def is_implausible_jump(
    prev_lat: float, prev_lon: float, prev_ts: float,
    lat: float, lon: float, ts: float,
    max_speed_kmh: float = DEFAULT_MAX_SPEED_KMH,
) -> bool:
    """True if getting from the previous fix to this one would require impossible speed.

    Guards against a confidently-wrong fix (see DEFAULT_MAX_SPEED_KMH). Deliberately
    one-sided: it judges the NEW point against the last accepted one, so a single bad fix
    is skipped and the track continues from the last trustworthy position.
    """
    dist = haversine_m(prev_lat, prev_lon, lat, lon)
    return implied_speed_kmh(dist, ts - prev_ts) > max_speed_kmh
