"""Zensus grid publication, parent compatibility, CLI access and rollback."""

import json
from pathlib import Path

import pytest

from app.application.service import DataService
from app.cli.main import main
from app.connectors.lor import LORConnector
from app.connectors.zensus import ZensusGridConnector
from app.domain import SchemaError

BACKEND = Path(__file__).parents[2]
FIXTURE = BACKEND / "tests/fixtures"


def create_service(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic-lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    zensus = json.loads((BACKEND / "config/datasets/zensus_grid.json").read_text())
    zensus["parser_options"].update(
        source_bbox=[4548000, 3270500, 4551000, 3272500],
        edge_buffer_m=100,
        min_expected_cells=1,
        expected_sha256=None,
        sentinels={"–": "zero", "/": "missing"},
        encoding="utf-8",
        official_total={"area": "synthetic Berlin", "value": 1000},
    )
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "zensus_grid.json").write_text(json.dumps(zensus))

    parent_source = FIXTURE / "lor/areas.geojson"
    source_path = tmp_path / "parent.geojson"
    source_path.write_bytes(parent_source.read_bytes())
    data_dir = tmp_path / "data"
    service = DataService(
        data_dir,
        config_dir,
        connectors={
            "lor": LORConnector(),
            "zensus_grid": ZensusGridConnector(),
        },
    )
    return service, config_dir, source_path, data_dir


def make_zip(tmp_path):
    import io
    import zipfile

    from openpyxl import Workbook

    fixture = FIXTURE / "zensus/population.csv"
    workbook = Workbook()
    sheet = workbook.active
    for row, text in enumerate(
        ("Version 2: 30.09.2024", "Stichtag 15. Mai 2022", "dl-de/by-2-0"),
        1,
    ):
        sheet.cell(row=row, column=1, value=text)
    description = io.BytesIO()
    workbook.save(description)
    workbook.close()
    path = tmp_path / "zensus.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Zensus2022_Bevoelkerungszahl_100m-Gitter.csv",
            fixture.read_bytes(),
        )
        archive.writestr(
            "Datensatzbeschreibung_Bevoelkerungszahl_Gitterzellen.xlsx",
            description.getvalue(),
        )
    return path


async def test_ingests_zensus_against_lor_supports_catchment_cli_and_rolls_back(tmp_path, capsys):
    service, config_dir, parent_file, data_dir = create_service(tmp_path)
    parent = await service.ingest("berlin_lor", local_file=parent_file)
    source = make_zip(tmp_path)
    manifest = await service.ingest("zensus_grid", local_file=source)

    assert manifest.reference_period == "2022-05-15"
    assert manifest.parent_snapshot_ids == (parent.snapshot_id,)
    assert "berlin_comparison" in manifest.coverage
    page = service.records("zensus_grid", manifest.snapshot_id, limit=100)
    assert page["records"]
    assert page["records"][0]["record_type"] == "grid_observation"

    exit_code = main(
        [
            "--data-dir",
            str(data_dir),
            "--config-dir",
            str(config_dir),
            "catchment",
            "zensus_grid",
            "--snapshot",
            manifest.snapshot_id,
            "--lon",
            "13.355",
            "--lat",
            "52.505",
            "--radius-m",
            "500",
        ]
    )
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["intersected_estimate"]["value_status"] == "estimated"
    assert result["reference_period"] == "2022-05-15"

    corrupted = tmp_path / "corrupt.zip"
    corrupted.write_bytes(b"not a valid ZIP")
    with pytest.raises(SchemaError):
        await service.ingest("zensus_grid", local_file=corrupted)
    latest = service.catalog.get("zensus_grid")
    assert latest.snapshot_id == manifest.snapshot_id
    assert service.records("zensus_grid", latest.snapshot_id, limit=100)["records"]
