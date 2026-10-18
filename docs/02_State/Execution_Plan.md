---
status: living
related_files: ["docs/02_State/Handoff.md", "docs/01_Architecture/Risalite_Spec.md", "docs/01_Architecture/CONCEPT.md"]
last_updated: 2026-10-18
purpose: Hour-by-hour plan for hackathon day with checkable items. Rev. 14:50 — Risalite with area path (must) and point path / LocalLens (T1, Option 1). This IS the todo list.
---

# Execution Plan — Sunday 18 Oct 2026 (Risalite, rev. 14:50)

Deadline: **18:30 video submitted**. Grand final 20:00. Feature freeze **17:00**. Must-have: T2 coffee (area) end-to-end with clickable citations. Second: T1 point path (LocalLens). Third: T3 Kita.

Streams (fill names in `Handoff.md` → Ownership Map):
- **S1 — Agent & orchestration (area)**: classify → plan → approve → researchers → skeptic → fact-checker → writer; events; HTTP API.
- **S1b — Point path (LocalLens)**: hypotheses → per-hypothesis FOR/AGAINST researchers → operator-fit scoring → `candidates.json`. Builds on S1's ledger, event schema and API; proposed owner Eraldo.
- **S2 — Data & scoring**: PLR cells, WFS/Overpass layers, normalization, scoring, `cells.json`; `locate(address)` → cell values + competitors within 500 m for the point path.
- **S3 — Frontend**: dashboard (stack = S3's choice), map, mixer, live view, sources; both result shapes.
- **S4 — Evidence & delivery**: ledger, fact-checker, `verify_citations.py`, README, repo, rehearsal, video.

Every item must serve citation integrity, visible decisions, or demo-ability.

## Phase 0 — Contracts & readiness (14:50–15:10)

- [ ] All: read `Risalite_Spec.md` §Contracts (5 min). **Freeze**: HTTP API, research event, ledger entry, `cells.json`, `candidates.json`. Changes after 15:10 go to all four by message.
- [ ] S1: cookbook has run end-to-end on Token Factory with sources (if not: first task); model IDs in `config.py`.
- [ ] S2: WFS fixtures for `lor_2021` (PLR geometries), `ua_einwohnerdichte_2025`, `mss_2025`, `gewerbedaten`, `oepnv_ungestoert` for the demo district; fetch now if missing.
- [ ] S4: public repo, `.env.example` (`NEBIUS_API_KEY`, `TAVILY_API_KEY`, `NEBIUS_API_BASE`), `.env` git-ignored.
- [ ] All: **freeze demo district** (richest café web story + a plausible risk story) + backup; **freeze one demo storefront + operator profile** for the point path. Record in `Handoff.md`.
- [ ] S3: decide stack; build against mock `cells.json`, mock `candidates.json`, mock event stream.
- [ ] Decide A3 (city-wide default when no area given) — recommended yes.

## Phase 1 — Build in parallel (15:10–16:30)

### S1 — Agent (area)
- [ ] `classify(question, profiles?)` → `location|gap|point` + area/address + *why*; `needs_clarification` only on `point` without address.
- [ ] `plan()` area: layers from per-template defaults, planner may adjust weights **with logged reason**; web sub-questions; paid sources listed as skipped.
- [ ] `POST /research`, `POST /runs/{id}/approve`, `GET /runs/{id}/events` (SSE), `GET /runs/{id}/result`.
- [ ] Web researchers (Nemotron Lightning), one per top-5 cell: Tavily `search` (domain-restricted first) + `extract` for quotes → `Evidence` with `tier`; future modifiers (after today, PLR localization, address only if stated).
- [ ] Skeptic (gpt-oss-120b) on #1 cell: risks with quote + severity + `claim_type`; `high` → demote, `rescore` event.
- [ ] Writer (Beam): summary + findings from ledger ids only, `claim_type` per sentence; post-processor strips `[n]` not in the ledger.
- [ ] Every call → research event with `why`, model, tokens, seconds.

### S1b — Point path (LocalLens)
- [ ] Parse space + operator profile from free text into the `candidates.json` `space`/`operator` blocks (planner, logged).
- [ ] `plan()` point: location sub-questions + 3–4 business hypotheses with *why* (operator experience × neighbourhood gaps); approval step shared.
- [ ] Ground: call S2 `locate(address)` → cell values + competitors within 500 m by NACE of each hypothesis.
- [ ] Per hypothesis: FOR researcher (competitor sites, menus, prices, openings, IHK, trade press) and skeptic AGAINST (competition, weak demand, operator gaps, space limits, regulation) → `for[]`, `against[]`, `unknowns[]` with `suggested_validation`.
- [ ] Scorer: six dimensions with `why` + evidence ids; recommendation score; `rank_initial`/`rank_final`; weak-tier evidence cannot carry a recommendation alone.
- [ ] Writes the same `evidence.jsonl` and events as S1; result shape `candidates.json`.

### S2 — Data & scoring
- [ ] PLR cells for the district (`lor_2021` → `cells.geojson`); all-Berlin variant if A3 = yes.
- [ ] Layer functions `{cell_id: raw}` + exact WFS/Overpass URL as citation: pop density + 18–45 share, MSS, cafés (`gewerbedaten` NACE 56 + Overpass), transit, schools/offices, BRW, Wohnlage. Kita: under-6, `kita`, `step_wo_2040`.
- [ ] Normalize within area, `direction`, weighted score, `score_present`/`score_future` (+0.10 modifier bonus).
- [ ] `build_cells(area, layers, weights, modifiers, risks, unknowns) → cells.json`, every value with an `evidence_id`.
- [ ] `locate(address)` for the point path: geocode (`adressen_berlin`), containing PLR + its layer values, competitors within 500 m (`gewerbedaten` by NACE list + Overpass), Wohnlage/BRW at the address — all as `Evidence`.
- [ ] Fixtures + `method=wfs-cached` fallback.

### S3 — Frontend
- [ ] Input: question box + optional space/operator profile fields; plan view with **Approve** (both plan shapes).
- [ ] Map: PLR heatmap from `cells.json` with hover values + citation links; point mode: the space, its cell shaded, competitors by hypothesis.
- [ ] Layer mixer: checkboxes with default weights; Present / Present+future; client-side recompute of score, heatmap, top-3, Location Battle.
- [ ] Findings: area top-3 (values, modifiers, risks, unknowns) + Location Battle; point top-3 hypotheses (score, per-dimension why, for / against / unknowns + validation) + Hypothesis Battle.
- [ ] Live investigation view from SSE (fallback: poll `trace.jsonl`).
- [ ] Sources with tier + confidence badge + "could not verify"; self-grade.

### S4 — Evidence & delivery
- [ ] `ledger.py` — entry schema from the spec (`tier`, `claim_type` on findings) → `runs/<id>/evidence.jsonl`.
- [ ] Fact-checker: re-fetch each URL (Tavily `extract` / WFS re-query), fuzzy quote ≥ 0.85, verified / partial / unsupported; unsupported → drop + list. Works on both result shapes.
- [ ] `scripts/verify_citations.py` for a judge.
- [ ] README draft: clone → `.env` → `uv sync` → one command → dashboard; architecture picture; model table; both example questions.
- [ ] Demo script: 5 rehearsal questions per template; storyboard from the spec.

## Phase 2 — Integrate (16:30–17:00)

- [ ] Merge to `main`; `uv run pytest -q` green; one command starts API + frontend.
- [ ] Full end-to-end run on the demo district (area): plan → approve → searches with *why* → skeptic → rescore → fact-check rejection → dashboard.
- [ ] Point path end-to-end on the demo storefront. If not green by 17:00, video closes on coffee → Kita.
- [ ] Click 10 citations by hand across both runs. Fix at the root.
- [ ] **17:00 feature freeze.**

## Phase 3 — Harden & rehearse (17:00–17:45)

- [ ] `verify_citations.py` 100 % on the demo runs.
- [ ] Rehearse the storyboard twice with fresh questions; time it; decide the closing switch (point or Kita).
- [ ] `log.md` + `Handoff.md` updated.

## Phase 4 — Deliver (17:45–18:30)

- [ ] Record: one uncut run; three citations clicked; closing switch.
- [ ] README final; push `main`; fresh `git clone` + README verified on a second laptop.
- [ ] **Submit by 18:30.**

## Stretch (only after Phase 2 is green)

- [ ] T3 Kita end-to-end (if not used as the closing switch).
- [ ] Sliders; block-level cells; real Google Places call when a key is pasted; evidence graph.

## Phase 5 — Grand-final prep (18:30–20:00)

- [ ] 90-second pitch: plan decision, skeptic flipping the ranking, three live citation clicks, the inverse-question switch.
- [ ] Backup video on two laptops.

## Review (fill at wrap-up)

- What worked / what to cut next time:
