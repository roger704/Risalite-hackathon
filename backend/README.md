# Location intelligence backend

The backend downloads, validates, stores, and serves Berlin LOR boundaries, population observations, and OpenStreetMap points of interest. It includes shared typed contracts, an ingestion CLI, immutable snapshots, provenance manifests, and a FastAPI read API. Scoring, location recommendations, research agents, and the other proposed connectors are still to be implemented.

## Setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Run these commands from the repository root:

```sh
cd backend
uv sync --python 3.11
uv run location-data datasets
```

No API key is needed for the implemented public datasets. The default store is the repository's `data/` directory, which Git ignores. Set `LOCATION_DATA_DIR` to choose another store for both CLI and API. CLI options `--data-dir` and `--config-dir` must appear **before** the subcommand:

```sh
uv run location-data --data-dir ../data --config-dir config/datasets datasets
```

Reviewed source URLs, parser mappings, licenses, size limits, and editions live in `config/datasets/*.json`. Category mappings are versioned separately under `config/categories/`; each dataset pins a mapping's SHA-256 so an edit changes parser configuration identity and prevents reuse of a snapshot created with a different mapping. A provider format change requires a reviewed configuration update; the backend does not discover replacement resources automatically.

## Import the official data

Import boundaries first, then population:

```sh
uv run location-data ingest berlin_lor
uv run location-data ingest berlin_population
uv run location-data datasets
```

Each successful import prints a JSON manifest containing its `snapshot_id`. Population ingestion pins the active published LOR snapshot by default and requires complete matching hierarchy, boundary-scheme, and area-code coverage. To select an existing boundary snapshot explicitly, replace the example identifier with the `snapshot_id` returned by the LOR import:

```sh
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_population --lor-snapshot "$LOR_SNAPSHOT_ID"
```

Previously downloaded LOR and population files can be imported without contacting the provider. These must match the reviewed source edition and schema; arbitrary CSVs or boundary archives are not accepted by the default configurations:

```sh
uv run location-data ingest berlin_lor --file /absolute/path/to/lor-source.7z
uv run location-data ingest berlin_population --file /absolute/path/to/SB_A01-16-00_2026h01_BE.xlsx
```

The primary OSM source is a pinned Geofabrik Berlin PBF extract. Import it directly, or provide a previously downloaded `.pbf` or `.osm` XML file. The default configuration verifies the PBF's UTC replication date against `source_edition`; headerless local XML or synthetic files require an explicit `allow_unverified_edition` parser override and produce a warning. That override is not enabled in the committed provider configuration:

```sh
uv run location-data ingest osm_pois
uv run location-data ingest osm_pois --file /absolute/path/to/berlin-261017.osm.pbf
```

`osm_pois_overpass` is an optional bounded refresh for a small central-Berlin bbox, not a citywide alternative. It is not needed for the demo and remains unvalidated because the public endpoint was unreachable or returned HTTP 500 during the 18 October 2026 check:

```sh
uv run location-data ingest osm_pois_overpass
```

The raw file is copied into the local store. Imports preserve SHA-256 hashes, source URLs, retrieval times, parser configuration hashes, quality reports, attribution, and parent snapshot IDs. Reimporting the same data with the same configuration and dependencies reuses its cached snapshot. Failed validation leaves the previously active snapshot available; diagnostic files remain in staging.

LOR geometry is stored as WGS84 GeoParquet and population records as Parquet. SQLite tracks immutable manifests, active snapshots, and ingestion jobs. Ingestion uses local filesystem locks and targets one backend instance on macOS/Linux; this is not a distributed ingestion service.

## Inspect snapshots

Set `POPULATION_SNAPSHOT_ID` to the population import's returned identifier:

```sh
POPULATION_SNAPSHOT_ID='replace-with-population-snapshot_id'
uv run location-data manifest berlin_population "$POPULATION_SNAPSHOT_ID"
uv run location-data records berlin_population --snapshot "$POPULATION_SNAPSHOT_ID" --area-code 01100101 --metric-code population_total --limit 10
```

Every records query requires an explicit snapshot. Supported filters are `area_code` and, for population, `metric_code`. Page limits are 1–1,000. Pass the returned `next_cursor` with `--cursor` to continue the same snapshot and filters. LOR codes are strings: preserve leading zeroes.

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

### Research dashboard

From `backend/`, start the app with `uv run uvicorn app.api.main:app --reload` and open
<http://127.0.0.1:8000/>. The dashboard uses the root research API. Its temporary run
stand-in replays synthetic fixtures; set `RISALITE_RUNS=off` to disable it. The dashboard
and the API share the same origin by default. For a separate API host, append `?api=BASE`
to the dashboard URL.

The fixture files live in `frontend/fixtures/`. Rebuild them offline with
`uv run python ../frontend/fixtures/build_fixtures.py --geojson-from ../frontend/fixtures/cells.geojson`.

The records response includes reference and retrieval dates, coverage, warnings, attribution, and pagination. Availability and freshness are separate. Freshness remains `unknown` unless an explicit refresh policy has been configured; a successful download alone does not establish current demographic conditions.

### Area summary

