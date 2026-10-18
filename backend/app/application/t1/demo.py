"""Synthetic in-memory T1 readers for offline runs (``RISALITE_T1_READERS=static``).

Not real Berlin data: one Kreuzberg address (Wiener Straße 12) with a few made-up POIs,
residents and departures so the T1 path can run end to end without snapshots or network.
Every source is labelled ``*-fixture`` so evidence entries are recognisable as such.
"""

from __future__ import annotations

import math

from app.application.t1.readers import Poi, ReferencePlr, StaticReaders
from app.domain import Address, record_to_dict

HOME = (13.4318, 52.4986)  # Wiener Straße 12, 10999 Berlin (approximate)
PLR_ID = "02040501"
PLR_NAME = "Wrangelkiez"


def _offset(dx_m: float, dy_m: float) -> tuple[float, float]:
    lon, lat = HOME
    return lon + dx_m / (111_320 * math.cos(math.radians(lat))), lat + dy_m / 110_540


def _square(d: float = 0.01) -> dict:
    lon, lat = HOME
    ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d]]
    return {"type": "Polygon", "coordinates": [[*ring, ring[0]]]}


def _address(record_id: str, number: int) -> dict:
    lon, lat = _offset(15.0 * (number - 12), 0.0)
    return record_to_dict(
        Address(
            source_id="berlin_addresses",
            snapshot_id="berlin_addresses-fixture",
            source_record_id=record_id,
            record_id=f"address:{record_id}",
            geometry={"type": "Point", "coordinates": [lon, lat]},
            street_name="Wiener Straße",
            address_type="H",
            house_number=number,
            postcode="10999",
            district_code="02",
            district_name="Friedrichshain-Kreuzberg",
            area_code=PLR_ID,
            area_name=PLR_NAME,
        )
    )


def _reference() -> list[ReferencePlr]:
    return [
        ReferencePlr(
            plr_id=f"ref{i:02d}",
            residents=2500 + 500 * i,
            area_km2=1.0,
            counts={
                "coffee_shop": i % 7,
                "restaurant": i % 9,
                "fast_food": i % 5,
                "bakery": i % 4,
                "coworking": i % 2,
            },
            young_adult_share=0.3 + 0.01 * i,
            transit={"morning": 100.0 * i, "evening": 80.0 * i},
        )
        for i in range(20)
    ]


def demo_readers() -> StaticReaders:
    near = [
        _offset(dx, dy) for dx, dy in ((100, 0), (-150, 80), (0, 200), (300, -100), (-80, -120))
    ]
    sources = {
        role: {
            "dataset_id": dataset,
            "snapshot_id": f"{dataset}-fixture",
            "source_url": "https://example.org/risalite-fixture",
        }
        for role, dataset in {
            "addresses": "berlin_addresses",
            "boundary": "berlin_lor",
            "census": "zensus_grid",
            "places": "osm_pois",
            "transit": "vbb_gtfs",
        }.items()
    }
    return StaticReaders(
        addresses=[_address("wiener-12", 12), _address("wiener-14", 14)],
        plrs=[
            {
                "plr_id": PLR_ID,
                "name": PLR_NAME,
                "geometry": _square(),
                "young_adult_share": 0.45,
            }
        ],
        residents=[(*HOME, 9000.0), (*_offset(200, 200), 3000.0)],
        places=[
            Poi("osm:c1", "coffee_shop", *near[0], "Café Eins"),
            Poi("osm:c2", "coffee_shop", *near[1], "Café Zwei"),
            Poi("osm:r1", "restaurant", *near[2], "Lokal"),
            Poi("osm:b1", "bakery", *near[3], "Bäcker"),
            Poi("osm:k1", "hairdresser", *near[4], "Salon"),
        ],
        stations=[
            {"lon": near[0][0], "lat": near[0][1], "departures": {"morning": 600, "evening": 500}}
        ],
        reference=_reference(),
        sources=sources,
    )
