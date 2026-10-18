# Risalite dashboard mockup

Clickable, self-contained mockup of the S3 frontend described in `docs/01_Architecture/Risalite_Spec.md` and `docs/02_State/Execution_Plan.md`. No build step. The 3D map needs network access (MapLibre GL JS from unpkg, OpenFreeMap vector tiles, Berlin orthophoto WMS); offline or without WebGL it falls back to a flat SVG map.

```sh
python3 -m http.server 8765 --directory frontend/mockup
# open http://localhost:8765
```

You can also open `index.html` directly in a browser.

**Covers:**

- Free-text question, optional role and operator profile. The `point` / `location` / `gap` classification is keyword-based, with a stated reason. An unspecified area triggers a clarifying question. Demo data exists only for cafés (location), Kitas (gap) and the Wiener Straße 12 storefront (point); other subjects or addresses show a note and fall back to that example. Operator fit is scored neutral unless the profile matches the demo bakery profile.
- Plan approval: sub-questions with their source class, layers with default weights, and paid sources marked "key not provided — skipped" (the key field is disabled; the mockup makes no provider calls). Unticked sub-questions, layers and hypotheses are dropped from the trace and results, and shown as "not approved" in the dashboard.
- Glass UI (translucent frosted panels, segmented step indicator Ask → Plan → Research → Results). Emoji icons replace most labels; secondary detail (per-cell evidence, sources, trace, self-grade) sits in expandable sections, and explanations are tooltips. Views cross-fade with a directional slide/blur and staggered cards; ranked cells re-order with a FLIP animation when layers or the future mode change. Honors `prefers-reduced-motion`.
- Live investigation view, which replays a mock `trace.jsonl` (stages, *why*, model, tokens, seconds; the skeptic's rescore; the fact-checker's rejection and the replan).
- Shared dashboard skeleton (sections 1–7 of the spec) for all three templates:
  - **T2 coffee in Kreuzberg / T3 Kita gap in Neukölln:** summary with `[n]` citations ("default mix"), top-3 cells, Location Battle, PLR heatmap with hover values, and a layer panel. The panel has checkboxes and a Present / Present + future toggle, and recomputes score, heatmap and top-3 in the browser.
  - **3D heat map (T2/T3):** MapLibre GL JS 4.7 with a tilted camera over OpenFreeMap vector tiles. The PLR cells are coloured by score; when zoomed in (z ≥ 14), OSM building extrusions take their cell's colour. A Map / Satellite switch swaps in Berlin's TrueDOP 2024 orthophoto (`gdi.berlin.de/services/wms/truedop_2024`, no key). There is also a 3D / 2D switch and a reset view. The layer panel floats over the map as a glass card. Hovering a layer previews that layer alone, and each row shows its weight, direction and a histogram. Overlay toggles cover top-3 pins, future developments (★, shown in Present + future), risks (⚠) and unknowns (?), live OSM cafés or Kitas, and U-/S-Bahn stations. Hovering a cell shows its layer values with citations; clicking it highlights the cell in the findings.
  - **T1 storefront (LocalLens):** top-3 hypotheses, each with six scored dimensions and FOR / AGAINST / UNKNOWNS lists, plus a Hypothesis Battle and a map of the space with competitors within 500 m.
- Sources (method, tier, confidence, verified/partial), a "could not verify" appendix, a model-per-role table and a self-grade.

**Data:** PLR boundaries are real: `lor_2021:a_lor_plr_2021` from the gdi.berlin.de WFS, simplified, Amt für Statistik Berlin-Brandenburg, CC BY 3.0. Basemap © OpenMapTiles / © OpenStreetMap contributors (ODbL), via OpenFreeMap. Orthophoto © GeoBasis-DE/SenSBW, dl-de/zero-2-0. All layer values, quotes, URLs under `example-*.de`, scores and traces are **synthetic**. Their shapes follow `cells.json`, `candidates.json`, `evidence.jsonl` and `trace.jsonl` so the real API can replace them later.
