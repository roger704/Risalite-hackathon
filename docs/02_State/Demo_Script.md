# Demo Script

Rehearsal questions (S4). Run each one before recording. Each must produce a plan, at least one fact-check rejection or partial, and a report where `scripts/verify_citations.py` scores 1.0.

## Location template (`shape=location`)

1. Where is it smart to open a coffee place in Kreuzberg? *(demo question)*
2. Which Neukölln areas suit a new bakery for families with young children?
3. Where in Friedrichshain is a late-night bar least likely to clash with residents?
4. Which Pankow neighbourhoods lack a Kita relative to their under-6 population? *(Kita closing switch)*
5. Where in Mitte would a co-working space find the most office workers within walking distance?

## Point template (`shape=point`)

1. What should open at Oranienstraße 25, a 120 m² ground-floor unit? *(demo storefront)*
2. Is Sonnenallee 100 a good spot for a bike repair shop?
3. What business fits a vacant corner unit at Boxhagener Platz?
4. Would a pharmacy work at Karl-Marx-Straße 150?
5. What could replace the closed café at Bergmannstraße 20?

## Storyboard (two minutes)

| Time | On screen | Say |
|---|---|---|
| 0:00 | Type the demo question | "A free question, no form." |
| 0:10 | Plan: shape, layers, web sources, skipped paid slots | "The planner classifies it and shows what it will and won't use." |
| 0:25 | Click *Approve* | "Nothing runs before approval." |
| 0:30 | Trace: WFS calls and Tavily searches, each with *why* | "Data first; the web explains." |
| 1:00 | `fact_check` event rejects a web claim; planner re-plans | "Every quote is re-fetched; this one did not match, so it is dropped." |
| 1:15 | Dashboard: summary, top-3, heatmap | |
| 1:30 | Toggle *Present + future*, untick *competition* | "The ranking is recomputed from the same evidence." |
| 1:45 | Click three citations: WFS URL, berlin.de/IHK page, news page | "Every number and sentence links to its source." |
| 2:00 | Self-grade + `verify_citations.py` PASS | Closing switch: point path or Kita question. |

## Before recording

```bash
cd backend && uv run python ../scripts/verify_citations.py ../runs/<demo-run-id>
```

Keep a backup run directory of the demo question in case the live run fails.
