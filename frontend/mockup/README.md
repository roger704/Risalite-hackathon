# Risalite dashboard mockup

Clickable, self-contained mockup of the S3 frontend described in `docs/01_Architecture/Risalite_Spec.md` and `docs/02_State/Execution_Plan.md`. No build step and no network access.

```sh
python3 -m http.server 8765 --directory frontend/mockup
# open http://localhost:8765
```

You can also open `index.html` directly in a browser.

**Covers:**

- Free-text question, optional role and operator profile. The `point` / `location` / `gap` classification is keyword-based, with a stated reason. An unspecified area triggers a clarifying question. Demo data exists only for cafés (location), Kitas (gap) and the Wiener Straße 12 storefront (point); other subjects or addresses show a note and fall back to that example. Operator fit is scored neutral unless the profile matches the demo bakery profile.
- Plan approval: sub-questions with their source class, layers with default weights, and paid sources marked "key not provided — skipped" (the key field is disabled; the mockup makes no provider calls). Unticked sub-questions, layers and hypotheses are dropped from the trace and results, and shown as "not approved" in the dashboard.
- Live investigation view, which replays a mock `trace.jsonl` (stages, *why*, model, tokens, seconds; the skeptic's rescore; the fact-checker's rejection and the replan).
- Shared dashboard skeleton (sections 1–7 of the spec) for all three templates:
  - **T2 coffee in Kreuzberg / T3 Kita gap in Neukölln:** summary with `[n]` citations ("default mix"), top-3 cells, Location Battle, PLR heatmap with hover values, and a layer panel. The panel has checkboxes and a Present / Present + future toggle, and recomputes score, heatmap and top-3 in the browser.
  - **T1 storefront (LocalLens):** top-3 hypotheses, each with six scored dimensions and FOR / AGAINST / UNKNOWNS lists, plus a Hypothesis Battle and a map of the space with competitors within 500 m.
- Sources (method, tier, confidence, verified/partial), a "could not verify" appendix, a model-per-role table and a self-grade.
- **Exports** (dashboard section 1): *Report .md*, *Report .html* (self-contained, print CSS, includes the current map) and *Print / PDF* (opens the HTML report and the print dialog) all reflect the current layer mix. *Sources .csv* (UTF-8 with BOM, unsupported rows unnumbered, spreadsheet-formula guarded) and *Sources .bib* (verified + partial only) are built from the source ledger. *Copy share link* writes the question, role, profile, area, approved plan, layer checkboxes and Present / Present + future into the URL hash; opening that link restores the dashboard without replaying the trace.

**Data:** PLR boundaries are real: `lor_2021:a_lor_plr_2021` from the gdi.berlin.de WFS, simplified, Amt für Statistik Berlin-Brandenburg, CC BY 3.0. All layer values, quotes, URLs under `example-*.de`, scores and traces are **synthetic**. Their shapes follow `cells.json`, `candidates.json`, `evidence.jsonl` and `trace.jsonl` so the real API can replace them later.
