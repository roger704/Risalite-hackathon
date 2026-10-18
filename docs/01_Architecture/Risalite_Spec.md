---
status: current
related_files: ["docs/01_Architecture/CONCEPT.md", "docs/01_Architecture/Data_Sources.md", "docs/02_State/Execution_Plan.md"]
last_updated: 2026-10-18
purpose: Product spec for Risalite (successor name for PlotScout) as decided 13:35–13:57. Supersedes CONCEPT.md where they differ; CONCEPT.md remains the reference for the agent topology, evidence ledger and the property path.
supersedes: parts of docs/01_Architecture/CONCEPT.md (name, input flow, output format)
---

# Risalite — Product Spec

## One-liner

**Risalite** is an agentic deep-research tool for Berlin. The user types a free question about a place or an area; the planner classifies it, proposes a research plan and sources, waits for approval, runs parallel researchers over Berlin open geodata (WFS/Overpass) and the web (Tavily), fact-checks every claim, and returns a one-page dashboard: summary, ranked findings, an interactive map, sources, trace.

Rename of PlotScout. Everything in `CONCEPT.md` about the agent topology, evidence ledger, fact-checker, model routing and the §34 envelope still applies.

## Input flow (Gemini-Deep-Research-style)

1. **Free question** in a text box (+ optional role).
2. **Classify** — the planner decides which shape the question has and logs why:
   - `point` — about one property/parcel ("what is the best use for Xstraße 12?")
   - `location` — where to put something ("where should I open a coffee place in Kreuzberg?")
   - `gap` — where is something missing ("where are Kitas missing in Neukölln?")
3. **Clarify if the area is unspecified** — one question back to the user ("Which district or neighbourhood?"). Never guess an area.
4. **Derive the area** from the question type:
   - `point` → parcel + ~100 m neighbourhood (§34 sampling)
   - `location` / `gap` → the named Bezirk / Ortsteil / neighbourhood, resolved to a set of cells (see Granularity)
5. **Present the plan**: sub-questions, the source class for each (official geodata · web · structured API), the exact layers/domains it intends to use, and **optional paid sources** listed as "key not provided — skipped" with a key field. User **approves** (or edits) before research starts. Shown in the demo video.
6. Research → ledger → fact-check → write (as in CONCEPT.md).
7. **Dashboard** rendered from `report.md` + `evidence.jsonl` + `trace.jsonl` + `cells.json`.

## The three templates (presented in the demo; the input is still free text)

| | T1 Property | T2 Business location | T3 Kita gap |
|---|---|---|---|
| Shape | `point` | `location` | `gap` |
| Example | "This lot at Xstraße / Ystraße just became available — what is the best use?" | "Where is it smart to open a coffee place in Kreuzberg?" | "Where are Kitas missing in Neukölln?" |
| Area | parcel + 100 m | district → cells | district → cells |
| Geodata | ALKIS parcels/buildings (`aog`), Umweltatlas use/sealing/GFZ, FNP, B-Plan, BRW, LOR pop + MSS, amenities | `ua_einwohnerdichte_2025` (pop + age groups), `mss_2025`, `gewerbedaten` (NACE 56), Overpass `amenity=cafe`, `oepnv_ungestoert`, `schulen`, `brw2026`, `wohnlagenadr2026` | `ua_einwohnerdichte_2025` (under-6), `kita` (places), `oepnv_ungestoert`, planned housing (`step_wo_2040`) |
| Web (Tavily) | district plans, StEP, news on the site, comparable projects, initiatives | IHK/Handelsverband reports, Zentren-/Einzelhandelskonzept of the Bezirk, news on upcoming residential/office projects, commercial rent levels | Bezirk Kita-Entwicklungsplan, news on Kita closures/openings, planned housing projects |
| Paid slot (skipped unless key) | — | hystreet.com footfall, Google Places (competitor ratings) | — |
| Output | summary · story stack + §34 envelope · map: parcel + neighbours coloured by storeys · sources · trace | summary · **top-3 ranked cells** with reasons · **heatmap** of the composite score · layer panel · sources · trace | same as T2 with gap score |

T1 keeps the full CONCEPT.md output (story stack, evidence table) *inside* the shared dashboard skeleton — "combine both".

## Shared dashboard skeleton (one HTML page, both paths)

1. Question + role, and the approved plan (collapsed)
2. **Summary of findings** (3–6 sentences, inline `[n]` citations). For T2/T3 labelled *"written for the default layer mix"*.
3. **Findings** — T1: story stack + evidence table; T2/T3: ranked top-3 list, each entry showing cell name, score, per-layer values with their citation, and any future modifier with its quote.
4. **Map** (Leaflet or MapLibre) — T1: parcel outline + neighbouring buildings coloured by `aog`; T2/T3: heatmap of the score over cells, hover tooltip = cell name, score, layer values, citation links.
5. **Layer panel** (T2/T3 only) — **checkboxes** per layer, each with the planner's default weight displayed; a toggle **Present only / Present + future developments**. Toggling recomputes score, heatmap and top-3 client-side.
6. **Sources** — generated from the ledger only: URL · verbatim quote · method (wfs/overpass/web/pdf) · confidence badge. Plus "could not verify" appendix.
7. **Trace** — plan, every search/tool call with *why*, the skip of paid sources, the stop decision, fact-checker rejections, model per role, tokens, seconds.

## Data model for T2/T3 (`runs/<id>/cells.json`)

