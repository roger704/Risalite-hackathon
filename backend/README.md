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

The records response includes reference and retrieval dates, coverage, warnings, attribution, and pagination. Availability and freshness are separate. Freshness remains `unknown` unless an explicit refresh policy has been configured; a successful download alone does not establish current demographic conditions.

## Validated sources and meaning

Source resources and mappings were checked on **18 October 2026**. Offline fixtures are small subsets of reviewed provider responses and contain no derived research findings.

| Dataset | Reviewed resource | Validation result and limitations |
|---|---|---|
| `berlin_lor` | [Official LOR page](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/sozialraumorientierte-planungsgrundlagen/lebensweltlich-orientierte-raeume/), [download archive](https://www.berlin.de/sen/stadt/_assets/stadtdaten/stadtwissen/lebensweltlich-orientierte-raeume/lor_2021-01-01_k3_shapefiles_nur_id.7z?ts=1780323163) | 542 Planungsräume; reviewed member `LOR_2023-01-01_PLR_EPSG_25833_nur_ID.shp`, source CRS EPSG:25833. Boundary scheme `2021`, actual geometry release `2023-01-01`. The archive provides IDs, not neighborhood names; output names are explicit code-based fallbacks. Attribution: Amt für Statistik Berlin-Brandenburg, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). |
| `berlin_lor_names` | [Official LOR WFS metadata](https://daten.berlin.de/datensaetze/lebensweltlich-orientierte-raume-lor-01-01-2021-wfs-34c86848), WFS `lor_2021:a_lor_plr_2021` | 542 Planungsräume, official `plr_name` values and related LOR attributes; source CRS EPSG:25833, WFS 2.0 GeoJSON paging (200 features/page). Licensed [CC BY 3.0 DE](https://creativecommons.org/licenses/by/3.0/de/), attributed to Amt für Statistik Berlin-Brandenburg. The WFS layer is stand 01.01.2021, while `berlin_lor` geometry is the 2023-01-01 release of the same 2021 scheme; code sets must match exactly, geometry differences are reported and not reconciled. Names are official LOR names, not marketing neighborhood names. |
| `ihk_businesses` | [IHKBerlin_Gewerbedaten](https://github.com/IHKBerlin/IHKBerlin_Gewerbedaten), [CSV at commit `75957615`](https://media.githubusercontent.com/media/IHKBerlin/IHKBerlin_Gewerbedaten/75957615265716ec8549d04d63ed76bd6d6c547a/data/IHKBerlin_Gewerbedaten.csv) | Source edition `2026-10` (update of 1 October 2026), 125,464,561 bytes, SHA-256 `64456f92…8181e`. UTF-8 with BOM, comma-delimited, 20 columns, `NULL` for missing values. 366,374 rows; 366,368 published businesses; 6 rows without valid coordinates excluded and listed in the quality report. 1,343 `coffee_shop` records under mapping `ihk_v1`. Supplied PLR codes: 11 missing, 0 unknown to LOR, 1 point outside its supplied polygon. Attribution: IHK Berlin, [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). |
| `berlin_population` | [Official population page](https://www.statistik-berlin-brandenburg.de/a-i-16-hj/), [June 2026 workbook](https://download.statistik-berlin-brandenburg.de/fca0a030166493d0/1289b0921aa2/SB_A01-16-00_2026h01_BE.xlsx), worksheet `T2` | Reference date **30 June 2026**, source edition `2026h01`: 542 areas and 4,878 observations, consisting of one total plus eight age bands per area. The checked population totals sum to 3,917,196 registered residents. The workbook's imprint provides the source legend and [CC BY 3.0 DE license](https://creativecommons.org/licenses/by/3.0/de/). |
| `berlin_planning` | [Berlin development-plan WFS](https://daten.berlin.de/datensaetze/bebauungsplanverfahren-in-berlin-wfs-4f574ea7), `bplan:a_bp_iv`, `bplan:b_bp_fs`, `bplan:c_bp_ak` | Separate page streams for plans in procedure, in force, and repealed; Polygon/MultiPolygon geometries originate in EPSG:25833. Statuses are mapped to a small procedure vocabulary; URLs are retained without fetching documents. A spatial match is evidence of a planning procedure, not a claim about construction or use. Attribution: Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin, [DL-DE-ZERO-2.0](https://www.govdata.de/dl-de/zero-2-0). |
| `osm_pois` | [Geofabrik Berlin page](https://download.geofabrik.de/europe/germany/berlin.html), [pinned Berlin extract](https://download.geofabrik.de/europe/germany/berlin-261017.osm.pbf) | `berlin-latest.osm.pbf` redirected to the dated `berlin-261017.osm.pbf` file on 18 October 2026. The 99,794,715-byte file header reports replication timestamp `2026-10-17T20:20:51Z` (source edition `2026-10-17`). A real import produced 11,837 places within bbox `[13.0, 52.3, 13.9, 52.8]`: 2,666 `coffee_shop`, 71 `coffee_retail`, 1,262 `bakery`, 2,877 `fast_food`, 4,799 `restaurant`, 148 `coworking`, and 14 unmapped; 11,112 nodes, 724 way polygons, and 1 relation multipolygon, with no assembly failures or unlocatable elements. The configured minimum is 1,800 coffee shops (70% of the observed count, rounded down to the nearest hundred). OSM completeness and operating status are not guaranteed. Attribution: © OpenStreetMap contributors, ODbL 1.0, Berlin extract by Geofabrik GmbH. |
| `osm_pois_overpass` | [Overpass API](https://overpass-api.de/api/interpreter) | Optional pinned query for bbox `[52.520, 13.390, 52.540, 13.420]`; this is a small-area refresh only. The endpoint was unreachable or returned HTTP 500 during the 18 October 2026 check. A response with an Overpass `remark` or without `elements` is rejected as incomplete. |
| `vbb_gtfs` | [VBB datasets page](https://unternehmen.vbb.de/digitale-services/datensaetze/), [GTFS resource](https://www.vbb.de/gtfs) | Reviewed feed edition `20261015-20261212`, validity 2026-10-15–2026-12-12. GTFS stops and scheduled departures are summarized by station and local time window; see the counting rules below. Attribution: Verkehrsverbund Berlin-Brandenburg GmbH (VBB), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). |

All 542 population codes matched the reviewed LOR code set, and each complete age-band breakdown matched its area total. The population code directory states December 2021. Matching codes establishes identifier compatibility; it does not certify that polygon boundaries were unchanged between source editions. The manifest pins the actual LOR snapshot used by each population import.

`reference_period` describes when the population was measured; `retrieved_at` records when this backend acquired the source. These dates must not be substituted for one another. Population measures registered principal-residence inhabitants, not workplace attendance, footfall, customer demand, or childcare vacancies.

Age intervals have an inclusive lower and **exclusive upper** bound: `[0, 6)`, `[6, 15)`, `[15, 18)`, `[18, 27)`, `[27, 45)`, `[45, 55)`, `[55, 65)`, and `[65, ∞)`. `population_total` and `population_age_band` are separate metrics. Adding a total to its age bands double-counts people.

The connector distinguishes numeric zero from suppressed, missing, and not-applicable values. The official legend's `–` means no quantity and becomes zero; `•` becomes a null suppressed value; `…` and `/` become null missing values; `x` becomes null not applicable. Original markers remain in quality flags. Unrecognized annotations fail validation instead of becoming zero. Explicit CSV settings are supported for reviewed alternative configurations, but the default official import uses XLSX.

HTML returned instead of data, changed headers, invalid identifiers, duplicate observations, overlapping age-band mappings, incomplete area coverage, and inconsistent age totals prevent publication. The general [connector blueprint](../CONNECTOR_BLUEPRINT.md) describes later connectors and the wider architecture; it is not a list of already implemented capabilities.

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

Shared record types live in `app/domain/models.py`: `Area`, `AreaObservation`, `Place` (mapped POIs), `Business` (registered businesses), `TransitStop`, and `StopService` (scheduled departures in an explicit window). Point geometries are WGS84 GeoJSON points, and snapshots of point records are stored as GeoParquet.

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

`berlin_wfs` is a generic WFS 2.0 GeoJSON adapter. Area datasets use `record_type: "area"` and their hierarchy/area mappings. The Berlin development-plan dataset uses `record_type: "development_plan"` and a separate plan normalizer with per-layer status, property, and count validation. Both retain page assets and validate next links against the reviewed query, apart from `COUNT`/`STARTINDEX`.

Opt-in live check (requests one feature): `uv run pytest -m live tests/live/test_berlin_wfs_live.py`.

### Berlin development plans (`berlin_planning`)

The connector pages the three WFS layers independently because the provider rejects combined `TYPENAMES` requests: `a_bp_iv` (in procedure), `b_bp_fs` (in force), and `c_bp_ak` (repealed). Procedure statuses are normalized as follows:

| Published status | Normalized status |
|---|---|
| `Im Verfahren`, `Öffentliche Auslegung` | `in_progress` |
| `In Kraft getreten` | `adopted` |
| `Teilweise untergegangen`, `Teilweise außer Kraft`, `Teilweise aufgehoben` | `partially_in_force` |
| `Außer Kraft`, `Aufgehoben` | `repealed` |
| Unknown source value | `unmapped` (original value retained) |

Read-only endpoints are `GET /api/v1/planning/plans/near`, `/api/v1/planning/plans/in-area`, and `/api/v1/planning/plans/{plan_id}`. Geometry is WGS84 GeoJSON; area and radius measurements are projected to EPSG:25833 and distances are straight-line, not walking time.

A development plan intersecting or near a location is evidence of a planning procedure only. It is not a claim about building completion, future residents, rental availability, or permitted premises use. Document links are retained only when they are valid HTTP(S) URLs; availability is `not_checked`, and documents are never fetched or extracted. Any future document extraction must be a separate dataset and snapshot referencing the plan `record_id` and source URL.

## Development checks

```sh
uv run pytest
uv run ruff check app tests
uv run ruff format --check app tests
```

GitHub Actions runs the same checks offline with `uv sync --locked`; tests marked `live` are excluded.

The normal suite runs offline with synthetic fixtures and mocked acquisition. It exercises the implemented connectors, contracts, ingestion failures and snapshot compatibility, API queries, and CLI behavior. It does not require downloading all provider data on each test run. Full official datasets and generated local snapshots are excluded from Git.

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
