# Risalite — Agentic Deep Research for Berlin Locations

**Challenge:** Agentic Deep Research with Nebius Token Factory & Tavily

**Risalite** (formerly PlotScout) is an agentic deep-research tool for Berlin. The user types a free question about a place or an area; the planner classifies it, proposes a research plan and sources, waits for approval, runs parallel researchers over Berlin open geodata (WFS/Overpass) and the web (Tavily), fact-checks every claim, and returns a one-page dashboard: summary, ranked findings, an interactive map, sources, and trace.

**"Don't just research a location. Discover where opportunity exists."**

## Key documents

| Document | Purpose |
|---|---|
| [Risalite spec](docs/01_Architecture/Risalite_Spec.md) | Product spec: input flow, templates, dashboard, `cells.json` data model, decisions log, open decisions, storyboard, risks |
| [Execution plan](docs/02_State/Execution_Plan.md) | Hour-by-hour plan for hackathon day with checkable items — this **is** the todo list |
| [Data sources research](DATA_SOURCES.md) | Free datasets, access methods, licensing, limitations, integration order |
| [Connector blueprint](CONNECTOR_BLUEPRINT.md) | Adapter designs, shared data contracts, caching, provenance |
| [Landing page](frontend/landing/README.md) | Animated, looping landing page: what Risalite is for, the workflow and the deliverables, in 10 seconds |
| [Backend setup guide](backend/README.md) | Working LOR and population connectors, ingestion CLI, versioned snapshots, read API |
| [Deep research experiment](backend/experiments/README.md) | Isolated Python 3.12 DeepAgents + Nebius Token Factory + Tavily research loop; local, Docker, and CPU-only Nebius VM deployment |

**Implementation status:** the backend currently implements the LOR and population connectors, ingestion CLI, snapshots, and read API. Everything else below is planned.

## Input flow (Gemini-Deep-Research-style)

1. **Free question** in a text box (+ optional role).
2. **Classify** — the planner decides the question shape and logs why: `point` (one property/storefront), `location` (where to put something), or `gap` (where something is missing).
3. **Clarify** if the area is unspecified — one question back to the user. Never guess an area.
4. **Derive the area** — `point` → parcel + ~100 m neighbourhood; `location`/`gap` → named Bezirk/Ortsteil resolved to PLR cells.
5. **Present the plan** — sub-questions, source class per sub-question, exact layers/domains, and optional paid sources shown as "key not provided — skipped". The user **approves** (or edits) before research starts.
6. **Research → evidence ledger → fact-check → write.**
7. **Dashboard** rendered from `report.md`, `evidence.jsonl`, `trace.jsonl`, and `cells.json` / `candidates.json`.

## Templates (the input is still free text)

| | T1 Property / point (LocalLens) | T2 Business location | T3 Kita gap |
|---|---|---|---|
| Shape | `point` | `location` | `gap` |
| Example | "This storefront at Xstraße 12 just became available — what is the best use?" | "Where is it smart to open a coffee place in Kreuzberg?" | "Where are Kitas missing in Neukölln?" |
| Area | parcel + 100 m | district → PLR cells | district → PLR cells |
| Output | top-3 business hypotheses with for / against / unknowns, scored on six dimensions | **top-3 ranked cells** + score **heatmap** + layer panel | same as T2 with a gap score |

Priority today: **T2 coffee (area) end-to-end with clickable citations** is the must-have; T1 point path is second; T3 Kita is third.

## Dashboard (one shared skeleton)

1. Question, role, and approved plan (collapsed)
2. Summary of findings with inline `[n]` citations (T2/T3 labelled "written for the default layer mix")
3. Findings — ranked top-3 (area) or top-3 hypotheses (point), plus a Location/Hypothesis Battle
4. Map — PLR heatmap with hover values and citation links; point mode shows the space, its cell, and competitors
5. Layer panel — checkboxes with default weights and a **Present / Present + future** toggle; recomputes score, heatmap, and top-3 client-side
6. Sources — generated from the ledger only: URL, verbatim quote, method, confidence badge, and a "could not verify" appendix
7. Trace — plan, every tool call with *why*, skipped paid sources, fact-checker rejections, model per role, tokens, seconds

## Technology

- **Nebius Token Factory** — planning, classification, researchers, skeptic, writer.
- **Tavily** — `search` (domain-restricted first) and `extract` for verbatim quotes.
- **Berlin open geodata** — `lor_2021` (PLR geometries), `ua_einwohnerdichte_2025`, `mss_2025`, `gewerbedaten` (NACE 56), `oepnv_ungestoert`, `schulen`, `brw2026`, `wohnlagenadr2026`, `kita`, `step_wo_2040`, `adressen_berlin`; plus OpenStreetMap via Overpass.
- **Optional paid slots** (skipped unless a key is given) — hystreet.com footfall, Google Places competitor ratings.
- **Application** — Python + FastAPI backend; dashboard as a single HTML page (Leaflet/MapLibre + vanilla JS) is the recommended stack.

## Work streams

- **S1 — Agent & orchestration (area):** classify → plan → approve → researchers → skeptic → fact-checker → writer; research events; HTTP API.
- **S1b — Point path (LocalLens):** hypotheses → per-hypothesis FOR/AGAINST researchers → operator-fit scoring → `candidates.json`.
- **S2 — Data & scoring:** PLR cells, WFS/Overpass layers, normalization, scoring, `cells.json`; `locate(address)` for the point path.
- **S3 — Frontend:** dashboard, map, layer mixer, live investigation view, sources; both result shapes.
- **S4 — Evidence & delivery:** ledger, fact-checker, `verify_citations.py`, README, repo, rehearsal, video.

Frozen contracts (see the spec): HTTP API (`POST /research`, `POST /runs/{id}/approve`, `GET /runs/{id}/events` SSE, `GET /runs/{id}/result`), research event, ledger entry, `cells.json`, `candidates.json`.

## Timeline — Sunday 18 Oct 2026

| Time | Phase |
|---|---|
| 14:50–15:10 | Phase 0 — freeze contracts, demo district, demo storefront + operator profile |
| 15:10–16:30 | Phase 1 — build in parallel |
| 16:30–17:00 | Phase 2 — integrate; **17:00 feature freeze** |
| 17:00–17:45 | Phase 3 — harden and rehearse |
| 17:45–18:30 | Phase 4 — record and **submit video by 18:30** |
| 18:30–20:00 | Phase 5 — grand-final prep (final at 20:00) |

## Two-minute demo storyboard

0:00 type "Where is it smart to open a coffee place in Kreuzberg?" → 0:10 planner classifies `location`, shows the plan with layers, web sources, and skipped hystreet/Google Places slots → 0:25 click *Approve* → 0:30 trace streams WFS calls and Tavily searches with *why* → 1:00 fact-checker rejects one web claim, planner re-plans → 1:15 dashboard: summary, top-3, heatmap → 1:30 toggle *Present + future*, untick *competition*, ranking changes → 1:45 click three citations (WFS URL, IHK/berlin.de page, news page) → 2:00 self-grade. Closing switch: point path or Kita.

## Out of scope

- Precise commercial revenue predictions.
- Comprehensive rental property availability.
- Automatically verified premises or permits.
- Model-estimated magnitudes — numbers appear only when a source states them.

## Success criteria

A judge can:

1. Type a real location question and approve the plan.
2. Watch the agent make autonomous research decisions with visible *why*.
3. See real public data influence the ranking.
4. Click and verify randomly selected citations (`verify_citations.py` passes 100 % on demo runs).
5. Understand why the recommendation was made — and what evidence could change it.