```json
{
  "area": {"name": "Friedrichshain-Kreuzberg", "level": "plr", "cell_count": 38},
  "mode_default": "present",
  "layers": [
    {"id": "demand_density", "label": "Population density", "direction": "+", "weight_default": 0.30,
     "source": "https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_2025?...", "evidence_id": "e12",
     "why": "Cafés need resident footfall; density is the cheapest proxy."},
    {"id": "competition", "label": "Existing cafés (NACE 56 + OSM)", "direction": "-", "weight_default": 0.25, "...": "..."}
  ],
  "cells": [
    {"id": "02030301", "name": "Wrangelkiez", "geometry_ref": "cells.geojson#02030301",
     "values": {"demand_density": 0.82, "competition": 0.91, "transit": 0.70, "cost": 0.40},
     "modifiers": [
       {"type": "future", "label": "Planned housing: 300 units at Cuvrystraße", "quote": "…300 Wohnungen bis 2028…",
        "evidence_id": "e31", "localization": "plr", "bonus": 0.10, "magnitude": {"units": 300}}
     ],
     "score_present": 0.61, "score_future": 0.71}
  ]
}
```

Rules:
- `values` are normalized 0–1 **within the area** (min–max; percentile if skewed). `direction` flips the sign in the sum.
- `score = Σ weight_i · (direction_i · value_i)` over the checked layers, re-normalized to 0–1 across cells. The browser recomputes it; the server computes the default for the report.
- **Future modifiers**: only findings dated **after today**. Default: qualitative flag with the verbatim quote and a fixed bonus (+0.10). A `magnitude` field is filled **only if the source states a number** — never estimated by the model. Localization defaults to **PLR**; an exact address is geocoded (`adressen_berlin`) **only when the page clearly states one** (`localization: "address"`).
- Every layer and every modifier carries an `evidence_id` into `evidence.jsonl`; the fact-checker verifies modifiers like any web claim and re-runs WFS requests for layers.
- The top-3 list is the citable object; the heatmap is a visualization of the same data.

## Decisions log (13:35–13:57)

| # | Decision |
|---|---|
| 1 | Name: Risalite. Free-text question; three templates shown in the presentation. |
| 2 | Area selection criterion for the demo: richest *web* story (geodata is uniform across Berlin). |
| 3 | Area is derived from the query; if unspecified, the tool asks a clarifying question. |
| 4 | T2 score: composite by default, layers separable via **checkboxes** with default weights shown. Sliders = stretch. |
| 5 | Web research feeds all of: IHK/retail reports, district concepts, future developments, rents. Future developments are a **Present / Present+future** toggle. |
| 6 | Output = ranked top-3 **and** heatmap. |
| 7 | Future modifier: qualitative + quote; magnitude only if stated by the source; horizon = anything dated after today. |
| 8 | Localization of web findings: PLR by default, exact address only when the page clearly states one. |
| 9 | T1 keeps story stack + §34 envelope inside the shared dashboard skeleton. |
| 10 | One shared dashboard skeleton for all templates. |
| 11 | Third template: Kita gap (reuses the T2 machinery). |
| 12 | Demo question typed fresh, within the three template shapes; rehearse ≥5 variants first. |
| 13 | Plan approval step is shown in the video. |
| 14 | Paid-source key slot: hystreet (footfall), Google Places (competitor ratings); listed and visibly skipped unless a key is given. |

## Open decisions

| # | Question | Recommendation |
|---|---|---|
| A2 | Cell granularity for T2/T3: Umweltatlas block (~25k, fine) vs **PLR** (542, demographics + MSS native) vs hex grid | **PLR** for today: matches modifier localization, every cell already has population/age/MSS, 20–60 cells per district renders instantly. Block-level as stretch. |
| 5a | Ownership of the dashboard (role D?) and what is cut to pay for it | Dashboard replaces the "rich CLI" default; cut sliders, the fact-checker retry loop stays. |
| 5b | Stack | Single FastAPI route serving one HTML page (Leaflet + vanilla JS reading `cells.json`); Streamlit only if nobody can write the page. |
| 5c | Has the cookbook run end-to-end on Token Factory? Do WFS fixtures exist? | Must be true before anyone starts the dashboard. |
| 5d | Video storyboard | see below |

## Draft 2-minute storyboard

0:00 type "Where is it smart to open a coffee place in Kreuzberg?" → 0:10 planner classifies `location`, shows plan with layers, web sources, and the skipped hystreet/Google Places slots → 0:25 click *Approve* → 0:30 trace streams: WFS calls, Tavily searches with *why* → 1:00 fact-checker rejects one web claim, planner re-plans → 1:15 dashboard appears: summary, top-3, heatmap → 1:30 toggle *Present + future*, untick *competition*, ranking changes → 1:45 click three citations (one WFS URL, one IHK/berlin.de page, one news page) → 2:00 self-grade.

## Risks specific to Risalite

| Risk | Fallback |
|---|---|
| Live-typed question classified wrongly | Rehearse 5 variants per template; keep three frozen questions as backup |
| Future-development search finds nothing for the chosen district | Pick the demo district by richest web story (decision 2); keep a second district |
| Client-side re-scoring and the written summary disagree | Summary labelled "default mix"; top-3 list is recomputed, summary is not |
| Dashboard not solid by 16:00 | Fall back to `report.md` + a static PNG heatmap (matplotlib) embedded in Markdown |
