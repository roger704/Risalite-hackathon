# Demo fixtures

`cells.geojson` contains real LOR 2021 Planungsraum geometry from the Berlin WFS,
attributed to Amt für Statistik Berlin-Brandenburg and licensed CC BY 3.0.
All other fixture content is synthetic demo data. Rebuild offline with
`cd backend && uv run python ../frontend/fixtures/build_fixtures.py --geojson-from ../frontend/fixtures/cells.geojson`.
Pass another cached WFS FeatureCollection with `--geojson-from PATH` if needed.
