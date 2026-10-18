# Demo fixtures

`cells.geojson` contains real LOR 2021 Planungsraum geometry from the Berlin WFS,
attributed to Amt für Statistik Berlin-Brandenburg and licensed CC BY 3.0.
All other fixture content is synthetic demo data ported from the mockup.
Rebuild with `cd backend && uv run python ../frontend/fixtures/build_fixtures.py`;
use `--geojson-from PATH` to supply a cached WFS FeatureCollection offline.
