"""Spatial queries for plans; radius membership uses distance, shares use a 64-segment disc."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Collection, Iterable

from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.strtree import STRtree

from app.domain import Area, DevelopmentPlan

PLAN_INTERPRETATION_GUARD = (
    "A development plan intersecting or near a location is evidence of a planning procedure only. "
    "It is not a claim about building completion, future residents, rental availability or "
    "permitted premises use."
)
EXCLUDED_CLAIMS = (
    "building_completion",
    "future_residents",
    "rental_availability",
    "permitted_premises_use",
)


@dataclass(frozen=True)
class PlanMatch:
    plan: DevelopmentPlan
    intersection_area_m2: float
    plan_area_share: float
    query_area_share: float
    distance_m: float


class DevelopmentPlanIndex:
    def __init__(self, plans: Iterable[DevelopmentPlan]):
        self._plans = tuple(plans)
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        self._geometries = tuple(
            transform(transformer.transform, shape(plan.geometry)) for plan in self._plans
        )
        self._plans_by_id = {plan.plan_id: plan for plan in self._plans}
        self._tree = STRtree(self._geometries) if self._geometries else None

    def get_plan(self, plan_id: str) -> DevelopmentPlan | None:
        return self._plans_by_id.get(plan_id)

    def intersecting_area(
        self,
        area: Area | BaseGeometry,
        statuses: Collection[str] | None = None,
    ) -> list[PlanMatch]:
        if isinstance(area, Area):
            transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
            query_geometry = transform(transformer.transform, shape(area.geometry))
        else:
            query_geometry = area
        return self._matches(query_geometry, statuses)

    def within_radius(
        self,
        x: float,
        y: float,
        radius_m: float,
        statuses: Collection[str] | None = None,
    ) -> list[PlanMatch]:
        if isinstance(radius_m, bool) or not isinstance(radius_m, (int, float)):
            raise ValueError("radius_m must be greater than zero and no more than 5000")
        if not 0 < radius_m <= 5000:
            raise ValueError("radius_m must be greater than zero and no more than 5000")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise ValueError("x and y must be finite EPSG:25833 coordinates")
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("x and y must be finite EPSG:25833 coordinates")
        point = Point(x, y)
        if not point.is_valid or point.is_empty:
            raise ValueError("x and y must be finite EPSG:25833 coordinates")
        query_geometry = point.buffer(radius_m, quad_segs=64)
        return self._matches(
            query_geometry,
            statuses,
            distance_from=point,
            radius_m=radius_m,
        )

    def _matches(
        self,
        query_geometry: BaseGeometry,
        statuses: Collection[str] | None,
        distance_from: BaseGeometry | None = None,
        radius_m: float | None = None,
    ) -> list[PlanMatch]:
        if query_geometry.is_empty or not query_geometry.is_valid or query_geometry.area <= 0:
            return []
        if self._tree is None:
            return []
        allowed_statuses = set(statuses) if statuses is not None else None
        matches = []
        query_area = query_geometry.area
        candidates = (
            self._tree.query(distance_from, predicate="dwithin", distance=radius_m)
            if distance_from is not None
            else self._tree.query(query_geometry)
        )
        for index in candidates:
            index = int(index)
            plan = self._plans[index]
            if allowed_statuses is not None and plan.status not in allowed_statuses:
                continue
            geometry = self._geometries[index]
            intersection_area = geometry.intersection(query_geometry).area
            distance = 0.0
            if distance_from is None:
                if intersection_area <= 0:
                    continue
            else:
                distance = geometry.distance(distance_from)
                if distance > radius_m:
                    continue
            matches.append(
                PlanMatch(
                    plan=plan,
                    intersection_area_m2=intersection_area,
                    plan_area_share=min(1.0, max(0.0, intersection_area / geometry.area)),
                    query_area_share=min(1.0, max(0.0, intersection_area / query_area)),
                    distance_m=distance,
                )
            )
        return sorted(
            matches,
            key=lambda match: (-match.intersection_area_m2, match.plan.plan_id),
        )
