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
| [Demo script](docs/02_State/Demo_Script.md) | Rehearsal questions per template and the two-minute storyboard |
| [Deep research experiment](backend/experiments/README.md) | Isolated Python 3.12 DeepAgents + Nebius Token Factory + Tavily research loop; local, Docker, and CPU-only Nebius VM deployment |

**Implementation status:** the backend implements Berlin data ingestion and snapshots, the read API, the live S1 deep-research runtime, the research dashboard, report/source exports, and the evidence ledger + fact-checker (`backend/app/evidence/`).

## Run it

```bash
git clone https://github.com/roger704/Risalite-hackathon.git && cd Risalite-hackathon
cp .env.example .env            # TAVILY_API_KEY, NEBIUS_API_KEY, NEBIUS_MODEL (optional for fixtures)
cd backend && uv sync
uv run uvicorn app.api.main:app --reload   # dashboard + API on http://127.0.0.1:8000/
```

## Live deep research (S1)

### Prerequisites and Berlin snapshots

Install `uv`, use Python 3.11, and provide `NEBIUS_API_KEY` and `TAVILY_API_KEY` through your secret manager for live model and search calls. From the repository root:

```sh
export LOCATION_DATA_DIR=/home/ubuntu/data/risalite
cd backend
LOR_SNAPSHOT_ID=berlin_lor-099dc691163b4bdf82567d4e643fbeee
uv run --python 3.11 location-data ingest berlin_lor
uv run --python 3.11 location-data ingest berlin_lor_names --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run --python 3.11 location-data ingest berlin_population
uv run --python 3.11 location-data ingest zensus_grid
uv run --python 3.11 location-data ingest osm_pois
uv run --python 3.11 location-data ingest vbb_gtfs
uv run --python 3.11 location-data ingest ihk_businesses
uv run --python 3.11 location-data ingest berlin_population_density_blocks --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run --python 3.11 location-data ingest berlin_addresses
uv run --python 3.11 location-data ingest berlin_kitas --lor-snapshot "$LOR_SNAPSHOT_ID"
uv run --python 3.11 location-data ingest berlin_centers --lor-snapshot "$LOR_SNAPSHOT_ID"
```

### Start and run

Start the dashboard and live API from `backend/`; replace the two secret placeholders with secret-manager references:

```sh
LOCATION_DATA_DIR=/home/ubuntu/data/risalite RISALITE_RUNS=live \
RISALITE_AREA_DATA=summary RISALITE_RUNS_DIR=/home/ubuntu/data/risalite/runs \
NEBIUS_API_KEY='<NEBIUS_API_KEY secret reference>' \
TAVILY_API_KEY='<TAVILY_API_KEY secret reference>' \
uv run --python 3.11 uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>, ask **“Wo in Kreuzberg sollte ich ein Café eröffnen?”**, review the proposed area and research plan, then approve it to start the run. The planner resolves the area; data and scoring v2 prepare PLR cells; wave-1 and wave-2 researchers gather evidence; fact-checking verifies findings; the skeptic challenges risks; ranking scores the cells; and the writer produces the report. Citations use evidence-ledger IDs (`[ev_…]` for web sources and `[ds_…]` for datasets).

Each `RISALITE_RUNS_DIR/<run_id>/` contains the report, result, plan, cells, evidence ledger, findings, fact-check output, event and trace logs, metrics, and run metadata (for example, `report.md`, `result.json`, `plan.json`, `cells.json`, `evidence.jsonl`, `findings.jsonl`, `factcheck.json`, `events.jsonl`, `trace.jsonl`, `metrics.json`, and `run.json`).

For an offline run with local snapshots, set `RISALITE_RESEARCH_FAKE=1`; this uses deterministic fake research instead of Nebius or Tavily calls.

Check the citations of a run (report marker → ledger entry → URL resolves → quote re-matches):

```bash
cd backend && uv run python ../scripts/verify_citations.py ../runs/<id> [--threshold 0.9]
cd backend && uv run python ../scripts/verify_citations.py ../frontend/fixtures --offline
```

`--offline` checks citation mapping and stored passages only. Online, quotes are re-fetched with Tavily `extract` when `TAVILY_API_KEY` is set, else a direct HTTP GET.

### Evidence and fact-check (`backend/app/evidence/`)

| Module | Role |
|---|---|
| `ledger.py` | Append-only `runs/<id>/evidence.jsonl`: Tavily `WebEvidence` (`ev_…`) and snapshot values (`ds_…`, value hash); status changes are new revisions |
| `matching.py` | Shared quote matcher: verified ≥ 0.85, partial ≥ 0.60 |
| `factcheck.py` | Re-fetch per source (Tavily extract / HTTP with per-hop SSRF checks / allow-listed WFS + Overpass re-query / snapshot re-read); writes `factcheck.json`, `findings.jsonl` verification, `fact_check` trace events, strips uncited markers from `report.md` and appends "Could not verify" |
| `judge.py` | Score used by `scripts/verify_citations.py` |

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
