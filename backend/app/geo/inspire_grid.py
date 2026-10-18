"""Geometry helpers for INSPIRE equal-area grid identifiers."""

import re
from dataclasses import dataclass
from functools import lru_cache

from pyproj import CRS, Transformer
from shapely.geometry import Polygon


@dataclass(frozen=True)
class GridCell:
    resolution_m: int
    north: int
    east: int

    def __post_init__(self) -> None:
        if (
            type(self.resolution_m) is not int
            or self.resolution_m <= 0
            or type(self.north) is not int
            or type(self.east) is not int
            or self.north % self.resolution_m
            or self.east % self.resolution_m
        ):
            raise ValueError("Grid cell coordinates must be integer resolution multiples")

    @property
    def grid_id(self) -> str:
        return f"CRS3035RES{self.resolution_m}mN{self.north}E{self.east}"


def parse_grid_id(text: str) -> GridCell:
    match = re.fullmatch(r"CRS3035RES(\d+)mN(\d+)E(\d+)", text) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"Invalid INSPIRE grid ID: {text!r}")
    resolution_m, north, east = map(int, match.groups())
    if resolution_m <= 0 or north % resolution_m or east % resolution_m:
        raise ValueError(f"Invalid INSPIRE grid ID: {text!r}")
    return GridCell(resolution_m=resolution_m, north=north, east=east)


def cell_polygon_3035(cell: GridCell) -> Polygon:
    east, north, resolution = cell.east, cell.north, cell.resolution_m
    return Polygon(
        [
            (east, north),
            (east + resolution, north),
            (east + resolution, north + resolution),
            (east, north + resolution),
            (east, north),
        ]
    )


@lru_cache(maxsize=16)
def _transformer(crs: str) -> Transformer:
    return Transformer.from_crs("EPSG:3035", crs, always_xy=True)


def cell_polygon(cell: GridCell, crs: str) -> Polygon:
    if CRS.from_user_input(crs) == CRS.from_epsg(3035):
        return cell_polygon_3035(cell)
    source = cell_polygon_3035(cell)
    transformer = _transformer(crs)
    return Polygon([transformer.transform(x, y) for x, y in source.exterior.coords])


def cell_geojson_wgs84(cell: GridCell) -> dict:
    ring = [list(point) for point in cell_polygon(cell, "EPSG:4326").exterior.coords]
    return {"type": "Polygon", "coordinates": [ring]}
