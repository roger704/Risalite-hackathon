"""`t1_catalog_v1` business-type catalog (Deep_Research_Spec §6)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
CATALOG_ID = "t1_catalog_v1"
DRIVERS = frozenset({"residents", "young_adult_share", "transit", "centre_proximity"})


@dataclass(frozen=True)
class CatalogType:
    id: str
    label: str
    mapping: str
    competitors: tuple[str, ...]
    complements: tuple[str, ...]
    demand_drivers: tuple[str, ...]
    transit_windows: tuple[str, ...]
    space_checks: tuple[str, ...] = ()
    operator_checks: tuple[str, ...] = ()
    operator_keywords: tuple[str, ...] = ()
    competitor_tags: tuple[str, ...] = ()
    complement_tags: tuple[str, ...] = ()
    ihk_crosscheck: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalog:
    id: str
    category_mapping: str
    types: tuple[CatalogType, ...]
    mapped_categories: frozenset[str]
    catchment: dict[str, Any] = field(default_factory=dict)
    transit_windows: dict[str, tuple[str, str]] = field(default_factory=dict)
    dimension_weights: dict[str, float] = field(default_factory=dict)
    data_dimensions: tuple[str, ...] = ()
    weak_tier_cap: float = 6.0
    neutral_score: float = 5.0
    risk_penalty: dict[str, float] = field(default_factory=dict)

    def is_offered(self, item: CatalogType) -> bool:
        """Only `osm_v1` types whose categories exist in the installed mapping are offered."""
        return (
            item.mapping == self.category_mapping
            and bool(item.competitors)
            and set(item.competitors) <= self.mapped_categories
            and set(item.complements) <= self.mapped_categories
        )

    def offered(self) -> tuple[CatalogType, ...]:
        return tuple(item for item in self.types if self.is_offered(item))

    def get(self, type_id: str) -> CatalogType:
        for item in self.types:
            if item.id == type_id:
                return item
        raise KeyError(type_id)

    @property
    def min_denominator(self) -> int:
        return int(self.catchment.get("min_residents_denominator", 2000))

    @property
    def low_resident_threshold(self) -> int:
        return int(self.catchment.get("low_resident_threshold", 2000))


def mapped_categories(mapping: str) -> frozenset[str]:
    path = CONFIG_DIR / "categories" / f"{mapping}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(
        item["canonical_category"]
        for item in data.get("categories", [])
        if "canonical_category" in item
    )


def _type(raw: dict[str, Any]) -> CatalogType:
    drivers = tuple(raw.get("demand_drivers", ()))
    unknown = set(drivers) - DRIVERS
    if unknown:
        raise ValueError(f"{raw['id']}: unknown demand drivers {sorted(unknown)}")
    return CatalogType(
        id=raw["id"],
        label=raw["label"],
        mapping=raw["mapping"],
        competitors=tuple(raw["competitors"]["categories"]),
        complements=tuple(raw.get("complements", {}).get("categories", ())),
        demand_drivers=drivers,
        transit_windows=tuple(raw.get("transit_windows", ())),
        space_checks=tuple(raw.get("space_checks", ())),
        operator_checks=tuple(raw.get("operator_checks", ())),
        operator_keywords=tuple(word.casefold() for word in raw.get("operator_keywords", ())),
        competitor_tags=tuple(raw["competitors"].get("osm", ())),
        complement_tags=tuple(raw.get("complements", {}).get("osm", ())),
        ihk_crosscheck=tuple(raw.get("ihk_crosscheck", ())),
    )


def parse_catalog(data: dict[str, Any], categories: frozenset[str] | None = None) -> Catalog:
    mapping = data.get("category_mapping", "osm_v1")
    types = tuple(_type(raw) for raw in data["types"])
    ids = [item.id for item in types]
    if len(ids) != len(set(ids)):
        raise ValueError("catalog type ids must be unique")
    windows = {key: (value[0], value[1]) for key, value in data.get("transit_windows", {}).items()}
    for item in types:
        if set(item.transit_windows) - set(windows):
            raise ValueError(f"{item.id}: unknown transit window")
    return Catalog(
        id=data["template_id"],
        category_mapping=mapping,
        types=types,
        mapped_categories=categories if categories is not None else mapped_categories(mapping),
        catchment=dict(data.get("catchment", {})),
        transit_windows=windows,
        dimension_weights={k: float(v) for k, v in data.get("dimension_weights", {}).items()},
        data_dimensions=tuple(data.get("data_dimensions", ())),
        weak_tier_cap=float(data.get("weak_tier_cap", 6)),
        neutral_score=float(data.get("neutral_score", 5)),
        risk_penalty={k: float(v) for k, v in data.get("risk_penalty", {}).items()},
    )


@lru_cache(maxsize=4)
def load_catalog(catalog_id: str = CATALOG_ID) -> Catalog:
    path = CONFIG_DIR / "templates" / f"{catalog_id}.json"
    return parse_catalog(json.loads(path.read_text(encoding="utf-8")))
