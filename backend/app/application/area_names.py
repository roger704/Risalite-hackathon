"""Pinned lookups for official area names and source attributes."""

from app.application.service import DataService


def area_names(
    service: DataService, snapshot_id: str, dataset_id: str = "berlin_lor_names"
) -> dict[str, dict[str, str]]:
    result = {}
    cursor = None
    while True:
        page = service.records(dataset_id, snapshot_id, limit=1000, cursor=cursor)
        for record in page["records"]:
            if record.get("record_type") == "area":
                result[record["area_code"]] = {
                    "name": record["name"],
                    **record.get("attributes", {}),
                }
        cursor = page["next_cursor"]
        if cursor is None:
            return result
