# Household workbook test data

The contract and ingestion tests generate small XLSX workbooks in memory and write them under pytest's temporary directory. Every cell value is synthetic and is not a Berlin statistic; no provider workbook is committed.

The table layout and reviewed mappings come from the official Amt für Statistik Berlin-Brandenburg A I 17 – j / 25 workbook for the reference date 31 December 2025, linked from <https://www.statistik-berlin-brandenburg.de/a-i-17-j/>. The source resource and metric mappings are recorded in `backend/config/datasets/berlin_households.json`.
