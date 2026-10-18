# Leaflet vendor assets

Version: 1.9.4, downloaded from the Leaflet 1.9.4 distribution on unpkg.

SHA-384 SRI for the vendored files:

- `leaflet.js`: `sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH`
- `leaflet.css`: `sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H`

The [Leaflet 1.9.4 download documentation](https://leafletjs.com/download)
publishes SHA-256 CDN SRI (it does not publish SHA-384): JavaScript
`sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=` and CSS
`sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=`. Those published hashes
were checked against the vendored files; the SHA-384 values above are computed
from the same downloaded bytes.
