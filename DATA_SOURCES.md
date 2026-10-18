# Free Data Sources — AI Location Intelligence

Research date: **18 October 2026**

The Berlin MVP can be built entirely from free public datasets. Start with OpenStreetMap, Berlin population data, neighborhood boundaries, IHK business data, and VBB schedules.

This is a practical inventory for the project. Provider documentation and dataset listings were checked; individual downloads still need integration testing. It covers the most useful accessible sources rather than claiming an exhaustive list of every Berlin dataset. Free dataset access does not include application hosting, inference, or research-tool costs.

## 1. Best Sources to Integrate First

| Source | What we get | Free access method | Use in our project |
|---|---|---|---|
| **[OpenStreetMap / Overpass](https://dev.overpass-api.de/overpass-doc/en/)** | Cafés, restaurants, shops, gyms, amenities, streets, and walking paths | Public query API; no key | Find competitors, complementary businesses, and walking connections |
| **[Geofabrik Berlin extract](https://download.geofabrik.de/europe/germany/berlin.html)** | Downloadable Berlin OSM data | PBF, Shapefile, GeoPackage; no account | Cache the city locally and avoid repeated Overpass queries |
| **[Berlin population by LOR](https://www.statistik-berlin-brandenburg.de/a-i-16-hj/)** | Residents and age structure for small planning areas | Published tables and downloads; CSV also listed in Berlin Open Data | Estimate resident demand and the number of young children |
| **[LOR neighborhood boundaries](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/sozialraumorientierte-planungsgrundlagen/lebensweltlich-orientierte-raeume/)** | Planning-area polygons and identifiers | Shapefile download | Join population, businesses, and other metrics geographically |
| **[IHK Berlin business data](https://github.com/IHKBerlin/IHKBerlin_Gewerbedaten)** | Anonymized business records with coordinates, industry, employee size class, business age, and LOR identifiers | Monthly CSV downloads; no account; CC0 | Strengthen competition analysis and estimate concentrations of workplaces |
| **[VBB GTFS schedules](https://unternehmen.vbb.de/digitale-services/datensaetze/)** | Transit stops, routes, trips, and schedules | ZIP containing text tables; no key; CC BY 4.0 | Calculate stop proximity and scheduled service frequency |
| **[Berlin kindergarten locations](https://daten.berlin.de/datensaetze/kindertagesstatten-wfs-d03b94d2)** | Locations and information about publicly funded kindergartens | Public WFS service; DL-DE-Zero 2.0 | Map existing childcare provision |
| **[Berlin household statistics](https://www.statistik-berlin-brandenburg.de/a-i-17-j/)** | Household size and family structures by LOR | Published tables and downloads | Identify family-oriented neighborhoods |
| **[Official Berlin address points](https://daten.berlin.de/datensaetze/adressen-berlin-wfs-634ab8ba)** | Addresses with coordinates | Public WFS service; DL-DE-Zero 2.0 | Locate candidate addresses without a paid geocoding API |

### Why IHK Business Data Matters

IHK business data is a strong addition to the original project brief. Its industry categories, coordinates, and employee size classes complement OSM well. Employee classes provide a rough workplace-demand signal; they do not measure actual daily attendance. The publisher documents monthly updates and the available fields in its [repository](https://github.com/IHKBerlin/IHKBerlin_Gewerbedaten).

## 2. Additional Free Datasets Worth Using

WFS is a standard API for downloading geographic features and their attributes. WMS provides rendered map layers, so it is generally less convenient for scoring.

| Source | Available information and access | Useful application / limitation |
|---|---|---|
| **[Zensus 2022](https://www.destatis.de/DE/Themen/Gesellschaft-Umwelt/Bevoelkerung/Zensus2022/_inhalt.html)** | Downloadable geographic grid data, including population and residential rent indicators; selected datasets reach 100 m resolution | More detailed population distribution. It is a historical snapshot; residential rent does not establish commercial rent |
| **[Monitoring Soziale Stadtentwicklung 2025](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/monitoring-soziale-stadtentwicklung/bericht-2025/)** | Neighborhood social indicators, tables, maps, and documentation | Understand social conditions and service needs; these indicators do not directly measure purchasing power |
| **[Population forecast 2024–2040](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/bevoelkerungsprognose-2024-2040/)** | Small-area XLSX forecasts, including age groups | Identify expected growth or changing childcare demand; keep forecasts separate from observed counts |
| **[StEP Zentren 2040](https://daten.berlin.de/datensaetze/stadtentwicklungsplan-step-zentren-2040-wfs-7eb416eb)** | Retail-center hierarchy and planning geography through WFS; DL-DE-Zero 2.0 | Identify established commercial centers |
| **[Development-plan procedures](https://daten.berlin.de/datensaetze/bebauungsplanverfahren-in-berlin-wfs-4f574ea7)** | Plan boundaries and procedural status through WFS; DL-DE-Zero 2.0 | Flag planned changes around candidates; boundaries alone do not establish permitted premises use |
| **[Berlin land-use plan](https://www.govdata.de/suche/daten/fnp-flachennutzungsplan-berlin)** | Land-use planning data, including a WFS download service | Understand residential, commercial, and other planned land uses |
| **[StEP Wohnen 2040](https://www.berlin.de/sen/stadtentwicklung/planung/stadtentwicklungsplaene/step-wohnen-2040/)** | Housing-development potential, new neighborhoods, downloadable reports and maps | Find potential future demand; development potential does not guarantee delivery |
| **[School locations and catchments](https://daten.berlin.de/datensaetze/schulen-wfs-ebc64e18)** | School type, main locations, and enrollment areas through WFS; DL-DE-Zero 2.0 | Identify education clusters and family-service context |
| **[Parks and public playgrounds](https://gdi.berlin.de/data/gruenanlagen/docs/Datenformatbeschreibung_Gruenanlagen_Spielplaetze.pdf)** | Geographic features through the documented Berlin WFS dataset | Evaluate nearby outdoor amenities and family-oriented surroundings |
| **[Hospital locations](https://daten.berlin.de/datensaetze/krankenhauser-wfs-71ad834d)** | Coordinates, addresses, and bed information through WFS; DL-DE-Zero 2.0 | Identify healthcare clusters and possible staff/visitor demand |
| **[Berlin bicycle counts](https://daten.berlin.de/datensaetze/radzahldaten-in-berlin)** | Observations from permanent counters, including downloadable historical data; DL-DE-Zero 2.0 | Measure cycling activity at covered locations; coverage is limited to counters |
| **[Road traffic volumes](https://www.berlin.de/sen/uvk/mobilitaet-und-verkehr/verkehrsmanagement/verkehrserhebungen/)** | Published traffic-volume maps and associated geographic data | Assess exposure to busy roads, traffic, and possible nuisance |
| **[Environmental Justice Atlas 2023/2024](https://www.berlin.de/sen/uvk/umwelt/nachhaltigkeit/umweltgerechtigkeit/umweltgerechtigkeitsatlas/)** | Noise, air pollution, green-space provision, thermal stress, and combined burden indicators; geodata services | Add environmental risks, especially for childcare |
| **[BORIS / land values](https://daten.berlin.de/datensaetze/bodenrichtwerte-01-01-2026-wms-fd3aa40c)** | Standard land-value zones; free WMS layer under DL-DE-Zero 2.0 | Provide property-market context; these values do not establish shop rents |
| **[Tourism statistics](https://www.statistik-berlin-brandenburg.de/wirtschaft/wirtschaftsbereiche/tourismus-und-gastgewerbe/)** | Arrivals, overnight stays, and accommodation capacity in published tables | Add tourism context; district totals cannot establish street-level footfall |
| **[Accident Atlas](https://www.destatis.de/DE/Service/OpenData/karten-geodaten.html)** | Accident locations downloadable as CSV or Shapefile | Evaluate recorded road-safety risks near childcare candidates |
| **[DWD Climate Data Center](https://www.dwd.de/EN/ourservices/cdc/cdc.html?lsbId=646268)** | Free station observations and climate grids through direct downloads | Optional weather and outdoor-seating context |

## 3. Other Sources for Enrichment and Verification

| Source | Access | Role |
|---|---|---|
| **[Overture Maps](https://docs.overturemaps.org/getting-data/)** | Browser export, Python client, or GeoParquet queries; no account needed for Explorer | Additional places and buildings. Deduplicate against OSM and retain source-specific [licensing information](https://docs.overturemaps.org/attribution/) |
| **[VBB real-time feed](https://production.gtfsrt.vbb.de/)** | No authentication; CC BY 4.0; up to 60 requests/minute | Optional live disruption context. At the research date, the provider reported incomplete coverage; recheck before integration |
| **[District retail and center studies](https://www.berlin.de/sen/stadtentwicklung/planung/stadtentwicklungsplaene/step-zentren-2040/)** | Publicly readable reports linked by district | Research local retail conditions and development priorities; extraction requires more work |
| **[Kita-Navigator](https://www.berlin.de/sen/jugend/familie-und-kinder/kindertagesbetreuung/kita-navigator/)** | Public search interface | Check individual facilities. A supported bulk API or reusable real-time vacancy feed was not established during this research |

District studies and Kita-Navigator are useful research references; free viewing alone does not establish unrestricted bulk reuse.

## 4. Access and Licensing Details

- **Download and cache for the demo.** Public Overpass servers share capacity and apply load limits; Geofabrik is the better option for a local citywide snapshot. See the [Overpass usage guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html).
- **Keep neighborhood versions consistent.** Berlin changed its LOR geography in 2021; older data cannot always be joined directly to current boundaries. See the [official LOR documentation](https://www.berlin.de/sen/stadt/stadtdaten/stadtwissen/sozialraumorientierte-planungsgrundlagen/lebensweltlich-orientierte-raeume/).
- **Record licenses per dataset.** IHK is CC0, VBB is CC BY 4.0, and many Berlin geographic layers use DL-DE-Zero 2.0. OSM requires attribution and compliance with its [ODbL license](https://www.openstreetmap.org/copyright).
- **Treat portal links as discovery links.** One population CSV link returned HTML during checking; use the publisher's current download pages and validate the response before importing.
- **Preserve provenance.** Store the source URL, dataset edition or reference date, retrieval date, and license alongside imported data so recommendations can cite their evidence.

## 5. Remaining Data Gaps

The research did not identify an easy, comprehensive, openly reusable source for:

- Current commercial rents.
- Available commercial premises.
- Street-by-street pedestrian footfall.
- Verified live kindergarten vacancies.

Proxies must be labeled explicitly:

- Transit frequency measures service availability, not passenger counts or pedestrian footfall.
- Bicycle counters measure bicycles at covered locations, not all passing customers.
- Kindergarten counts measure facilities, not available childcare places.
- Residential rents and land values do not establish commercial rents.
- Business employee size classes do not measure actual daily workplace attendance.

## 6. Recommended Integration Order

1. **Coffee shop MVP:** OSM + IHK + population + LOR boundaries + VBB GTFS.
2. **Kindergarten use case:** Add the kindergarten registry, child-age counts, and household data.
3. **Verification and richer recommendations:** Add development plans, housing-growth context, environmental risks, and targeted research into shortlisted locations.

This provides a free data foundation for both demo scenarios while keeping uncertain assumptions visible.

## 7. First Implementation Validation

The [backend setup guide](backend/README.md) documents the two working connectors and their reviewed download resources. On 18 October 2026, a full download-to-publication run imported 542 LOR planning-area polygons and 4,878 population observations (542 areas, one total plus eight age bands) from the official sources without rejected rows. Population refers to 30 June 2026; the selected boundary archive is the 1 January 2023 release of the 2021 LOR scheme. All area codes matched; code matching alone does not certify unchanged geometry between editions.

The official Kita WFS (`kita:kita`, revision 31 December 2025, DL-DE-Zero 2.0) was imported on 18 October 2026 as `berlin_kitas`: 2,905 publicly funded facilities (equal to `numberMatched`), no rejected rows, all assigned to a Planungsraum. Its `e_platz` field is the licensed maximum number of places (missing for 22 facilities); the source provides no vacancies, and facility counts are not evidence of a childcare shortage.

The selected LOR archive is licensed CC BY 3.0 and supplies IDs without neighborhood names. The population workbook is licensed CC BY 3.0 DE. These source-specific licenses take precedence over general assumptions about Berlin Open Data licensing. Exact URLs, mappings, attribution, and reference dates are in the reviewed files under `backend/config/datasets/`.
