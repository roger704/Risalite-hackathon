import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import httpx
import pytest
from pyproj import Transformer
from shapely.geometry import Point, box, mapping

from app.contracts import CellsDocument, Evidence
from app.scoring.areas import AreaNotFoundError, ResolvedArea, resolve_area
from app.scoring.build import build_cells
from app.scoring.layers import LayerResult, compute_layers
from app.scoring.locate import locate
from app.scoring.scoring import normalize, score_cells


def test_minmax_and_constant_normalization():
    normalized, method = normalize({"a": 10, "b": 20, "c": 30}, "minmax")
    assert normalized == {"a": 0, "b": 0.5, "c": 1}
    assert method == "minmax"
    assert normalize({"a": 7, "b": 7}, "minmax")[0] == {"a": 0.5, "b": 0.5}


def test_auto_uses_percentile_for_skew_and_minmax_for_symmetric():
    skewed, skewed_method = normalize(dict(zip("abcde", [1, 1, 1, 1, 100])))
    symmetric, symmetric_method = normalize(dict(zip("abcde", [1, 2, 3, 4, 5])))
    assert skewed_method == "percentile"
    assert skewed["e"] == 1
    assert symmetric_method == "minmax"
    assert symmetric["a"] == 0 and symmetric["e"] == 1


def test_negative_direction_inverts_competition_ranking():
    layers = [{"id": "competition", "direction": "-", "weight_default": 1, "required": True}]
    scored, _, _ = score_cells({"low": {"competition": 1}, "high": {"competition": 10}}, layers)
    assert scored["low"]["score_present"] > scored["high"]["score_present"]


def test_missing_required_excludes_before_other_layer_normalization():
    layers = [
        {"id": "demand_density", "direction": "+", "weight_default": 1, "required": True},
        {"id": "optional", "direction": "+", "weight_default": 1, "required": False},
    ]
    scored, excluded, _ = score_cells(
        {
            "a": {"demand_density": 1, "optional": 10},
            "b": {"demand_density": 2, "optional": 20},
            "c": {"demand_density": None, "optional": 10000},
        },
        layers,
        names={"c": "Suppressed"},
    )
    assert [item["id"] for item in excluded] == ["c"]
    assert (
        "missing required value: demand_density (population suppressed/missing)"
        in excluded[0]["reason"]
    )
    assert scored["a"]["values"]["optional"] == 0
    assert scored["b"]["values"]["optional"] == 1


def test_missing_optional_layer_is_listed_and_weights_renormalized():
    layers = [
        {"id": "a", "direction": "+", "weight_default": 0.25, "required": True},
        {"id": "b", "direction": "+", "weight_default": 0.75, "required": False},
    ]
    scored, excluded, _ = score_cells({"x": {"a": 1, "b": None}, "y": {"a": 2, "b": 3}}, layers)
    assert not excluded
    assert scored["x"]["missing_layers"] == ["b"]
    assert scored["x"]["values"] == {"a": 0}
    assert scored["x"]["score_present"] == 0
    assert scored["y"]["score_present"] == 1


def test_weights_change_ranking_and_zero_weight_is_unchecked():
    layers = [
        {"id": "need", "direction": "+", "weight_default": 0.5, "required": True},
        {"id": "competition", "direction": "-", "weight_default": 0.5, "required": True},
    ]
    values = {
        "A": {"need": 10, "competition": 10},
        "B": {"need": 1, "competition": 1},
    }
    balanced, _, _ = score_cells(values, layers, {"need": 1, "competition": 1})
    weighted, _, _ = score_cells(values, layers, {"need": 9, "competition": 1})
    assert balanced["A"]["score_present"] == balanced["B"]["score_present"]
    assert weighted["A"]["score_present"] > weighted["B"]["score_present"]
    unchecked, _, _ = score_cells(values, layers, {"need": 0, "competition": 1})
    assert all("need" not in cell["values"] for cell in unchecked.values())


def test_future_modifier_bonus_filters_and_clamps():
    layers = [{"id": "x", "direction": "+", "weight_default": 1, "required": True}]
    modifiers = {
        "a": [
            {"type": "future", "bonus": 0.1},
            {"type": "future", "bonus": 0.1, "accepted": False},
            {"type": "context", "bonus": 0.5},
        ],
        "b": [{"type": "future", "bonus": 0.8}],
    }
    scored, _, _ = score_cells({"a": {"x": 1}, "b": {"x": 2}}, layers, modifiers=modifiers)
    assert scored["a"]["score_future"] == 0.1
    assert scored["b"]["score_future"] == 1


