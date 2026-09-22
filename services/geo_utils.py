"""Shared great-circle distance helper. No geo library is already a
dependency of this codebase, and pulling one in for a single haversine
calculation (used by the transport arrival-geofence check in
routers/security.py::post_bus_location) would be overkill."""
import math

EARTH_RADIUS_METERS = 6371000.0


def haversine_distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))
