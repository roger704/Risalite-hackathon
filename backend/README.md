# Location intelligence backend

The first implementation downloads, validates, stores, and serves Berlin LOR boundaries, official LOR names, and population observations. It includes shared typed contracts, an ingestion CLI, immutable snapshots, provenance manifests, and a FastAPI read API. Scoring, location recommendations, research agents, and the other proposed connectors are still to be implemented.

## Setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Run these commands from the repository root:

```sh
cd backend
uv sync --python 3.11
uv run location-data datasets
```

No API key is needed for either implemented public dataset. The default store is the repository's `data/` directory, which Git ignores. Set `LOCATION_DATA_DIR` to choose another store for both CLI and API. CLI options `--data-dir` and `--config-dir` must appear **before** the subcommand:

```sh
uv run location-data --data-dir ../data --config-dir config/datasets datasets
```

Reviewed source URLs, parser mappings, licenses, size limits, and editions live in `config/datasets/*.json`. A provider format change requires a reviewed configuration update; the backend does not discover replacement resources automatically.

## Import the official data

Import boundaries first, then official names and population:

```sh
uv run location-data ingest berlin_lor
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_lor_names --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run location-data ingest berlin_population
uv run location-data datasets
```

Each successful import prints a JSON manifest containing its `snapshot_id`. Names and population ingestion pin the active published LOR snapshot by default and require complete matching hierarchy, boundary-scheme, and area-code coverage. Use `--lor-snapshot` to pin the exact boundary snapshot used for either import:

```sh
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_population --lor-snapshot "$LOR_SNAPSHOT_ID"
```

Previously downloaded source files can be imported without contacting the provider. These must match the reviewed source edition and schema; arbitrary CSVs or boundary archives are not accepted by the default configurations:

```sh
uv run location-data ingest berlin_lor --file /absolute/path/to/lor-source.7z
uv run location-data ingest berlin_lor_names --file /absolute/path/to/combined-wfs-feature-collection.json --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run location-data ingest berlin_population --file /absolute/path/to/SB_A01-16-00_2026h01_BE.xlsx
```

For a provider WFS download, each page is a separate raw asset copied in response order to `raw/<dataset>/<snapshot>/<index>-page-NNNN.json`, with its own SHA-256 hash. `--file` accepts one combined FeatureCollection. Imports preserve source URLs, retrieval times, parser configuration hashes, quality reports, attribution, and parent snapshot IDs. Reimporting the same data with the same configuration and dependencies reuses its cached snapshot. Failed validation leaves the previously active snapshot available; diagnostic files remain in staging.

LOR geometry is stored as WGS84 GeoParquet and population records as Parquet. SQLite tracks immutable manifests, active snapshots, and ingestion jobs. Ingestion uses local filesystem locks and targets one backend instance on macOS/Linux; this is not a distributed ingestion service.

## Inspect snapshots

Set `POPULATION_SNAPSHOT_ID` to the population import's returned identifier:

```sh
POPULATION_SNAPSHOT_ID='replace-with-population-snapshot_id'
uv run location-data manifest berlin_population "$POPULATION_SNAPSHOT_ID"
uv run location-data records berlin_population --snapshot "$POPULATION_SNAPSHOT_ID" --area-code 01100101 --metric-code population_total --limit 10
```

Every records query requires an explicit snapshot. Supported filters are `area_code` and, for population, `metric_code`. Page limits are 1–1,000. Pass the returned `next_cursor` with `--cursor` to continue the same snapshot and filters. LOR codes are strings: preserve leading zeroes. Query an official name and its source attributes with:

```sh
uv run location-data records berlin_lor_names --snapshot "$NAMES_SNAPSHOT_ID" --area-code 01100101
```

For Python callers, `app.application.area_names.area_names(service, snapshot_id)` returns a pinned lookup of area codes to names and string attributes.

The API serves existing local snapshots and performs no hidden provider downloads:

```sh
uv run uvicorn app.api.main:app --reload
```

