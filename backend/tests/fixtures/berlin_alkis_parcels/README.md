# ALKIS parcel WFS contract fixture

These two six-feature GeoJSON pages were extracted from `/tmp/kx.json`, a real
Berlin ALKIS WFS response for the reviewed EPSG:25833 candidate extent
`[387500, 5816500, 392500, 5821500]`. The fixture keeps the original feature
properties and geometries; `numberMatched` and the first page's `next` link are
set to 12 to represent this deliberately small offline sample. The sample is
for parser contracts only, not a complete source snapshot.

Source layer: `alkis_flurstuecke:flurstuecke`; service
`https://gdi.berlin.de/services/wfs/alkis_flurstuecke`. Properties and source
facts were reviewed on 2026-10-18. See `backend/config/datasets/berlin_alkis_parcels.json`
and the repository's `DATA_SOURCES.md` for license and provenance.
