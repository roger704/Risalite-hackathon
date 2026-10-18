# Synthetic IHK fixtures

All files here were written for this repository. They are **not** IHK Berlin data and assert nothing about real businesses or places. Codes and labels copy the format of the reviewed October 2026 file (UTF-8 with BOM, comma delimiter, CRLF, `NULL` for missing values). Coordinates fall into the synthetic polygons of `../lor/areas.geojson`.

`businesses.csv` (10 rows) covers:

| opendata_id | Case |
|---|---|
| 1001 | Café (`56114`) in leading-zero PLR `01011101`, `1 - 3 Beschäftigte` |
| 1002 | Stehcafe (`561141`), `unbekannt` employees, `NULL` business age |
| 1003 | Unmapped industry code (`62100`), open size class `10000 und mehr Beschäftigte` |
| 1004 | Bar (`56301`), missing (`NULL`) employees range |
| 1005 | Invalid (`NULL`) coordinates: excluded and reported |
| 1006 | Supplied PLR `01011101`, but the point lies in `01011102` |
| 1007 | Point outside Berlin, `NULL` LOR fields |
| 1008 | `NULL` industry codes |
| 1009 | PLR code `09999999` that the LOR snapshot does not contain |
| 1010 | Ambiguous `5611` code and an unrecognised employees label |

`lfs_pointer.csv` is a Git LFS pointer (what `raw.githubusercontent.com` serves for the LFS-tracked file). `html_response.csv` is an HTML error page saved with a `.csv` name. Both must fail before parsing.