def test_cells_contract_accepts_spec_shape_and_round_trips():
    example = {
        "area": {"name": "Friedrichshain-Kreuzberg", "level": "bezirk", "cell_count": 1},
        "mode_default": "present",
        "layers": [
            {
                "id": "demand_density",
                "label": "Population density",
                "direction": "+",
                "weight_default": 0.3,
                "source": "https://example.invalid/data",
                "evidence_id": "e12",
                "why": "Resident demand.",
                "units": "residents/km²",
                "method": "minmax",
            }
        ],
        "cells": [
            {
                "id": "02030301",
                "name": "Wrangelkiez",
                "geometry_ref": "cells.geojson#02030301",
                "values": {"demand_density": 0.82},
                "modifiers": [
                    {
                        "type": "future",
                        "label": "Planned housing",
                        "quote": "300 Wohnungen bis 2028",
                        "evidence_id": "e31",
                        "localization": "plr",
                        "bonus": 0.1,
                        "magnitude": {"units": 300},
                    }
                ],
                "score_present": 0.61,
                "score_future": 0.71,
                "raw_values": {"demand_density": 12000},
                "evidence_ids": {"demand_density": "e12"},
                "risks": [],
                "unknowns": [],
                "missing_layers": [],
            }
        ],
        "excluded_cells": [],
        "template": "coffee",
        "scoring_version": "coffee_v1",
    }
    validated = CellsDocument.model_validate(example)
    assert CellsDocument.model_validate(json.loads(validated.model_dump_json())) == validated


def test_build_cells_output_validates_and_round_trips():
    evidence = Evidence(
        id="s2-demand_density-12345678",
        method="snapshot",
        title="Population",
        url="https://example.invalid/population",
        quote="3 PLRs; density computed from total population and area",
        tier="official",
        confidence="high",
    )
    result = LayerResult(
        "demand_density",
        {"00000001": 100.0, "00000002": 200.0, "00000003": 300.0},
        "residents/km²",
        "calculated",
        evidence.url,
        "population-snapshot",
        evidence,
    )
    doc, evidence_list = build_cells(
        ResolvedArea("Test area", "bezirk", "bez", ["00000001", "00000002", "00000003"]),
        [result],
        None,
        [],
        {},
        {},
        config={
            "version": "test_v1",
            "future_modifier_bonus": 0.1,
            "skew_threshold": 1,
            "layers": [
                {
                    "id": "demand_density",
                    "label": "Density",
                    "direction": "+",
                    "weight_default": 1,
                    "required": True,
                    "normalization": "minmax",
                    "why": "Test layer",
                }
            ],
            "_geometry_source_url": "https://example.invalid/lor",
            "_geometry_snapshot_id": "names-snapshot",
            "_geometry_geometry_snapshot_id": "geometry-snapshot",
        },
        names={key: {"name": key} for key in ("00000001", "00000002", "00000003")},
        template="coffee",
    )
    assert len(evidence_list) == 2
    restored = CellsDocument.model_validate_json(doc.model_dump_json())
    assert restored == doc
    assert [cell.id for cell in doc.cells] == ["00000003", "00000002", "00000001"]


def test_resolve_area_exact_prefix_city_and_unknown():
    names = {
        "02030101": {
            "name": "Kreuzberg Nord",
            "bzr_name": "Nördliche Luisenstadt",
            "pgr_name": "Kreuzberg Nord",
            "bez": "02 - Friedrichshain-Kreuzberg",
        },
        "02030102": {
            "name": "Kreuzberg Süd",
            "bzr_name": "Südliche Friedrichstadt",
            "pgr_name": "Kreuzberg Süd",
            "bez": "02 - Friedrichshain-Kreuzberg",
        },
        "02030201": {
            "name": "Friedrichshain West",
            "bzr_name": "Frankfurter Allee Nord",
            "pgr_name": "Friedrichshain West",
            "bez": "02 - Friedrichshain-Kreuzberg",
        },
    }
    assert resolve_area("Friedrichshain-Kreuzberg", names).level == "bezirk"
    prefix = resolve_area("Kreuzberg", names)
    assert prefix.level == "ortsteil_approx"
    assert prefix.plr_ids == ["02030101", "02030102"]
    assert resolve_area(None, names).plr_ids == sorted(names)
    assert resolve_area("02030101", names).plr_ids == ["02030101"]
    with pytest.raises(AreaNotFoundError, match="Suggestions"):
        resolve_area("not an area", names)


