# Valhalla fixtures

Recorded from the locally self-hosted Valhalla 3.9.1 service on 2026-10-18. The
status response reports engine version `3.9.1-7fd2228e5` and tileset timestamp
`1792333178`; the reviewed graph version is `valhalla-5fa8cb119afde520`.
Source graph: Geofabrik Berlin OSM extract, edition `2026-10-17`, SHA-256
`3ac2970b662434290d9c42f3bd98c6be1b57b20f697533c99a28124be2e52cf6`.

The recorded route request body uses `costing: "pedestrian"`,
`costing_options: {"pedestrian":{"walking_speed":5.1,"use_ferry":0.0}}`,
`units: "kilometers"`, `directions_type: "none"`, and two locations with
`search_cutoff: 150`, `minimum_reachability: 50`, and `radius: 0`.
`route_ok.json` records `(13.4132,52.5219)` to `(13.3777,52.5163)`;
`route_ferry.json` records the Pfaueninsel pair `(13.12,52.433)` to
`(13.1557,52.4311)`; `route_error_171.json` records the nearby out-of-extract
route `(13.06,52.40)` to `(13.061,52.401)`. `route_error_442.json` uses the
central route request plus an `exclude_polygons` ring around the destination to
simulate a barrier. The walking client never sends `exclude_polygons`.

The isochrone body uses the same pedestrian costing options, `polygons: true`,
`denoise: 1.0`, `generalize: 10`, `show_locations: true`, and locations with
`minimum_reachability: 50`, `radius: 0`. `isochrone_ok.json` records the central
origin with contours 5, 10, and 15 minutes; `isochrone_unsnapped.json` records
`(13.06,52.40)` with a 10-minute contour. The latter's nearby synthetic-looking
network snap is outside the configured 150 m limit and is rejected by the
adapter.

Fixtures retain the provider's response JSON. OSM-derived graph data is
attributed to © OpenStreetMap contributors, ODbL 1.0, Berlin extract by
Geofabrik GmbH; see <https://www.openstreetmap.org/copyright> and
<https://opendatacommons.org/licenses/odbl/1-0/>. Synthetic error-code 170 and
HTTP-200 HTML cases are constructed in contract tests and are labelled there.
