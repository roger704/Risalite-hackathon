"""Snapshot integration for Berlin environment indicators and their API legend."""

import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_environment import BerlinEnvironmentConnector
from app.connectors.lor import LORConnector
from app.connectors.registry import load_connector
from app.domain import IncompatibleSnapshotError, SchemaError
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_environment"


def service_setup(tmp_path, parent_codes=("01100101", "03400831", "06200418")):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(source_crs="EPSG:4326")
    lor["parser_options"].update(
        code_field="PLR_ID",
        name_field="PLR_NAME",
        expected_rows=3,
    )
    environment = json.loads((BACKEND / "config/datasets/berlin_environment.json").read_text())
    environment["parser_options"].update(
        page_size=2,
        max_pages=2,
        expected_count=3,
    )
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_environment.json").write_text(json.dumps(environment))

    source = json.loads((BACKEND / "tests/fixtures/lor/areas.geojson").read_text())
    features = []
    for index, code in enumerate(parent_codes):
        feature = json.loads(json.dumps(source["features"][index % 2]))
        feature["properties"]["PLR_ID"] = code
        feature["properties"]["PLR_NAME"] = f"Synthetic {code}"
        features.append(feature)
    parent_path = tmp_path / "parent.geojson"
    parent_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    state = {"fail": False}

    async def handler(request):
        if state["fail"]:
            exception = (BACKEND / "tests/fixtures/berlin_wfs/exception_report.xml").read_bytes()
            return httpx.Response(200, content=exception, request=request)
        layer_name = request.url.params["TYPENAMES"].split(":")[-1]
        collection = json.loads((FIXTURES / f"{layer_name}.json").read_text())
        start = int(request.url.params["STARTINDEX"])
        count = int(request.url.params["COUNT"])
        collection["features"] = collection["features"][start : start + count]
        collection["numberMatched"] = 3
        collection["numberReturned"] = len(collection["features"])
        if start + count < 3:
            query = [
                (key, value)
                for key, value in request.url.params.multi_items()
                if key.upper() not in {"COUNT", "STARTINDEX"}
            ]
            query.extend((("COUNT", str(count)), ("STARTINDEX", str(start + count))))
            collection["links"] = [
                {
                    "rel": "next",
                    "href": urlunsplit(
                        (
                            urlsplit(str(request.url)).scheme,
                            urlsplit(str(request.url)).netloc,
                            urlsplit(str(request.url)).path,
                            urlencode(query),
                            "",
                        )
                    ),
                }
            ]
        else:
            collection["links"] = []
        return httpx.Response(200, json=collection, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={
            "lor": LORConnector(),
            "berlin_environment": BerlinEnvironmentConnector(HTTPFetcher(client)),
        },
    )
    return service, parent_path, client, state, config_dir


@pytest.mark.asyncio
async def test_ingests_mixed_records_checks_parent_and_serves_legend(tmp_path):
    service, parent_path, client, state, config_dir = service_setup(tmp_path)
    empty_legend = service.legend("berlin_environment")
    assert empty_legend["latest_snapshot_id"] is None
    assert empty_legend["active_snapshot_id"] is None
    assert empty_legend["snapshot_config_matches"] is None
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_path)
        environment = await service.ingest(
            "berlin_environment", parent_snapshot_id=parent.snapshot_id
        )
        assert environment.parent_snapshot_ids == (parent.snapshot_id,)
        assert environment.coverage["parent_code_match"] == "exact"
        assert environment.coverage["parent_area_count"] == 3
        assert environment.coverage["join_method"] == "exact planungsraum code"
        assert environment.coverage["geometry_comparison"]["crs"] == "EPSG:25833"
        assert environment.coverage["geometry_comparison"]["metric"] == (
            "symmetric_difference_area / parent_area"
        )
        assert environment.coverage["record_count"] == 30
        assert (
            len(
                service.records("berlin_environment", environment.snapshot_id, limit=100)["records"]
            )
            == 30
        )

        with TestClient(create_app(service=service)) as api:
            response = api.get("/api/v1/datasets/berlin_environment/legend")
            assert response.status_code == 200
            legend = response.json()
            assert legend["latest_snapshot_id"] == environment.snapshot_id
            assert legend["active_snapshot_id"] == environment.snapshot_id
            assert legend["snapshot_config_matches"] is True
            assert len(legend["indicators"]) == 9
            assert legend["indicators"][0]["indicator_code"] == "noise_burden"
            assert legend["indicators"][0]["missing"]["null"]["color"] == "#C0C0C0"
            assert api.get("/api/v1/datasets/berlin_lor/legend").status_code == 404

        config_path = config_dir / "berlin_environment.json"
        changed_config = json.loads(config_path.read_text())
        noise_indicator = next(
            indicator
            for layer in changed_config["parser_options"]["layers"]
            for indicator in layer.get("indicators", [])
            if indicator["indicator_code"] == "noise_burden"
        )
        noise_indicator["classes"][0]["color"] = "#FFFFFF"
        config_path.write_text(json.dumps(changed_config))
        changed_service = DataService(service.root, config_dir)
        with TestClient(create_app(service=changed_service)) as api:
            changed_legend = api.get("/api/v1/datasets/berlin_environment/legend").json()
            assert changed_legend["latest_snapshot_id"] is None
            assert changed_legend["active_snapshot_id"] == environment.snapshot_id
            assert changed_legend["snapshot_config_matches"] is False
            assert changed_legend["indicators"][0]["classes"][0]["color"] == "#FFFFFF"

        state["fail"] = True
        with pytest.raises(SchemaError, match="OGC ExceptionReport"):
            await service.ingest("berlin_environment", parent_snapshot_id=parent.snapshot_id)
        assert service.catalog.get("berlin_environment").snapshot_id == environment.snapshot_id
        assert (
            len(
                service.records("berlin_environment", environment.snapshot_id, limit=100)["records"]
            )
            == 30
        )


@pytest.mark.asyncio
async def test_parent_code_mismatch_rejects_environment_snapshot(tmp_path):
    service, parent_path, client, _, _ = service_setup(
        tmp_path,
        parent_codes=("01100101", "03400831", "09999999"),
    )
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_path)
        with pytest.raises(IncompatibleSnapshotError, match="coverage differs"):
            await service.ingest("berlin_environment", parent_snapshot_id=parent.snapshot_id)


def test_environment_adapter_is_registered():
    connector = load_connector("berlin_environment")
    assert isinstance(connector, BerlinEnvironmentConnector)