def _record(**values):
    return SimpleNamespace(**values)


def test_transit_clusters_adjacent_stops_and_applies_400m_buffer():
    geometry = gpd.GeoDataFrame(
        {"id": ["00000001"]},
        geometry=[box(0, 0, 20, 20)],
        crs=25833,
    )
    stops = [
        _record(stop_id="in", geometry=mapping(Point(370, 10)), record_type="transit_stop"),
        _record(stop_id="out", geometry=mapping(Point(470, 10)), record_type="transit_stop"),
        _record(stop_id="far", geometry=mapping(Point(1000, 10)), record_type="transit_stop"),
    ]
    services = [
        _record(
            stop_id=stop_id,
            window_start="07:00:00",
            window_end="10:00:00",
            departures=departures,
            record_type="stop_service",
        )
        for stop_id, departures in (("in", 10), ("out", 20), ("far", 30))
    ]
    config = {
        "layers": [{"id": "transit"}],
        "categories": {},
    }
    fake_service = SimpleNamespace(
        configs={
            "vbb_gtfs": SimpleNamespace(
                resource_url="https://vbb.example/gtfs",
                reference_period=None,
                parser_options={"service_date": "2026-11-10"},
            )
        }
    )
    manifest = SimpleNamespace(
        snapshot_id="gtfs-test", retrieved_at=datetime.now(timezone.utc), reference_period=None
    )
    result = compute_layers(
        fake_service,
        ["00000001"],
        config,
        records={"vbb_gtfs": (manifest, stops + services)},
        geometries=geometry,
    )
    assert result[0].values == {"00000001": 30.0}
    assert "scheduled supply, not footfall" in result[0].evidence.quote


def test_population_and_competition_layers_use_synthetic_plr_records():
    ids = ["00000001", "00000002", "00000003", "00000004"]
    polygons = gpd.GeoDataFrame(
        {"id": ids},
        geometry=[box(index * 1100, 0, index * 1100 + 1000, 1000) for index in range(4)],
        crs=25833,
    )
    population = []
    for index, plr_id in enumerate(ids):
        total = None if index == 3 else (index + 1) * 1000
        population.extend(
            [
                _record(
                    area_code=plr_id,
                    metric_code="population_total",
                    age_min=None,
                    age_max_exclusive=None,
                    value=total,
                ),
                _record(
                    area_code=plr_id,
                    metric_code="population_age_band",
                    age_min=18,
                    age_max_exclusive=27,
                    value=200 if total is not None else None,
                ),
                _record(
                    area_code=plr_id,
                    metric_code="population_age_band",
                    age_min=27,
                    age_max_exclusive=45,
                    value=300 if total is not None else None,
                ),
            ]
        )
    transformer = Transformer.from_crs(25833, 4326, always_xy=True)
    lon, lat = transformer.transform(500, 500)
    osm = [
        _record(
            record_id="osm-1",
            canonical_category="coffee_shop",
            geometry={"type": "Point", "coordinates": [lon, lat]},
        ),
        _record(
            record_id="osm-ignored",
            canonical_category="restaurant",
            geometry={"type": "Point", "coordinates": [lon, lat]},
        ),
    ]
    ihk = [
        _record(record_id="ihk-1", canonical_category="coffee_shop", area_code=ids[1]),
        _record(record_id="ihk-unassigned", canonical_category="coffee_shop", area_code=None),
    ]
    snapshots = {
        dataset_id: (
            SimpleNamespace(
                snapshot_id=f"{dataset_id}-test",
                retrieved_at=datetime.now(timezone.utc),
                reference_period="2026-06-30",
            ),
            dataset_records,
        )
        for dataset_id, dataset_records in (
            ("berlin_population", population),
            ("osm_pois", osm),
            ("ihk_businesses", ihk),
        )
    }
    service = SimpleNamespace(
        configs={
            dataset_id: SimpleNamespace(
                resource_url=f"https://example.invalid/{dataset_id}",
                reference_period="2026-06-30",
            )
            for dataset_id in snapshots
        }
    )
    config = {
        "layers": [
            {"id": "demand_density"},
            {"id": "young_adult_share"},
            {"id": "competition_osm"},
            {"id": "competition_ihk"},
        ],
        "categories": {"osm": ["coffee_shop"], "ihk": ["coffee_shop"]},
    }
    layers = compute_layers(
        service,
        ids,
        config,
        records=snapshots,
        geometries=polygons,
    )
    by_id = {layer.layer_id: layer for layer in layers}
    assert by_id["demand_density"].values[ids[0]] == 1000
    assert by_id["demand_density"].values[ids[3]] is None
    assert by_id["young_adult_share"].values[ids[0]] == 0.5
    assert by_id["competition_osm"].values[ids[0]] == 1
    assert by_id["competition_ihk"].values[ids[1]] == 1
    assert by_id["competition_ihk"].evidence.details["unassigned"] == 1


