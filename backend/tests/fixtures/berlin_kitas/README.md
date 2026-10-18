# Synthetic Berlin Kita WFS fixtures

Small synthetic GeoJSON pages shaped like the official `kita:kita` WFS layer
(EPSG:25833 points, `e_nr` IDs, string properties, `links[rel=next]` paging). The
facilities, names, providers and coordinates are invented and do not reproduce
official records. Points fall inside the synthetic LOR areas in `../lor/areas.geojson`.

`edition-metadata.json` is a trimmed, CKAN-shaped `package_show` response carrying only the
fields the edition check reads.