Uvicorn binds to loopback by default. Open [API documentation](http://127.0.0.1:8000/docs), or query from another terminal with the actual population snapshot ID:

```sh
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/api/v1/datasets
POPULATION_SNAPSHOT_ID='replace-with-population-snapshot_id'
curl --fail "http://127.0.0.1:8000/api/v1/datasets/berlin_population/snapshots/$POPULATION_SNAPSHOT_ID"
curl --fail --get http://127.0.0.1:8000/api/v1/datasets/berlin_population/records \
  --data-urlencode "snapshot_id=$POPULATION_SNAPSHOT_ID" \
  --data-urlencode 'area_code=01100101' \
  --data-urlencode 'metric_code=population_total' \
  --data-urlencode 'limit=10'
```

The records response includes reference and retrieval dates, coverage, warnings, attribution, and pagination. Availability and freshness are separate. Freshness remains `unknown` unless an explicit refresh policy has been configured; a successful download alone does not establish current demographic conditions.

## Validated sources and meaning

Source resources and mappings were checked on **18 October 2026**. The checked files are real official resources; offline test fixtures are synthetic and contain no research findings.

| Dataset | Reviewed resource | Validation result and limitations |
|---|---|---|
| `berlin_lor` | [Official LOR page](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/sozialraumorientierte-planungsgrundlagen/lebensweltlich-orientierte-raeume/), [download archive](https://www.berlin.de/sen/stadt/_assets/stadtdaten/stadtwissen/lebensweltlich-orientierte-raeume/lor_2021-01-01_k3_shapefiles_nur_id.7z?ts=1780323163) | 542 Planungsräume; reviewed member `LOR_2023-01-01_PLR_EPSG_25833_nur_ID.shp`, source CRS EPSG:25833. Boundary scheme `2021`, actual geometry release `2023-01-01`. The archive provides IDs, not neighborhood names; output names are explicit code-based fallbacks. Attribution: Amt für Statistik Berlin-Brandenburg, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). |
| `berlin_lor_names` | [Official LOR WFS metadata](https://daten.berlin.de/datensaetze/lebensweltlich-orientierte-raume-lor-01-01-2021-wfs-34c86848), WFS `lor_2021:a_lor_plr_2021` | 542 Planungsräume, official `plr_name` values and related LOR attributes; source CRS EPSG:25833, WFS 2.0 GeoJSON paging (200 features/page). Licensed [CC BY 3.0 DE](https://creativecommons.org/licenses/by/3.0/de/), attributed to Amt für Statistik Berlin-Brandenburg. The WFS layer is stand 01.01.2021, while `berlin_lor` geometry is the 2023-01-01 release of the same 2021 scheme; code sets must match exactly, geometry differences are reported and not reconciled. Names are official LOR names, not marketing neighborhood names. |
| `berlin_population` | [Official population page](https://www.statistik-berlin-brandenburg.de/a-i-16-hj/), [June 2026 workbook](https://download.statistik-berlin-brandenburg.de/fca0a030166493d0/1289b0921aa2/SB_A01-16-00_2026h01_BE.xlsx), worksheet `T2` | Reference date **30 June 2026**, source edition `2026h01`: 542 areas and 4,878 observations, consisting of one total plus eight age bands per area. The checked population totals sum to 3,917,196 registered residents. The workbook's imprint provides the source legend and [CC BY 3.0 DE license](https://creativecommons.org/licenses/by/3.0/de/). |

All 542 population codes matched the reviewed LOR code set, and each complete age-band breakdown matched its area total. The population code directory states December 2021. Matching codes establishes identifier compatibility; it does not certify that polygon boundaries were unchanged between source editions. The manifest pins the actual LOR snapshot used by each population import.

`reference_period` describes when the population was measured; `retrieved_at` records when this backend acquired the source. These dates must not be substituted for one another. Population measures registered principal-residence inhabitants, not workplace attendance, footfall, customer demand, or childcare vacancies.

Age intervals have an inclusive lower and **exclusive upper** bound: `[0, 6)`, `[6, 15)`, `[15, 18)`, `[18, 27)`, `[27, 45)`, `[45, 55)`, `[55, 65)`, and `[65, ∞)`. `population_total` and `population_age_band` are separate metrics. Adding a total to its age bands double-counts people.

The connector distinguishes numeric zero from suppressed, missing, and not-applicable values. The official legend's `–` means no quantity and becomes zero; `•` becomes a null suppressed value; `…` and `/` become null missing values; `x` becomes null not applicable. Original markers remain in quality flags. Unrecognized annotations fail validation instead of becoming zero. Explicit CSV settings are supported for reviewed alternative configurations, but the default official import uses XLSX.

HTML returned instead of data, changed headers, invalid identifiers, duplicate observations, overlapping age-band mappings, incomplete area coverage, and inconsistent age totals prevent publication. The general [connector blueprint](../CONNECTOR_BLUEPRINT.md) describes later connectors and the wider architecture; it is not a list of already implemented capabilities.

## Adding a connector

Shared record types live in `app/domain/models.py`: `Area`, `AreaObservation`, `Place` (mapped POIs), `Business` (registered businesses), `TransitStop`, and `StopService` (scheduled departures in an explicit window). Point geometries are WGS84 GeoJSON points, and snapshots of point records are stored as GeoParquet.

Register an adapter name in `app/connectors/registry.py`; modules load lazily, so a planned adapter that has no module yet fails with `Adapter is not implemented`. A dataset that must be pinned to another snapshot sets `parser_options.parent_dataset`, and its connector implements `check_parent(config, records, parent_records) -> dict`. The returned dict is merged into the manifest coverage. Use `--lor-snapshot` to pin an explicit parent.

`berlin_wfs` is a generic WFS 2.0 GeoJSON adapter. A reviewed dataset configuration specifies `typename`, `page_size`, `max_pages`, optional `max_total_bytes`, `expected_crs`, `source_bbox`, optional `allowed_bbox`, `expected_properties`, `id_field`, optional `id_pattern`, exactly one of `expected_count` or `expected_count_range`, `record_type`, `field_map`, `attribute_fields`, `hierarchy`, and optional `geometry_types`. Area sources may also set `parent_dataset` and `geometry_difference_threshold`. Once a record type has a normalizer, datasets such as Berlin Kitas or schools can reuse the WFS paging and CRS checks through configuration rather than a source-specific fetcher.

The WFS provider includes a changing `timeStamp` in each response, so a fresh provider download changes its raw hashes and does not hit the snapshot cache. Reimporting the same local files does reuse the cache.

## Development checks

```sh
uv run pytest
uv run pytest -m live tests/live/test_berlin_wfs_live.py -k berlin_wfs
uv run ruff check app tests
uv run ruff format --check app tests
```

Pytest's default options exclude tests marked `live`; an explicit `-m live` opts in to public-provider tests. GitHub Actions runs the offline checks with `uv sync --locked`.

The normal suite runs offline with synthetic fixtures and mocked acquisition. It exercises both connectors, contracts, ingestion failures and snapshot compatibility, API queries, and CLI behavior. It does not require downloading all provider data on each test run. Full official datasets and generated local snapshots are excluded from Git.