class _Catalog:
    def __init__(self, manifests):
        self.manifests = manifests

    def get(self, dataset_id, snapshot_id):
        return self.manifests[dataset_id]


class _FakeService:
    def __init__(self, names, osm, ihk):
        self.manifests = {
            dataset_id: SimpleNamespace(dataset_id=dataset_id, snapshot_id=f"{dataset_id}-snapshot")
            for dataset_id in ("berlin_lor_names", "osm_pois", "ihk_businesses")
        }
        self.catalog = _Catalog(self.manifests)
        self.records = {
            "berlin_lor_names": names,
            "osm_pois": osm,
            "ihk_businesses": ihk,
        }
        self.configs = {
            dataset_id: SimpleNamespace(resource_url=f"https://example.invalid/{dataset_id}")
            for dataset_id in self.records
        }

    def list_datasets(self):
        return [
            {"dataset_id": key, "latest_snapshot_id": value.snapshot_id}
            for key, value in self.manifests.items()
        ]

    def _load_records(self, manifest):
        return self.records[manifest.dataset_id]


@pytest.mark.asyncio
async def test_locate_mock_wfs_separates_nearby_osm_and_ihk(monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures" / "locate" / "address.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    transformer = Transformer.from_crs(25833, 4326, always_xy=True)

    def point_record(identifier, category, x_offset, name):
        lon, lat = transformer.transform(392000 + x_offset, 5810000)
        return _record(
            record_id=identifier,
            canonical_category=category,
            name=name,
            geometry={"type": "Point", "coordinates": [lon, lat]},
        )

    names = [
        _record(
            area_code="02030301",
            attributes={
                "bez": "02 - Friedrichshain-Kreuzberg",
                "pgr_name": "Kreuzberg Nord",
                "bzr_name": "Wrangelkiez",
            },
            name="Wrangelkiez",
        )
    ]
    service = _FakeService(
        names,
        [
            point_record("osm-near", "coffee_shop", 100, "OSM Cafe"),
            point_record("osm-far", "coffee_shop", 700, "Far OSM Cafe"),
        ],
        [point_record("ihk-near", "coffee_shop", 200, "IHK Cafe")],
    )
    evidence = Evidence(
        id="layer",
        method="snapshot",
        title="layer",
        url="https://example.invalid",
        quote="layer",
        tier="official",
        confidence="high",
    )
    monkeypatch.setattr(
        "app.scoring.locate.compute_layers",
        lambda *_args, **_kwargs: [
            LayerResult(
                "demand_density",
                {"02030301": 10000.0},
                "residents/km²",
                "calculated",
                "https://example.invalid/pop",
                "population-snapshot",
                evidence,
            )
        ],
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await locate(
            "Oranienstraße 12a, 10999 Berlin",
            service=service,
            http=client,
            categories={
                "layers": [{"id": "demand_density", "normalization": "minmax"}],
                "categories": {"osm": ["coffee_shop"], "ihk": ["coffee_shop"]},
            },
        )
    assert result.plr_id == "02030301"
    assert [item["record_id"] for item in result.osm] == ["osm-near"]
    assert [item["record_id"] for item in result.ihk] == ["ihk-near"]
    assert result.values["demand_density"] == 0.5
    assert result.evidence[0]["method"] == "wfs"
