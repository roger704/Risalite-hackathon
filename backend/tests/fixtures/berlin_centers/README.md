# Recorded StEP Zentren 2040 WFS fixtures

Source: `https://gdi.berlin.de/services/wfs/step_zen_2040`  
Retrieval date: 2026-10-18  
License: Datenlizenz Deutschland – Zero – Version 2.0 (DL-DE-Zero-2.0).

These recorded GeoJSON subsets were selected from the provider responses in
`/tmp/zh.json` and `/tmp/fma.json`. They retain the source CRS, properties, feature
IDs, and geometries without altering coordinates. The Zentrenhierarchie (`zh`)
fixtures contain Z4001 (Zentrumsbereichskern), Z4058 (Ortsteilzentrum), and the
complete Z40F1 MultiPolygon; they are split across two pages with the WFS next-link
shape and `numberMatched` set to the three fixture features. The
Fachmarktagglomerationen (`fma`) fixture contains F4001 and F4002 on one page with
`numberMatched` set to two. Provider-wide metadata and all unselected features were
trimmed; the polygon geometry is retained as recorded.
