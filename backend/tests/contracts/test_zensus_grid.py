"""Offline contracts for the pinned Zensus 2022 grid connector."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import unary_union

from app.connectors.zensus import ZensusGridConnector
from app.domain import (
    Area,
    AreaObservation,
    DatasetConfig,
    GridObservation,
    IncompatibleSnapshotError,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    record_from_dict,
    record_to_dict,
)
from app.geo.inspire_grid import (
    GridCell,
    cell_geojson_wgs84,
    cell_polygon,
    cell_polygon_3035,
    parse_grid_id,
)
from app.geo.zensus_catchment import aggregate_grid, circle_catchment

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/zensus"
LOREAS = BACKEND / "tests/fixtures/lor/areas.geojson"
CONFIG_PATH = BACKEND / "config/datasets/zensus_grid.json"
HEADER = ["GITTER_ID_100m", "x_mp_100m", "y_mp_100m", "Einwohner"]
DESCRIPTION_TEXTS = [
    "Version 2: 30.09.2024",
    "Stichtag 15. Mai 2022",
    "dl-de/by-2-0",
]


def configuration(**parser_changes):
    values = json.loads(CONFIG_PATH.read_text())
    options = values["parser_options"]
    options.update(
        source_bbox=[4548000, 3270500, 4551000, 3272500],
        edge_buffer_m=100,
        min_expected_cells=1,
        expected_sha256=None,
        sentinels={"–": "zero", "/": "missing"},
        official_total={"area": "synthetic", "value": 1000},
        encoding="utf-8",
    )
    options.update(parser_changes)
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    return DatasetConfig(**values)


def make_zip(
    tmp_path,
    *,
    rows=None,
    header=HEADER,
    description_texts=DESCRIPTION_TEXTS,
    extra_member=None,
):
    rows = (
        [line.split(";") for line in (FIXTURES / "population.csv").read_text().splitlines()[1:]]
        if rows is None
        else rows
    )
    csv_text = ";".join(header) + "\n" + "\n".join(";".join(row) for row in rows) + "\n"
    workbook = Workbook()
    sheet = workbook.active
    for row_number, text in enumerate(description_texts, 1):
        sheet.cell(row=row_number, column=1, value=text)
    description = io.BytesIO()
    workbook.save(description)
    workbook.close()
    path = tmp_path / f"zensus-{len(list(tmp_path.glob('zensus-*.zip')))}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(configuration().parser_options["archive_member"], csv_text.encode("utf-8"))
        archive.writestr(
            configuration().parser_options["description_member"], description.getvalue()
        )
        if extra_member:
            archive.writestr(extra_member[0], extra_member[1])
    return path


def bundle(path, snapshot_id="synthetic-zensus"):
    data = path.read_bytes()
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                resource_url="https://www.destatis.de/synthetic.zip",
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="synthetic",
    )


def normalized_fixture(tmp_path):
    return ZensusGridConnector().normalize(bundle(make_zip(tmp_path)), configuration())


def fixture_areas():
    source = json.loads(LOREAS.read_text())
    return tuple(
        Area(
            source_id="berlin_lor",
            snapshot_id="synthetic-lor",
            source_record_id=feature["properties"]["PLR_ID"],
            record_id=f"planungsraum:{feature['properties']['PLR_ID']}",
            geometry=feature["geometry"],
            area_code=feature["properties"]["PLR_ID"],
            name=feature["properties"]["PLR_NAME"],
            hierarchy="planungsraum",
            boundary_edition="2021",
        )
        for feature in source["features"]
    )


def grid_observation(cell, value, *, status="observed", period="2022-05-15"):
    return GridObservation(
        source_id="zensus_grid",
        snapshot_id="synthetic",
        source_record_id=cell.grid_id,
        record_id=f"{cell.grid_id}:population_census:{period}",
        geometry=cell_geojson_wgs84(cell),
        grid_id=cell.grid_id,
        grid_crs="EPSG:3035",
        resolution_m=cell.resolution_m,
        metric_code="population_census",
        value=value,
        unit="persons",
        reference_period=period,
        value_status=status,
    )


def test_grid_id_parsing_and_formatting():
    cell = parse_grid_id("CRS3035RES100mN2689100E4337000")
    assert cell == GridCell(resolution_m=100, north=2689100, east=4337000)
    assert cell.grid_id == "CRS3035RES100mN2689100E4337000"
    for invalid in (
        "CRS4326RES100mN2689100E4337000",
        "CRS3035RES100mN2689101E4337000",
        "CRS3035RES100mN2689100E4337001",
        "CRS3035RES0mN2689100E4337000",
    ):
        with pytest.raises(ValueError):
            parse_grid_id(invalid)


def test_grid_crs_transform_and_closed_wgs84_geometry():
    origin = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True).transform(
        4321000, 3210000
    )
    assert origin == pytest.approx((10.0, 52.0), abs=1e-10)
    cell = GridCell(resolution_m=100, north=2689100, east=4337000)
    geometry = cell_geojson_wgs84(cell)
    ring = geometry["coordinates"][0]
    assert geometry["type"] == "Polygon"
    assert len(ring) == 5
    assert ring[0] == ring[-1]
    assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in ring)
    assert cell_polygon_3035(cell).area == pytest.approx(10000)
    assert cell_polygon(cell, "EPSG:25833").area == pytest.approx(10000, rel=0.005)


def test_normalizes_observed_zero_and_missing_values(tmp_path):
    connector = ZensusGridConnector()
    batch = normalized_fixture(tmp_path)
    by_id = {record.grid_id: record for record in batch.records}
    zero = by_id["CRS3035RES100mN3271100E4548500"]
    missing = by_id["CRS3035RES100mN3271100E4548600"]
    assert by_id["CRS3035RES100mN3270900E4548500"].value == 12
    assert type(by_id["CRS3035RES100mN3270900E4548500"].value) is int
    assert zero.value == 0
    assert zero.quality_flags == ("zero_or_set_to_zero",)
    assert missing.value is None
    assert missing.value_status.value == "missing"
    assert batch.input_rows == len(batch.records) == 9
    assert batch.rejected_rows == 0
    assert connector.validate(batch).valid
    assert any("Scanned 9 rows" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    ("row_index", "field_index", "value", "message"),
    [
        (0, 3, "unknown", "unknown value token"),
        (0, 1, "4548551", "cell centre"),
        (0, 0, "CRS3035RES200mN3270800E4548400", "wrong grid resolution"),
    ],
)
def test_rejects_bad_rows_and_caps_messages(tmp_path, row_index, field_index, value, message):
    rows = [line.split(";") for line in (FIXTURES / "population.csv").read_text().splitlines()[1:]]
    rows[row_index][field_index] = value
    connector = ZensusGridConnector()
    batch = connector.normalize(bundle(make_zip(tmp_path, rows=rows)), configuration())
    assert batch.rejected_rows == 1
    assert batch.input_rows == len(batch.records) + 1
    assert message in batch.rejections[0]
    assert not connector.validate(batch).valid


def test_skips_rows_outside_bbox_without_counting_them(tmp_path):
    rows = [["CRS3035RES100mN3270900E4600000", "4600050", "3270950", "99"]]
    path = make_zip(tmp_path, rows=rows)
    batch = ZensusGridConnector().normalize(bundle(path), configuration())
    assert batch.records == ()
    assert batch.input_rows == 0
    assert batch.rejected_rows == 0
    assert any("Scanned 1 rows" in warning for warning in batch.warnings)


def test_rejects_wrong_column_count_and_minimum_cell_shortfall(tmp_path):
    rows = [["CRS3035RES100mN3270900E4548500", "4548550", "3270950"]]
    connector = ZensusGridConnector()
    wrong_width = connector.normalize(bundle(make_zip(tmp_path, rows=rows)), configuration())
    assert wrong_width.input_rows == wrong_width.rejected_rows == 1
    assert "wrong column count" in wrong_width.rejections[0]

    shortfall = connector.normalize(
        bundle(make_zip(tmp_path)), configuration(min_expected_cells=100)
    )
    assert any("Expected at least 100 cells" in error for error in shortfall.rejections)
    assert not connector.validate(shortfall).valid


def test_rejects_changed_header_description_pin_non_zip_and_traversal(tmp_path):
    connector = ZensusGridConnector()
    with pytest.raises(SchemaError, match="header differs"):
        connector.normalize(
            bundle(make_zip(tmp_path, header=["wrong", *HEADER[1:]])), configuration()
        )
    with pytest.raises(SchemaError, match="missing expected text: Stichtag"):
        connector.normalize(
            bundle(make_zip(tmp_path, description_texts=DESCRIPTION_TEXTS[:1])),
            configuration(),
        )
    with pytest.raises(SchemaError, match="source file changed"):
        connector.normalize(
            bundle(make_zip(tmp_path)),
            configuration(expected_sha256="0" * 64),
        )
    non_zip = tmp_path / "not-a-zip.txt"
    non_zip.write_text("not a ZIP")
    with pytest.raises(SchemaError, match="not a ZIP"):
        connector.normalize(bundle(non_zip), configuration())
    with pytest.raises(SchemaError, match="Unsafe path"):
        connector.normalize(
            bundle(make_zip(tmp_path, extra_member=("../escape.txt", b"bad"))),
            configuration(),
        )


def test_grid_observation_validation_and_roundtrip():
    record = grid_observation(GridCell(100, 3270900, 4548500), 12)
    serialized = record_to_dict(record)
    assert serialized["geometry"]["coordinates"] == record.geometry["coordinates"]
    assert record_from_dict(serialized) == record
    with pytest.raises(ValidationError, match="must be null"):
        grid_observation(GridCell(100, 3270900, 4548500), 1, status="suppressed")
    with pytest.raises(ValidationError, match="Polygon"):
        GridObservation(
            source_id="zensus_grid",
            snapshot_id="snapshot",
            source_record_id="grid",
            record_id="record",
            geometry={"type": "Point", "coordinates": [13.4, 52.5]},
            grid_id="CRS3035RES100mN3270900E4548500",
            grid_crs="EPSG:3035",
            resolution_m=100,
            metric_code="population_census",
            value=1,
            unit="persons",
            reference_period="2022-05-15",
            value_status="observed",
        )


def test_check_parent_counts_and_bbox_guard(tmp_path):
    records = normalized_fixture(tmp_path).records
    connector = ZensusGridConnector()
    coverage = connector.check_parent(configuration(), records, fixture_areas())
    comparison = coverage["berlin_comparison"]
    assert coverage["parent_dataset"] == "berlin_lor"
    assert comparison["cells_in_extract"] == len(records)
    assert comparison["cells_intersecting_berlin"] > 0
    assert comparison["cells_within_berlin"] > 0
    assert comparison["cells_partially_in_berlin"] > 0
    assert comparison["cells_outside_berlin"] > 0
    assert (
        comparison["population_cells_within_berlin"]
        <= comparison["population_cells_intersecting_berlin"]
    )
    assert comparison["official_total"]["value"] == 1000
    with pytest.raises(IncompatibleSnapshotError, match="requires Berlin LOR"):
        connector.check_parent(configuration(), records, ())
    with pytest.raises(IncompatibleSnapshotError, match="source_bbox truncates Berlin"):
        connector.check_parent(
            configuration(source_bbox=[4550000, 3271500, 4550100, 3271600]),
            records,
            fixture_areas(),
        )


def half_cell_catchment(first, second):
    minx, miny, maxx, maxy = second.bounds
    low, high = minx, maxx
    for _ in range(60):
        middle = (low + high) / 2
        left = second.intersection(box(minx - 1, miny - 1, middle, maxy + 1))
        if left.area < second.area / 2:
            low = middle
        else:
            high = middle
    half = second.intersection(box(minx - 1, miny - 1, high, maxy + 1))
    return unary_union([first, half])


def test_aggregate_grid_whole_cells_estimate_coverage_and_null_values():
    values = [10, 20, 30, None, 40, 50, 60, 70, 80]
    cells = []
    for row, north in enumerate((3270900, 3271000, 3271100)):
        for column, east in enumerate((4548500, 4548600, 4548700)):
            cell = GridCell(100, north, east)
            value = values[row * 3 + column]
            cells.append(
                grid_observation(cell, value, status="missing" if value is None else "observed")
            )
    polygon_a = cell_polygon(parse_grid_id(cells[0].grid_id), "EPSG:25833")
    polygon_b = cell_polygon(parse_grid_id(cells[1].grid_id), "EPSG:25833")
    catchment = half_cell_catchment(polygon_a, polygon_b)
    result = aggregate_grid(cells, catchment)
    assert result["whole_cells"]["within_sum"] == 10
    assert result["whole_cells"]["intersecting_sum"] == 30
    assert result["intersected_estimate"]["value"] == pytest.approx(20, abs=0.1)
    assert result["intersected_estimate"]["value_status"] == "estimated"
    assert result["bounds"]["lower"] == 10
    assert result["bounds"]["upper"] == 30
    assert result["cells"]["without_value"] == 0
    assert result["coverage"]["share_of_catchment"] == pytest.approx(1.0, abs=0.0001)
    assert result["definition"].startswith("Zensus 2022 census population")

    missing_result = aggregate_grid(
        [cells[3]], cell_polygon(parse_grid_id(cells[3].grid_id), "EPSG:25833")
    )
    assert missing_result["cells"]["without_value"] == 1
    assert missing_result["cells"]["without_value_by_status"] == {"missing": 1}
    assert missing_result["whole_cells"]["intersecting_sum"] == 0


def test_aggregate_grid_marks_missing_bounds_incomplete_and_excludes_missing_area():
    first = grid_observation(GridCell(100, 3270900, 4548500), 10)
    suppressed = grid_observation(GridCell(100, 3270900, 4548600), None, status="suppressed")
    first_polygon = cell_polygon(parse_grid_id(first.grid_id), "EPSG:25833")
    suppressed_polygon = cell_polygon(parse_grid_id(suppressed.grid_id), "EPSG:25833")
    catchment = unary_union([first_polygon, suppressed_polygon])

    result = aggregate_grid([first, suppressed], catchment)

    assert result["bounds"]["lower"] == 10
    assert result["bounds"]["upper"] is None
    assert result["bounds"]["complete"] is False
    assert result["whole_cells"]["intersecting_sum"] == 10
    assert result["whole_cells"]["complete"] is False
    assert result["coverage"]["published_cell_area_in_catchment_m2"] == pytest.approx(
        round(first_polygon.area, 1)
    )
    assert result["coverage"]["valueless_cell_area_in_catchment_m2"] == pytest.approx(
        round(suppressed_polygon.area, 1)
    )
    assert result["coverage"]["share_of_catchment"] == pytest.approx(0.5, abs=0.0001)
    assert any(
        "1 intersecting cells have no published value (statuses suppressed)" in warning
        and "upper bound unavailable" in warning
        for warning in result["warnings"]
    )


def test_aggregate_grid_rejects_mixed_data_or_periods():
    cell = GridCell(100, 3270900, 4548500)
    record = grid_observation(cell, 10)
    polygon = cell_polygon(cell, "EPSG:25833")
    with pytest.raises(ValueError, match="[Cc]ensus grid and register-based area data"):
        aggregate_grid(
            [
                record,
                AreaObservation(
                    source_id="berlin_population",
                    snapshot_id="register",
                    source_record_id="area",
                    record_id="area:population",
                    area_code="01011101",
                    hierarchy="planungsraum",
                    boundary_edition="2021",
                    metric_code="population_total",
                    value=10,
                    unit="persons",
                    reference_period="2022-05-15",
                    value_status="observed",
                ),
            ],
            polygon,
        )
    with pytest.raises(ValueError, match="reference_period"):
        aggregate_grid(
            [
                record,
                replace(
                    grid_observation(GridCell(100, 3270900, 4548600), 10),
                    reference_period="2022-05-16",
                ),
            ],
            polygon,
        )
    other_record = grid_observation(GridCell(100, 3270900, 4548600), 10)
    with pytest.raises(ValueError, match="snapshot_id"):
        aggregate_grid([record, replace(other_record, snapshot_id="other")], polygon)
    with pytest.raises(ValueError, match="unit"):
        aggregate_grid([record, replace(other_record, unit="households")], polygon)
    with pytest.raises(ValueError, match="duplicate grid_id"):
        aggregate_grid([record, record], polygon)
    result = aggregate_grid([record], polygon)
    assert result["snapshot_id"] == record.snapshot_id


def test_circle_catchment_has_metric_area_and_rejects_invalid_radius():
    radius = 500
    circle = circle_catchment(13.4132, 52.5219, radius)
    assert circle.area == pytest.approx(3.14159 * radius**2, rel=0.001)
    with pytest.raises(ValueError, match="greater than zero"):
        circle_catchment(13.4, 52.5, 0)
