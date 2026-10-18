"""Metric distance helpers for geographic coordinates."""

from pyproj import Transformer

from app.domain.routing import Coordinate

_TO_EPSG_25833 = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


def straight_line_distance_m(a: Coordinate, b: Coordinate) -> float:
    """Return projected Euclidean distance in metres between two WGS84 points."""
    ax, ay = _TO_EPSG_25833.transform(a.lon, a.lat)
    bx, by = _TO_EPSG_25833.transform(b.lon, b.lat)
    return ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
