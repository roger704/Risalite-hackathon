"""Typed records in Parquet; boundary geometry additionally uses GeoParquet metadata."""

import json
from pathlib import Path

import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import shape

from app.domain import NormalizedRecord, record_from_dict, record_to_dict


def write_records(records: tuple[NormalizedRecord, ...], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "record_id": item.record_id,
            "record_type": item.record_type,
            "area_code": getattr(item, "area_code", None),
            "record_json": json.dumps(record_to_dict(item), sort_keys=True, ensure_ascii=False),
        }
        for item in records
    ]
    if records and all(hasattr(item, "geometry") for item in records):
        frame = gpd.GeoDataFrame(
            rows, geometry=[shape(item.geometry) for item in records], crs="EPSG:4326"
        )
        frame.to_parquet(target, index=False)
    else:
        schema = pa.schema(
            [
                (name, pa.string())
                for name in (
                    "record_id",
                    "record_type",
                    "area_code",
                    "record_json",
                )
            ]
        )
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), target)


def read_records(path: Path) -> tuple[NormalizedRecord, ...]:
    table = pq.read_table(path, columns=["record_json"])
    return tuple(
        record_from_dict(json.loads(value)) for value in table.column("record_json").to_pylist()
    )
