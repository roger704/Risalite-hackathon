# Synthetic Zensus grid fixture

`population.csv` contains nine synthetic rows using the reviewed 100 m CSV header,
semicolon delimiter, INSPIRE ID format, and cell-centre convention. The IDs are
placed in and around the synthetic polygons in `../lor/areas.geojson` after
projection to EPSG:3035. The numeric values and sentinel examples are fabricated;
they do not describe actual population. Contract and integration tests generate
the ZIP and XLSX description workbook at runtime to avoid committing source data.
