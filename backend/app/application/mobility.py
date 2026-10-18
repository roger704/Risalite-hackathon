"""Read-only projections for fixed mobility counters."""

import json

import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.domain import ConnectorError


def station_layer(service, dataset_id: str, snapshot_id: str, window_id: str, year: int) -> dict:
    config = service._config(dataset_id)
    if config.adapter != "bicycle_counts":
        raise ConnectorError("INVALID_FILTER", "Counter layers require a mobility dataset")
    manifest = service.catalog.get(dataset_id, snapshot_id)
    table = pq.read_table(
        service._snapshot_path(manifest),
        columns=["record_id", "record_json"],
    )
    selected = table.filter(
        pc.and_(
            pc.starts_with(table["record_id"], f"window:{window_id}:"),
            pc.ends_with(table["record_id"], f":{year}"),
        )
    )
    selected_records = [
        json.loads(encoded) for encoded in selected.column("record_json").to_pylist()
    ]
    if not selected_records or any(
        record.get("record_type") != "mobility_observation" for record in selected_records
    ):
        raise ConnectorError(
            "INVALID_FILTER", "No counter records for window/year in this snapshot"
        )
    features = []
    for record in selected_records:
        expected = record["expected_intervals"]
        usable = record["observed_intervals"] + record["reconstructed_intervals"]
        features.append(
            {
                "type": "Feature",
                "geometry": record["geometry"],
                "properties": {
                    "location_id": record["location_id"],
                    "location_name": record["location_name"],
                    "direction": record["direction"],
                    "value": record["value"],
                    "unit": record["unit"],
                    "value_status": record["value_status"],
                    "coverage_ratio": usable / expected if expected else 0.0,
                    "expected_intervals": expected,
                    "observed_intervals": record["observed_intervals"],
                    "reconstructed_intervals": record["reconstructed_intervals"],
                    "implausible_intervals": record["implausible_intervals"],
                    "missing_intervals": record["missing_intervals"],
                    "quality_flags": record["quality_flags"],
                    "window_id": record["window_id"],
                    "window_label": record["attributes"]["label"],
                    "label": (
                        "Observed bicycle counts at this counter only — not footfall or customers"
                    ),
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "attribution": manifest.attribution,
        "warnings": list(manifest.quality_report.warnings),
        "features": features,
    }