`GET /api/v1/areas/{area_code}/summary` combines the active (or explicitly pinned) `berlin_lor`, `berlin_lor_names`, `berlin_population`, `osm_pois`, `ihk_businesses`, and `vbb_gtfs` snapshots into one provenance-bearing Planungsraum summary. Only `berlin_lor` is required; other datasets degrade to explicit `missing`/`unavailable`/`incompatible` states. Schema, statuses, and a frontend example are in [CONNECTOR_BLUEPRINT.md](../CONNECTOR_BLUEPRINT.md#area-summary).

```sh
curl --fail --get http://127.0.0.1:8000/api/v1/areas/01100101/summary \
  --data-urlencode 'category=coffee_shop' --data-urlencode 'include_geometry=false'
```

Pin snapshots with `lor_snapshot_id`, `names_snapshot_id`, `population_snapshot_id`, `osm_snapshot_id`, `ihk_snapshot_id`, `gtfs_snapshot_id`. The first request per snapshot builds an in-memory spatial index (about 1–2 s on the full real snapshots); later requests take about 0.5 s. Synthetic frontend fixtures are in `tests/fixtures/area_summary/`; regenerate them with `UPDATE_AREA_SUMMARY_FIXTURES=1 uv run pytest tests/integration/test_area_summary.py`.

## Validated sources and meaning

Source resources and mappings were checked on **18 October 2026**. The checked files are real provider resources; offline fixtures are small reviewed subsets or synthetic. Berlin environmental WFS subsets preserve source geometries. Fixtures contain no derived research findings.

| Dataset | Reviewed resource | Validation result and limitations |
|---|---|---|
| `berlin_lor` | [Official LOR page](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/sozialraumorientierte-planungsgrundlagen/lebensweltlich-orientierte-raeume/), [download archive](https://www.berlin.de/sen/stadt/_assets/stadtdaten/stadtwissen/lebensweltlich-orientierte-raeume/lor_2021-01-01_k3_shapefiles_nur_id.7z?ts=1780323163) | 542 Planungsräume; reviewed member `LOR_2023-01-01_PLR_EPSG_25833_nur_ID.shp`, source CRS EPSG:25833. Boundary scheme `2021`, actual geometry release `2023-01-01`. The archive provides IDs, not neighborhood names; output names are explicit code-based fallbacks. Attribution: Amt für Statistik Berlin-Brandenburg, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). |
| `berlin_lor_names` | [Official LOR WFS metadata](https://daten.berlin.de/datensaetze/lebensweltlich-orientierte-raume-lor-01-01-2021-wfs-34c86848), WFS `lor_2021:a_lor_plr_2021` | 542 Planungsräume, official `plr_name` values and related LOR attributes; source CRS EPSG:25833, WFS 2.0 GeoJSON paging (200 features/page). Licensed [CC BY 3.0 DE](https://creativecommons.org/licenses/by/3.0/de/), attributed to Amt für Statistik Berlin-Brandenburg. The WFS layer is stand 01.01.2021, while `berlin_lor` geometry is the 2023-01-01 release of the same 2021 scheme; code sets must match exactly, geometry differences are reported and not reconciled. Names are official LOR names, not marketing neighborhood names. |
| `berlin_centers` | [StEP Zentren 2040 dataset](https://daten.berlin.de/datensaetze/stadtentwicklungsplan-step-zentren-2040-wfs-7eb416eb), [WFS service](https://gdi.berlin.de/services/wfs/step_zen_2040) | Source edition/reference date `2026-01-13`: 81 `zh` features (79 Points, 2 MultiPolygons) and 33 `fma` Points. Mapping `step_zentren_2040_v1`; source CRS EPSG:25833 and stored geometry WGS84. DL-DE-Zero-2.0, attributed to Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin. Specialist-retail sites are complementary planning sites, not städtische Zentren; only two Zentrumsbereich polygons have published boundaries. |
| `berlin_environment` | [Umweltgerechtigkeit 2023/2024 package](https://daten.berlin.de/datensaetze/umweltgerechtigkeit-2023-2024-umweltatlas-wfs-2d02ba21), WFS `ua_umweltgerechtigkeit2023` | Eight 542-Planungsraum WFS layers provide nine ordinal `AreaIndicatorClass` indicators and a shared `Area` geometry set. Source CRS EPSG:25833; layers and classes are validated against one another. Licensed [DL-DE-ZERO-2.0](https://www.govdata.de/dl-de/zero-2-0), attributed to Umweltatlas Berlin, Senatsverwaltung für Mobilität, Verkehr, Klimaschutz und Umwelt Berlin. Historical/modelled area indicators are context only and are not part of any opportunity score. |
| `berlin_traffic_volumes_2019` | [Umweltatlas Verkehrsmengen](https://www.berlin.de/umweltatlas/verkehr-laerm/verkehrsmengen/), WFS `ua_verkehrsmengen_2019:verkehrsmengen_2019` | 10,420 road segments with 2019 average daily motor-vehicle counts (DTV), source CRS EPSG:25833; WFS 2.0 GeoJSON paging (1,000 features/page). Licensed [Datenlizenz Deutschland – Zero – 2.0](https://www.govdata.de/dl-de/zero-2-0), attributed to Senatsverwaltung für Mobilität, Verkehr, Klimaschutz und Umwelt Berlin (Umweltatlas). |
| `ihk_businesses` | [IHKBerlin_Gewerbedaten](https://github.com/IHKBerlin/IHKBerlin_Gewerbedaten), [CSV at commit `75957615`](https://media.githubusercontent.com/media/IHKBerlin/IHKBerlin_Gewerbedaten/75957615265716ec8549d04d63ed76bd6d6c547a/data/IHKBerlin_Gewerbedaten.csv) | Source edition `2026-10` (update of 1 October 2026), 125,464,561 bytes, SHA-256 `64456f92…8181e`. UTF-8 with BOM, comma-delimited, 20 columns, `NULL` for missing values. 366,374 rows; 366,368 published businesses; 6 rows without valid coordinates excluded and listed in the quality report. 1,343 `coffee_shop` records under mapping `ihk_v1`. Supplied PLR codes: 11 missing, 0 unknown to LOR, 1 point outside its supplied polygon. Attribution: IHK Berlin, [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). |
| `berlin_population` | [Official population page](https://www.statistik-berlin-brandenburg.de/a-i-16-hj/), [June 2026 workbook](https://download.statistik-berlin-brandenburg.de/fca0a030166493d0/1289b0921aa2/SB_A01-16-00_2026h01_BE.xlsx), worksheet `T2` | Reference date **30 June 2026**, source edition `2026h01`: 542 areas and 4,878 observations, consisting of one total plus eight age bands per area. The checked population totals sum to 3,917,196 registered residents. The workbook's imprint provides the source legend and [CC BY 3.0 DE license](https://creativecommons.org/licenses/by/3.0/de/). |
| `berlin_planning` | [Berlin development-plan WFS](https://daten.berlin.de/datensaetze/bebauungsplanverfahren-in-berlin-wfs-4f574ea7), `bplan:a_bp_iv`, `bplan:b_bp_fs`, `bplan:c_bp_ak` | Separate page streams for plans in procedure, in force, and repealed; Polygon/MultiPolygon geometries originate in EPSG:25833. Statuses are mapped to a small procedure vocabulary; URLs are retained without fetching documents. A spatial match is evidence of a planning procedure, not a claim about construction or use. Attribution: Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin, [DL-DE-ZERO-2.0](https://www.govdata.de/dl-de/zero-2-0). |
| `berlin_population_forecast` | [Official forecast page](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/bevoelkerungsprognose-2024-2040/), pinned [small-area workbook](https://www.berlin.de/sen/stadt/_assets/stadtdaten/stadtwissen/bevoelkerungsprognose-2024-2040/kleinraeumige_ergebnistabellen_bevoelkerungsprognose_2024-2040-_1_.xlsx?ts=1780323150) and [scenario overview](https://www.berlin.de/sen/stadt/_assets/stadtdaten/stadtwissen/bevoelkerungsprognose-2024-2040/uebersichtstabellen_grafiken_bevoelkerungsprognose-2024-2040-_1_.xlsx?ts=1780323147) | Source edition `2024-2040`, base date 31 December 2024, scenarios middle/upper/lower. The middle variant covers Berlin, 12 districts, and 58 Prognoseräume with total and single-year ages; upper/lower are Berlin-wide rounded thousands for 2025, 2030, and 2040 only. No explicit reuse terms were found; redistribution remains `unknown`. |
| `zensus_grid` | [Official Zensus 2022 page](https://www.destatis.de/DE/Themen/Gesellschaft-Umwelt/Bevoelkerung/Zensus2022/_inhalt.html), [pinned population-grid ZIP](https://www.destatis.de/static/DE/zensus/gitterdaten/Zensus2022_Bevoelkerungszahl.zip) | 100 m census population cells, publication Version 2 (30 September 2024), reference date **15 May 2022**. INSPIRE IDs encode cell corners in EPSG:3035; stored cell polygons are WGS84. Licensed [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0), with attribution to the Statistische Ämter des Bundes und der Länder. |
| `berlin_households` | [Official household tables page](https://www.statistik-berlin-brandenburg.de/a-i-17-j/), [2025 workbook](https://download.statistik-berlin-brandenburg.de/1c12fbe76a5c983f/bef7fc5b1b3a/SB_A01-17-00_2025j01_BE.xlsx), worksheets `T1.1`, `T1.2`, `T2.2`, `T3`, `T4` | Reference date **31 December 2025**, source edition `2025j01`: household counts, residents in households, household-size and household-type breakdowns, single-person age groups, and child-household counts across 542 Planungsräume. A derived share reports households with children as a percentage of all households. `T4!I` is the source-published single-parent share among households with children. |
| `osm_pois` | [Geofabrik Berlin page](https://download.geofabrik.de/europe/germany/berlin.html), [pinned Berlin extract](https://download.geofabrik.de/europe/germany/berlin-261017.osm.pbf) | `berlin-latest.osm.pbf` redirected to the dated `berlin-261017.osm.pbf` file on 18 October 2026. The 99,794,715-byte file header reports replication timestamp `2026-10-17T20:20:51Z` (source edition `2026-10-17`). A real import produced 11,837 places within bbox `[13.0, 52.3, 13.9, 52.8]`: 2,666 `coffee_shop`, 71 `coffee_retail`, 1,262 `bakery`, 2,877 `fast_food`, 4,799 `restaurant`, 148 `coworking`, and 14 unmapped; 11,112 nodes, 724 way polygons, and 1 relation multipolygon, with no assembly failures or unlocatable elements. The configured minimum is 1,800 coffee shops (70% of the observed count, rounded down to the nearest hundred). OSM completeness and operating status are not guaranteed. Attribution: © OpenStreetMap contributors, ODbL 1.0, Berlin extract by Geofabrik GmbH. |
| `osm_pois_overpass` | [Overpass API](https://overpass-api.de/api/interpreter) | Optional pinned query for bbox `[52.520, 13.390, 52.540, 13.420]`; this is a small-area refresh only. The endpoint was unreachable or returned HTTP 500 during the 18 October 2026 check. A response with an Overpass `remark` or without `elements` is rejected as incomplete. |
| `vbb_gtfs` | [VBB datasets page](https://unternehmen.vbb.de/digitale-services/datensaetze/), [GTFS resource](https://www.vbb.de/gtfs) | Reviewed feed edition `20261015-20261212`, validity 2026-10-15–2026-12-12. GTFS stops and scheduled departures are summarized by station and local time window; see the counting rules below. Attribution: Verkehrsverbund Berlin-Brandenburg GmbH (VBB), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). |
| `berlin_bicycle_counts` | [Berlin bicycle-counts page](https://daten.berlin.de/datensaetze/radzahldaten-in-berlin), [hourly workbook](https://www.berlin.de/sen/uvk/_assets/verkehr/verkehrsplanung/radverkehr/weitere-radinfrastruktur/zaehlstellen-und-fahrradbarometer/gesamtdatei-stundenwerte.xlsx) | Reviewed edition `2026-06-25`; hourly values from 35 fixed bicycle counters are normalized to daily totals and configured time-window means. Counts measure bicycle traffic only at those counters, not pedestrian footfall, customers, or revenue, and are not projected onto unobserved streets. Attribution: Senatsverwaltung für Mobilität, Verkehr, Klimaschutz und Umwelt Berlin; [Datenlizenz Deutschland – Zero – 2.0](https://www.govdata.de/dl-de/zero-2-0). |

All 542 population codes matched the reviewed LOR code set, and each complete age-band breakdown matched its area total. The population code directory states December 2021. Matching codes establishes identifier compatibility; it does not certify that polygon boundaries were unchanged between source editions. The manifest pins the actual LOR snapshot used by each population import.

`reference_period` describes when the population was measured; `retrieved_at` records when this backend acquired the source. These dates must not be substituted for one another. Population measures registered principal-residence inhabitants, not workplace attendance, footfall, customer demand, or childcare vacancies.

Age intervals have an inclusive lower and **exclusive upper** bound: `[0, 6)`, `[6, 15)`, `[15, 18)`, `[18, 27)`, `[27, 45)`, `[45, 55)`, `[55, 65)`, and `[65, ∞)`. `population_total` and `population_age_band` are separate metrics. Adding a total to its age bands double-counts people.

The connector distinguishes numeric zero from suppressed, missing, and not-applicable values. The official legend's `–` means no quantity and becomes zero; `•` becomes a null suppressed value; `…` and `/` become null missing values; `x` becomes null not applicable. Original markers remain in quality flags. Unrecognized annotations fail validation instead of becoming zero. Explicit CSV settings are supported for reviewed alternative configurations, but the default official import uses XLSX.

HTML returned instead of data, changed headers, invalid identifiers, duplicate observations, overlapping age-band mappings, incomplete area coverage, and inconsistent age totals prevent publication. The general [connector blueprint](../CONNECTOR_BLUEPRINT.md) describes later connectors and the wider architecture; it is not a list of already implemented capabilities.

## Berlin population forecast (`berlin_population_forecast`)

Import a compatible LOR snapshot first, then import the forecast pinned to that parent:

```sh
uv run location-data ingest berlin_lor
uv run location-data ingest berlin_population_forecast --lor-snapshot "$LOR_SNAPSHOT_ID"
```

The committed config has `redistribution_status: "unknown"`, so ingestion is refused by the publication gate until reuse terms are confirmed and that field is changed; until then, use a reviewed local `--config-dir` copy for evaluation.

The connector retains fractional forecasts at their published precision. Small-area values exist only for the middle variant at Berlin, district, and Prognoseraum levels; upper and lower variants are Berlin-wide values rounded to thousands. The finest published geography is the Prognoseraum, and the LOR prefix check does not assign any forecast to Planungsräume. Records at 31 December 2024 are observed base stock; later periods are forecasts. Total and age records are alternative views, not additive quantities. The landing page and workbooks did not state reuse terms, so the committed redistribution status remains `unknown` pending confirmation.

The API exposes expected change between the observed base and a selected target year:

```sh
curl --fail --get http://127.0.0.1:8000/api/v1/population-forecast/expected-change \
  --data-urlencode "snapshot_id=$FORECAST_SNAPSHOT_ID" \
  --data-urlencode 'area_code=0210' \
  --data-urlencode 'hierarchy=prognoseraum' \
  --data-urlencode 'target_year=2040' \
  --data-urlencode 'scenario=middle'
```

## Berlin environmental indicators (`berlin_environment`)

Import the reviewed Berlin Planungsraum environment indicators after the canonical LOR boundaries. The import pins the active LOR snapshot by default; use `--lor-snapshot` to select one explicitly:

```sh
uv run location-data ingest berlin_lor
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_environment --lor-snapshot "$LOR_SNAPSHOT_ID"
ENVIRONMENT_SNAPSHOT_ID='replace-with-environment-snapshot_id'
uv run location-data records berlin_environment --snapshot "$ENVIRONMENT_SNAPSHOT_ID" --area-code 01100101
```

`berlin_environment` emits 542 `Area` records with geometry from `a_laerm2023` and 4,878 `AreaIndicatorClass` records (542 for each of nine indicators). Classes are ordinal labels, not numeric measurements; indicator records have no geometry and join by exact `area_code`. Missing source values remain null with their source markers in `quality_flags`. A local `--file` import is unsupported because each snapshot requires all eight coordinated WFS layers; it fails with a schema error rather than publishing partial data.

| Indicator | Reference period | Rank direction |
|---|---|---|
| `noise_burden` | 2022 | Higher rank means more burden |
| `air_pollution_burden` | 2023 | Higher rank means more burden |
| `green_space_provision` | 2020 | Higher rank means worse provision |
| `thermal_burden` | 2022 | Higher rank means more burden |
| `social_disadvantage` | 2023 | Higher rank means more disadvantage |
| `multiple_burden_environment` | 2023/2024 | Higher rank means more worst-class indicators |
| `multiple_burden_environment_social` | 2023/2024 | Higher rank means more worst-class indicators |
| `population_density_class` | 31 December 2023 | Higher rank means denser |
| `simple_residential_location_hint` | 2024 | Flag only; null means not flagged or unknown |

The classes are relative to Berlin and use different methods and reference periods; they are not legal limits, address-level measurements, individual health predictions, or measures of exposure at a particular premises. Noise, air, thermal and green-space classes are model/interpolation based; social index areas below 300 residents are excluded and remain missing. Green-space classes measure public provision per resident, not quality or private gardens. The official population legend says “Einwohnerdichte in EW/km²” while the method describes resident counts. The official maps also mask largely uninhabited areas, while the WFS contains full Planungsraum polygons. Do not average or sum ordinal ranks. These indicators are contextual data and are not used in any opportunity score; any future scoring transformation requires separate documentation.

The config-driven legend and indicator details are available without contacting the provider:

```sh
curl --fail http://127.0.0.1:8000/api/v1/datasets/berlin_environment/legend
```

The response includes the configured class colors, missing markers, method/data-basis citations, limitations, active snapshot ID and whether its config matches. `latest_snapshot_id` is null when no snapshot is active or the active snapshot was normalized with a different config.

The live contract check is bounded to one feature request per layer and is opt-in:

```sh
RUN_LIVE_TESTS=1 uv run pytest tests/live/test_berlin_environment_live.py -m live
```

## Zensus 2022 population grid

Import the reviewed census grid after the LOR boundaries so the snapshot can be checked against Berlin:

```sh
uv run location-data ingest berlin_lor
uv run location-data ingest zensus_grid
```

The connector streams the pinned 100 m CSV from the ZIP and retains cells intersecting the configured EPSG:3035 extent. The manifest reports whole cells within Berlin, all cells intersecting Berlin, cells whose centroids fall inside Berlin, and an area-weighted estimate. The fully contained published-cell sum is the lower bound; the all-intersecting-cell sum is an upper bound only when cells with published values cover the whole catchment (otherwise the upper bound is null because absent cells may be unpublished). Areal weighting assumes uniform density within each partially intersecting cell. Coverage reports how much of the catchment is covered by published cells, while absent cells remain unpublished rather than being treated as measured zero.

Catchments must lie inside the recorded extract extent; otherwise the CLI rejects them to avoid undercounting. If an intersecting cell has no published value, the upper bound is unavailable.

The counts are census population at the historical reference date **15 May 2022**, perturbed using the cell-key method (CKM). They are not current residents or the Einwohnerregister population. Berlin's census total is 3,596,999; the register-based count for 30 June 2022 is 3,725,650. Do not add, mix, or substitute `zensus_grid` and `berlin_population` values. Grid totals also need not equal official totals because of CKM perturbation, unpublished cells, and addresses not represented in the grid.

Query a pinned snapshot with a straight-line circular catchment (not walking time):

```sh
ZENSUS_SNAPSHOT_ID='replace-with-zensus-grid-snapshot_id'
uv run location-data records zensus_grid --snapshot "$ZENSUS_SNAPSHOT_ID" --limit 3
uv run location-data catchment zensus_grid --snapshot "$ZENSUS_SNAPSHOT_ID" \
  --lon 13.4132 --lat 52.5219 --radius-m 500
```

## OpenStreetMap points of interest

`osm_pois` is the primary Geofabrik PBF import; `osm_pois_overpass` is an optional bounded Overpass dataset using the same adapter and normalization contract. The parser streams OSM objects, assembles closed ways and multipolygon relations with pyosmium, and represents polygons by their WGS84 point-on-surface. Open ways use a representative point of their line. If an area cannot be assembled but member coordinates are available, the connector emits a fallback point and flags the failure; selected elements with no usable coordinates are reported separately and do not become rejected rows. The PBF replication timestamp's UTC date must match the configured edition; headerless synthetic inputs are only accepted with the explicit test/local override.

`config/categories/osm_v1.json` is the versioned category mapping. Selectors decide which tagged elements are retained, and ordered exact tag matches assign canonical categories. `coffee_shop` is the primary competitor category; `coffee_retail` is contextual (`shop=coffee` is not counted as a café competitor); bakery, fast-food, restaurant, and coworking are complementary or contextual. A selected element with no exact match remains in the output with `canonical_category: null` and a `category_unmapped` quality flag. The original matched tag is preserved in `original_category`. Only the mapping's explicit attribute allowlist is exported; phone numbers and address-tag dumps are not retained. Mapping roles document interpretation and do not alter the `Place` model.

OSM is a community-maintained map, not a verified inventory of currently operating businesses. Opening-hours and website tags may be missing or outdated; completeness and tagging practices vary. `shop=coffee` may represent beans or equipment rather than a café. Overpass way/relation centers can lie outside the small query bbox when their geometry intersects it, so returned points are checked against the broader Berlin extent instead; Overpass nodes are additionally constrained to the query bbox. Derived databases remain subject to ODbL share-alike requirements, and dated Geofabrik URLs may expire or be removed; changing the pinned source or minimum expected counts requires a reviewed configuration update.

## VBB GTFS scheduled-service summaries

Import the reviewed feed from VBB or a previously downloaded ZIP:

```sh
uv run location-data ingest vbb_gtfs
uv run location-data ingest vbb_gtfs --file /path/to/gtfs.zip
```

The connector streams `stop_times.txt` from the archive and counts scheduled departure supply, not passenger boardings. Trips active on the selected service date use their GTFS service times; trips active on the previous service date shift times of 24:00 or later back by 24 hours so after-midnight service can fall into the selected date's local windows. The last stop of a trip and any event with `pickup_type=1` are excluded. Departures at multiple platforms of one station are deduplicated by station, service day, trip, and frequency run. Frequency-based trips use their headway expansion rather than literal template times; non-exact headways carry an estimate quality flag. Each configured window includes its start and excludes its end.

The reviewed feed hash is pinned as a warning-only integrity reference. VBB updates the feed twice weekly according to its datasets page. A feed whose computed validity differs from the reviewed edition fails ingestion until the configuration is reviewed; a changed SHA-256 with matching validity is reported as a warning. Re-pinning a new edition is a manual review step: download the ZIP, confirm its calendar range and license, then update `source_edition`, `reference_period`, `valid_from`, `valid_to`, `service_date` (a non-DST weekday inside the new range) and `reviewed_sha256` in `config/datasets/vbb_gtfs.json` before re-importing.

Limitations: scheduled supply is not passengers or footfall; straight-line proximity is not walking time; GTFS-RT is out of scope; `wheelchair_boarding=0` means no information, not inaccessible; `StopService.area_code` remains null until the scoring PR's LOR spatial join; and this feed covers all of Berlin-Brandenburg, not just Berlin. VBB assigns `parent_station` to only 2,113 of 27,940 platforms; nearby tram and bus stops of a hub (for example `S+U Alexanderplatz Bhf/Memhardstr.`) are separate stations, which is why the Alexanderplatz row below lists only S-Bahn (109) and U-Bahn (400). Hub-level supply needs distance-based aggregation in the scoring layer.

### Real-feed import measurements

Both imports used the reviewed feed edition (valid 2026-10-15 through 2026-12-12) and produced raw SHA-256 `1328a75246d9ce72d9a01def4a37ab9bcf36cd87e079c29a0f351daab3b7897d`.

| Acquisition | Runtime | Maximum RSS |
|---|---:|---:|
| Local ZIP (`--file`) | 18.17 s | 800,696 KB |
| VBB provider download | 23.17 s | 929,388 KB |

The imports processed 4,032,869 input rows into 86,067 records with zero rejections. Output contains 27,940 type-0 stops, 868 type-1 stations, 6,417 type-2 entrances, and 25,421 `StopService` station keys. The morning window (07:00–10:00) has 22,287 stations with departures and 245,048 departures total; the afternoon window (16:00–19:00) has 21,964 stations and 240,589 departures. The feed had no foreign-key or hash-mismatch warnings; 6,740 type-3 nodes were skipped as configured, alongside the documented limitations and per-window summaries.

| Station | Window | Departures | Trips | Route types |
|---|---|---:|---:|---|
| S+U Alexanderplatz Bhf (Berlin) | 07:00–10:00 | 361 | 361 | 109, 400 |
| S+U Alexanderplatz Bhf (Berlin) | 16:00–19:00 | 365 | 365 | 109, 400 |
| S+U Zoologischer Garten Bhf (Berlin) | 07:00–10:00 | 301 | 301 | 109, 400 |
| S+U Zoologischer Garten Bhf (Berlin) | 16:00–19:00 | 306 | 306 | 109, 400 |

### Berlin household profiles (`berlin_households`)

The connector emits the reviewed metrics from `T1.1` (residents in households), `T1.2` (household size and average size), `T2.2` (household types), `T3` (single-person household age groups), and `T4` (households with and without children). It also derives `households_with_children_share` as a percentage of all households; `T4!I` remains the source-published single-parent share among households with children.

These are register-generated household profiles at **31 December 2025**, not the population snapshot's 30 June 2026 reference date. Household counts are not person counts; only `residents_in_households_total` is measured in persons. The source defines children as unmarried persons under 18 without their own children and without their own household. All household tables are overlapping breakdowns and must not be added together.

After importing the matching `berlin_lor` snapshot, ingest the workbook or pin an explicit LOR parent snapshot:

```sh
uv run location-data ingest berlin_lor
uv run location-data ingest berlin_households
uv run location-data ingest berlin_households --lor-snapshot "$LOR_SNAPSHOT_ID"
```

Real import on 18 October 2026 (provider download into a fresh data dir after `berlin_lor`): 542 of 542 Planungsräume, 14,634 observations (27 metrics × 542), 0 rejected rows, joined exactly to `berlin_lor` (542 areas). Wall time 1.34 s, peak RSS 239,376 KB; raw XLSX 580,763 bytes (SHA-256 `e83a88f1…87a7a`), normalized snapshot 1,033,989 bytes. Checked totals: 2,231,314 households, 376,366 with children (16.9 %), 3,930,822 residents in households. Example `01100101` (Stülerstraße): 2,310 households, average size 1.59, 247 with children (`households_with_children_share` 10.69), single-parent share 22.3 %.

## Research adapters (Tavily, Nebius)

The backend includes offline-testable Tavily search/extraction and Nebius structured-inference adapters. They expose research-domain contracts and do not add orchestration or HTTP endpoints.

Configure provider credentials in the runtime environment; keys are never included in URLs, logs, or config representations:

| Setting | Required | Default / purpose |
|---|---|---|
| `TAVILY_API_KEY` | Yes to use Tavily | Tavily bearer credential |
| `TavilyConfig.timeout_seconds` | No | HTTP client timeout in seconds (default 30; finite and positive; must exceed extraction timeout when configured) |
| `TavilyConfig.extract_timeout_seconds` | No | Optional provider-side extraction timeout in seconds (unset by default and omitted from requests; range 1–60) |
| `NEBIUS_API_KEY` | Yes to use Nebius | Nebius Token Factory bearer credential |
| `NEBIUS_MODEL` | Yes to use Nebius | Account-enabled model identifier; there is no hard-coded model |
| `NEBIUS_BASE_URL` | No | `https://api.tokenfactory.nebius.com/v1/` |

The clients can be instantiated with a shared `ResearchBudget`. Tavily counts one search per logical cache-miss request and one extract unit for each submitted URL on a cache-miss batch (up to 20 URLs per provider request). Nebius counts every HTTP request, including HTTP retries and structured-schema retries. Tokens are recorded from provider usage when available and may overshoot the cap on the final call; no subsequent request is allowed once the token cap is exhausted. `BudgetExceededError` is intended to map to HTTP 429 when an API layer is added. Provider response caches live only on each client instance and are therefore scoped to that run.

The provider base URLs are host-allowlisted: Tavily defaults to `api.tavily.com`, and Nebius defaults to `api.tokenfactory.nebius.com`. A custom `NEBIUS_BASE_URL` or Tavily base URL is rejected unless its host is explicitly added to the corresponding `allowed_hosts` config in code. The Tavily adapter validates extraction URLs before asking Tavily to fetch them: it rejects userinfo, non-default ports, localhost/internal names, non-global IP literals, and hostnames resolving to any non-global address. The DNS check protects this request and any later direct-fetch implementation; Tavily itself performs the provider-side fetch. Evidence is untrusted, wrapped in nonce-delimited markers, and marker-like source text is neutralized. Nebius uses fixed system prompts, sends no tools, and sends keys only in the `Authorization` header.

### Verified vs assumed

Provider request and response details below were checked against official provider documentation on **18 October 2026**. This is a documentation verification, not a live credential or account test.

| Provider | Verified from official docs | Not verified live / account-dependent |
|---|---|---|
| Tavily | Base `https://api.tavily.com`; POST `/search` and `/extract`; bearer auth; search depths `advanced`, `basic`, `fast`, `ultra-fast`; topics `general`, `news`, `finance`; extract depths `basic`, `advanced`; `markdown`/`text` formats; search results use `content` and extract results use `raw_content`; extraction partials use `results` plus `failed_results`; `Retry-After` on 429; documented 400, 401, 422, 429, 432, 433, and 500 behavior. | No credentials are available in this environment, so authentication, actual response timing, account limits, and live provider behavior have not been checked. |
| Nebius Token Factory | OpenAI-compatible base `https://api.tokenfactory.nebius.com/v1/` and `/chat/completions`; bearer auth; `model`, `messages`, `temperature`, `max_tokens`, and JSON response formats; response content, refusal, finish reason, and token usage fields; documented 400, 401, 402, 403, 404, 422, 429, 500, 503, and 529 behavior. | No credentials are available, so enabled model availability and actual latency are unknown. JSON-schema enforcement is per model and has not been verified for any account-enabled model. |

Live checks are opt-in and skipped when their corresponding keys (and `NEBIUS_MODEL`) are absent:

```sh
uv run pytest -m live tests/live
```

### IHK businesses (`ihk_businesses`)

Import boundaries first; like population, the IHK import pins the active LOR snapshot unless `--lor-snapshot` is given:

```sh
uv run location-data ingest ihk_businesses
uv run location-data ingest ihk_businesses --file /absolute/path/to/IHKBerlin_Gewerbedaten.csv
```

The download is about 125 MB. The CSV is streamed; the full October 2026 edition peaks at roughly 1.5 GB of resident memory (about 30 seconds on a Devin VM). A local file must have the reviewed SHA-256. Another month's file, such as `2026_09__IHK_Berlin_Gewerbedaten.csv` from the [IHK archive](https://cloud.ihk.berlin/d/62fad06b540745098ab7/), needs a reviewed configuration update first. A Git LFS pointer or HTML page instead of the CSV, a changed header, or a different file hash prevents publication.

**OSM and IHK counts are separate inventories.** `ihk_businesses` counts businesses registered with IHK Berlin; OSM places count mapped venues. A café can appear in both, one, or neither, so the counts must be reported side by side and never added. IHK covers Handelsregister entries and Kleingewerbetreibende only: craft businesses registered only with the Handwerkskammer, freelancers and public bodies are absent. A registration is not proof of an open venue, as the address may be an office or home and branch locations may be missing.

IHK records keep `opendata_id` as `record_id`, the supplied `planungsraum_id` as `area_code` (a string, leading zeros kept), the most specific IHK branch code as `industry_code`, the supplied size class as `employees_label`, and `business_age` as `business_age_label`. Size classes become inclusive bounds (`1 - 3 Beschäftigte` → 1–3; `0 Beschäftigte` → 0–0; `10000 und mehr Beschäftigte` → 10,000 with an open upper bound). `unbekannt`, missing, or unrecognized classes give null bounds and a quality flag. The IHK-supplied PLR code is checked against the pinned LOR snapshot and polygon in EPSG:25833 and reported in the manifest coverage; it is never replaced from coordinates.

Categories come from the versioned mapping [`config/categories/ihk_v1.json`](config/categories/ihk_v1.json). `coffee_shop` means IHK branches `56114` (Cafés) and `561141` (Stehcafés). NACE 56.30 *Ausschank von Getränken* is mapped to `bar` (and `56302` to `nightclub`), because its IHK subclasses are pubs, bars, discos and shisha lounges, not cafés. Records classified only as `56`, `561` or `5611` may be cafés, but stay `food_service_unspecified`. Bakeries, coffee retail, coffee roasting, catering and other hospitality and retail classes are separate categories; the remaining codes stay unmapped (`canonical_category` null).

## Adding a connector

Shared record types live in `app/domain/models.py`: `Area`, `AreaObservation`, `AreaIndicatorClass` (ordinal area-indicator classes), `Place` (mapped POIs), `Business` (registered businesses), `TransitStop`, and `StopService` (scheduled departures in an explicit window). Point geometries are WGS84 GeoJSON points, and snapshots of point records are stored as GeoParquet.

Register an adapter name in `app/connectors/registry.py`; modules load lazily, so a planned adapter that has no module yet fails with `Adapter is not implemented`. A dataset that must be pinned to another snapshot sets `parser_options.parent_dataset`, and its connector implements `check_parent(config, records, parent_records) -> dict`. The returned dict is merged into the manifest coverage. Use `--lor-snapshot` to pin an explicit parent.

## Official LOR names (`berlin_lor_names`, `berlin_wfs`)

`berlin_lor_names` emits 542 `Area` records (hierarchy `planungsraum`, boundary edition `2021`) with `name = plr_name`. The `bzr_id/bzr_name/pgr_id/pgr_name/bez/stadtraum_id/stadtraum_name/stand` values go in `Area.attributes`. `berlin_lor` remains the canonical geometry. Scoring and the UI should take names from this dataset. The import is pinned to a `berlin_lor` snapshot and needs an identical PLR code set:

```sh
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_lor_names --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run location-data ingest berlin_lor_names --file /absolute/path/to/combined-wfs-feature-collection.json --lor-snapshot "$LOR_SNAPSHOT_ID"
NAMES_SNAPSHOT_ID='replace-with-names-snapshot_id'
uv run location-data records berlin_lor_names --snapshot "$NAMES_SNAPSHOT_ID" --area-code 01100101
```

Python callers can use `app.application.area_names.area_names(service, snapshot_id)`, which returns a pinned `{area_code: {"name": ..., **attributes}}` lookup.

In a provider download, each WFS page is stored as a separate raw asset, copied in response order to `raw/<dataset>/<snapshot>/<index>-page-NNNN.json` with its own SHA-256 hash. `--file` accepts one combined FeatureCollection. Geometry differences from the parent snapshot are measured in EPSG:25833 and reported in the manifest coverage as `geometry_comparison`; they never cause a failure.

The provider includes a changing `timeStamp` in every response, so a fresh provider download changes the raw hashes and does not hit the snapshot cache. This is deliberate: raw bytes and their hashes are stored exactly as received. Reimporting the same local files does reuse the cache.

`berlin_wfs` is a generic WFS 2.0 GeoJSON adapter. Area datasets use `record_type: "area"` and their hierarchy/area mappings. The Berlin development-plan dataset uses `record_type: "development_plan"` and a separate plan normalizer with per-layer status, property, and count validation. Road traffic datasets use `record_type: "road_traffic_segment"` and road-specific field and volume mappings. A reviewed dataset configuration specifies `typename`, `page_size`, `max_pages`, optional `max_total_bytes`, `expected_crs`, `source_bbox`, optional `allowed_bbox`, `expected_properties`, `id_field`, optional `id_pattern`, exactly one of `expected_count` or `expected_count_range`, and `record_type` (`area`, `development_plan`, or `road_traffic_segment`). Area options include `field_map`, `attribute_fields`, `hierarchy`, optional `geometry_types`, and optional `parent_dataset` and `geometry_difference_threshold`; development-plan layers use per-layer status, property, and count validation; road options use road-specific field and volume mappings. The adapter retains page assets and validates next links against the reviewed query, apart from `COUNT`/`STARTINDEX`. Additional gdi.berlin layers need a compatible record-type normalizer as well as configuration.

Opt-in live check (requests one feature): `uv run pytest -m live tests/live/test_berlin_wfs_live.py`.

## Berlin retail centers (`berlin_centers`)

`berlin_centers` imports both layers from the Berlin StEP Zentren 2040 WFS,
`step_zen_2040:step_zen_2040_zh` (Zentrenhierarchie) and
`step_zen_2040:step_zen_2040_fma` (Fachmarktagglomerationen). The reviewed source
edition and reference date is `2026-01-13`. The source contains 81 hierarchy features
(79 Points and two MultiPolygons) and 33 specialist-retail Points. Source geometries
are validated in EPSG:25833, transformed to WGS84 GeoJSON for storage, and retain
their feature IDs, source links, mapped categories, and source attributes. Category
semantics are versioned in `config/categories/step_zentren_2040_v1.json`.

Import against a pinned Berlin LOR snapshot so the point and representative-point
locations can be checked for coverage:

```sh
LOR_SNAPSHOT_ID='replace-with-lor-snapshot_id'
uv run location-data ingest berlin_centers --lor-snapshot "$LOR_SNAPSHOT_ID"
CENTERS_SNAPSHOT_ID='replace-with-berlin_centers-snapshot_id'
uv run location-data records berlin_centers --snapshot "$CENTERS_SNAPSHOT_ID" --limit 100
```

The nearby API is
`GET /api/v1/datasets/berlin_centers/nearby?snapshot_id=...&lon=...&lat=...&radius_m=...`.
It returns centers within a straight-line radius (metres). Distances are computed in
EPSG:25833 to the published polygon area (zero when the candidate is inside it) or
to the published center point. Membership is determined only when the candidate is
covered by a published Zentrumsbereich polygon. Point records have no boundary and
are not treated as membership areas. The response labels the result as planning
context, not footfall, revenue, or suitability, and explicitly distinguishes
straight-line distance from walking distance or time.

StEP Zentren 2040 is a planning designation, not a measurement of customer traffic,
sales, rents, vacancy, or shop counts. Only two Zentrumsbereich polygons have
published boundaries; boundaries are not inferred for point features. Parcel-exact
boundaries for central supply areas and Nahversorgungszentren belong to district
center concepts and are not present in this WFS. The `fma` layer describes
complementary sites for large specialist stores with non-center-relevant assortments;
these sites are not städtische Zentren. Missing source names and unmapped source
types remain explicitly missing or flagged. License: [Datenlizenz Deutschland –
Zero – Version 2.0](https://www.govdata.de/dl-de/zero-2-0); attribution to
Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin.

## Berlin bicycle counters (`berlin_bicycle_counts`)

Import the reviewed annual workbook with:

```sh
uv run location-data ingest berlin_bicycle_counts
```

The `mobility_observation` records preserve counter location and direction, explicit
Europe/Berlin local intervals, value status, and interval coverage. Daily totals are
published only when every expected hour is usable; window means require at least 80%
usable coverage. Missing or implausible hours are never filled with zero. The
`/api/v1/datasets/{dataset_id}/layers/counters` endpoint returns pinned counter
points for a selected window and year. These are permanent point counters, not a
citywide cycling estimate, pedestrian-footfall measure, customer count, or revenue
proxy. No LOR join is made; `area_code` remains null.

The source labels timestamps in `Europe/Berlin`; the nonexistent spring DST hour is
excluded, while the repeated autumn hour is flagged because the source's handling is
undocumented. Two other theme-7 markings occur outside the spring gap: a blank row
at `2020-10-26 04:00` and the repeated hour at `2021-10-31 02:00`. Their values are
discarded and counted as missing, with an `unlegended_source_marking` quality flag.
Directional counters report only their stated direction. The source publishes
annual workbook editions and notes that construction, detours, network changes,
replacement transport, and wrong-way cycling can affect counts.

## Berlin development plans (`berlin_planning`)

The connector pages the three WFS layers independently because the provider rejects combined `TYPENAMES` requests: `a_bp_iv` (in procedure), `b_bp_fs` (in force), and `c_bp_ak` (repealed). A complete snapshot requires all three provider layers; single-file `--file` imports are not supported. Procedure statuses are normalized as follows:

| Published status | Normalized status |
|---|---|
| `Im Verfahren`, `Öffentliche Auslegung` | `in_progress` |
| `In Kraft getreten` | `adopted` |
| `Teilweise untergegangen`, `Teilweise außer Kraft`, `Teilweise aufgehoben` | `partially_in_force` |
| `Außer Kraft`, `Aufgehoben` | `repealed` |
| Unknown source value | `unmapped` (original value retained) |

Read-only endpoints are `GET /api/v1/planning/plans/near`, `/api/v1/planning/plans/in-area`, and `/api/v1/planning/plans/{plan_id}`. Geometry is WGS84 GeoJSON; spatial queries use EPSG:25833. Radius membership uses straight-line distance less than or equal to the requested radius, not walking time. Intersection area and area shares are measured against a 64-segment radius disc, so a plan within the true radius can have zero disc intersection area at the polygonal disc edge.

A development plan intersecting or near a location is evidence of a planning procedure only. It is not a claim about building completion, future residents, rental availability, or permitted premises use. Document links are retained only when they are valid HTTP(S) URLs; availability is `not_checked`, and documents are never fetched or extracted. Any future document extraction must be a separate dataset and snapshot referencing the plan `record_id` and source URL.

## Road traffic volumes 2019 (`berlin_traffic_volumes_2019`, `berlin_wfs`)

This dataset emits `RoadTrafficSegment` records with WGS84 `LineString` or
`MultiLineString` geometry. `dtv_motor_vehicles_per_day` and
`vehicle_class_volumes_per_day` are average daily motor-vehicle counts in
vehicles/day; `source_length_m` preserves the source's segment length. A missing
count remains null/omitted and is never filled with zero. Source attributes
include `link_id` and `herkunft`.

The source covers the classified main road network, not every residential
street. It measures motor vehicles only—not pedestrians, cyclists, footfall, or
customer potential. Values are from 2019 and include counts dated 2017–2019;
they are not current traffic. DTV is not split by direction. Straight-line
proximity is not walking time, and a street without a segment has unknown
traffic, not zero. Segments are not assigned to PLRs during ingestion because
they can cross area boundaries.

Use the metric helpers to query proximity and summarize clipped lengths within
area boundaries:

```python
from app.geo.traffic import road_segments_near_point, summarize_road_traffic_by_area

nearby = road_segments_near_point(segments, 13.4132, 52.5219, 300)
by_area = summarize_road_traffic_by_area(segments, planungsraeume)
```

`road_segments_near_point` returns segments within the inclusive radius, sorted
by metric distance then record ID. `summarize_road_traffic_by_area` counts only
positive-length intersections and weights DTV by the clipped metric length of
segments with known volume.

The reviewed full import contains 10,420 of 10,420 `numberMatched` features,
with zero rejected rows and 11 WFS pages. In the measured run it took 10.43 s
(peak RSS 252,812 KB); the raw and normalized snapshot directories occupied
6.4 MiB and 3.0 MiB, respectively, and `records.parquet` was 3,077,663 bytes.
The quality report records the source limitations, DTV-source breakdown,
missing-volume count, and total metric network length. On the imported road
and LOR snapshots, the Alexanderplatz 300 m nearby query (five nearest roads listed)
ran in 0.22 s; the area summary covered all 542 PLRs in 0.91 s (525 with road
segments, 17 without).

## Development checks

```sh
uv run pytest
uv run ruff check app tests
uv run ruff format --check app tests
```

GitHub Actions runs the same checks offline with `uv sync --locked`; tests marked `live` are excluded.

The normal suite runs offline with synthetic fixtures and mocked acquisition. It exercises the implemented connectors, contracts, ingestion failures and snapshot compatibility, API queries, and CLI behavior. It does not require downloading all provider data on each test run. Full official datasets and generated local snapshots are excluded from Git.

## Walking routes and reachable areas (Valhalla)

Walking routes use a self-hosted Valhalla 3.9.1 service and the reviewed, pinned Geofabrik Berlin OSM extract. This is a routing service configuration, not a dataset-registry entry. Build and serve the graph locally:

```sh
backend/routing/build_valhalla_graph.sh ~/valhalla/work /path/to/berlin-261017.osm.pbf
backend/routing/run_valhalla_service.sh ~/valhalla/work/current 8002
```

The build script verifies the PBF SHA-256 from `config/routing/valhalla_berlin_walking.json`, creates a staged graph using the pinned container image, and atomically updates `current` only after a successful build. Omit the local PBF argument to download the pinned source. The service binds to loopback by default and mounts the graph read-only at `/data`. A build needs about 1.7 GB peak RAM and 30 seconds on the checked VM; its tiles archive is about 165 MB. The graph is limited to bbox `[13.0,52.3,13.9,52.8]`; nearby places beyond the extract, including Potsdam, may not snap.

Use the CLI (global options precede the subcommand) or API:

```sh
uv run location-routing --data-dir /tmp/routing status
uv run location-routing route --from 13.4132,52.5219 --to 13.3777,52.5163
uv run location-routing reachable-area --origin 13.4132,52.5219 --minutes 5,10,15

curl --fail http://127.0.0.1:8002/status
curl --fail --get http://127.0.0.1:8000/api/v1/routing/walking/status
curl --fail --get http://127.0.0.1:8000/api/v1/routing/walking/route \
  --data-urlencode 'origin=13.4132,52.5219' \
  --data-urlencode 'destination=13.3777,52.5163'
curl --fail --get http://127.0.0.1:8000/api/v1/routing/walking/reachable-area \
  --data-urlencode 'origin=13.4132,52.5219' \
  --data-urlencode 'minutes=5,10,15'
```

Responses distinguish `measure: network_walking_estimate` (or `network_walking_isochrone_estimate`) from `straight_line`, which is a separately labelled EPSG:25833 distance and never a walking-time estimate. Route outputs include snapped endpoints, snap distances, duration, network distance, a WGS84 LineString, graph/profile provenance, assumptions, quality flags, failure details, and cache status. Reachable-area outputs include ascending time contours with WGS84 Polygon/MultiPolygon geometries and areas measured in EPSG:25833. `ok`, `unreachable`, and `invalid_snap` are valid response statuses; validation and provider failures use stable error codes.

By default, API/CLI routing clients enforce a 500-request budget per fixed one-hour window, and the service limits concurrent route requests to two. Results persist in `LOCATION_DATA_DIR/routing/cache.sqlite` (or `<data-dir>/routing/cache.sqlite`) under keys containing graph version, profile hash, request kind, coordinates rounded to six decimal places, and contours. Rebuilding the graph changes its version and naturally invalidates old cache keys; changing the profile changes its hash. `VALHALLA_BASE_URL` may override the configured service address only when its host passes the explicit allowlist; public demo hosts are rejected.

Set `graph.manifest_path` or `VALHALLA_GRAPH_MANIFEST` to verify the serving graph's pinned source hash, edition, and tileset timestamp; without a manifest, responses are marked `graph_source_unverified`, and mismatches make status unavailable.

The graph uses Geofabrik's Berlin extract (ODbL-1.0, attributed to © OpenStreetMap contributors and Geofabrik GmbH); Valhalla is MIT-licensed. ODbL share-alike conditions may apply to derived data. The pedestrian profile assumes a 5.1 km/h walking speed, no crossing/signal wait and no elevation, follows OSM access/barrier tags as of the extract date, and may include flagged ferry segments. Isochrones are generalized, denoised network approximations; small separate reachable pockets can be omitted. Temporary closures, construction, indoor passages, and step-free accessibility are not modelled. The public Valhalla demo server has a fair-use policy and is not a production backend.

## Exports

Persisted research runs under `runs/<run_id>/` can be exported as a Markdown report, a
self-contained printable HTML report (use **Print / Save as PDF** in a browser), a sources CSV,
or a BibTeX bibliography. The CLI writes to standard output by default or to a file with `-o`:

```sh
uv run location-data export ../runs/demo-coffee --format md
uv run location-data export ../runs/demo-coffee --format html --mode future -o report.html
uv run location-data export ../runs/demo-coffee --format csv -o sources.csv
```

Area reports accept `--layers layer_a,layer_b` and `--mode present|future`; report formats also
accept `--share-url URL`. The API serves the same formats from
`GET /api/v1/runs/{run_id}/export/{fmt}` where `fmt` is `report.md`, `report.html`,
`sources.csv`, or `sources.bib`. Set `RISALITE_RUNS_DIR` to select the run store; it defaults to
the repository-root `runs/` directory. API filters `layers` and `mode` customize area findings.
