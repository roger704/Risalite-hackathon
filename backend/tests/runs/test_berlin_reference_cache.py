from app.application.area_data import (
    _REFERENCE_CACHE,
    _REFERENCE_CACHE_LOCK,
    SummaryAreaData,
)
from app.scoring import v2


def test_berlin_reference_is_cached_in_memory_and_on_disk(tmp_path):
    calls = []
    names = {"02100101": {"name": "PLR", "pgr_id": "0202", "pgr_name": "Kreuzberg"}}
    template = v2.Template(
        "reference_cache_test",
        (v2.LayerSpec("cafe_saturation", "Cafés", "-", 1.0),),
    )
    snapshots = {"berlin_lor": str(tmp_path)}

    def summary(plr, category="coffee_shop", include_geometry=True):
        calls.append((plr, category))
        return {
            "area": {"name": {"value": "PLR"}, "area_km2": {"value": 0.5}, "boundary": None},
            "residents": {
                "total": {"value": 4000},
                "density": {"value": 8000},
                "age_bands": [
                    {"age_min": 18, "age_max_exclusive": 27, "count": {"value": 1000}},
                    {"age_min": 27, "age_max_exclusive": 45, "count": {"value": 1000}},
                ],
            },
            "competitors": {
                "sources": [
                    {
                        "inventory": "mapped_places",
                        "count": {
                            "value": {
                                "coffee_shop": 4,
                                "bakery": 2,
                                "restaurant": 3,
                                "fast_food": 1,
                            }[category],
                            "sources": [{"dataset_id": "osm_pois", "snapshot_id": "osm-test"}],
                        },
                    },
                    {
                        "inventory": "registered_businesses",
                        "count": {"value": 2, "sources": []},
                    },
                ]
            },
            "transit": {
                "windows": [
                    {
                        "window_start": "07:00",
                        "window_end": "10:00",
                        "scheduled_departures": {"value": 1},
                    },
                    {
                        "window_start": "16:00",
                        "window_end": "19:00",
                        "scheduled_departures": {"value": 2},
                    },
                ]
            },
        }

    first = SummaryAreaData(
        summary,
        names,
        snapshot_ids=snapshots,
        reference_cache_dir=tmp_path,
    )
    [cell] = first.berlin_reference(template)
    assert cell.raw["cafe_saturation"] == 10.0
    assert len(calls) == 4
    [cache_path] = tmp_path.glob("*.json")

    def unavailable(*args, **kwargs):
        raise AssertionError("cached Berlin reference should avoid recomputation")

    memory_hit = SummaryAreaData(
        unavailable,
        names,
        snapshot_ids=snapshots,
        reference_cache_dir=tmp_path,
    )
    [memory_cell] = memory_hit.berlin_reference(template)
    assert memory_cell.raw == cell.raw
    assert len(calls) == 4

    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_CACHE.pop(cache_path.stem, None)
    second = SummaryAreaData(
        unavailable,
        names,
        snapshot_ids=snapshots,
        reference_cache_dir=tmp_path,
    )
    [cached_cell] = second.berlin_reference(template)
    assert cached_cell.raw == cell.raw
    assert len(calls) == 4
