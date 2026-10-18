# SpotWise landing page

Single self-contained `index.html` (no build step, no external requests). The hero is a 10-second loop:

- **0–5 s, the idea:** a person asks a question → planner, data, web and skeptic agents research → every block of the area is scored on a hex heatmap → a ranked, cited answer.
- **5–10 s, the details:** approve the research plan → the skeptic verifies one claim and drops one without a source → deliverables (report PDF/HTML/MD, heatmap, sources CSV/BibTeX, share link).

Each loop follows a different person: Mira (café founder, Kreuzberg, T2), Jonas (district planner, Kita gap in Neukölln, T3) and Aylin (empty storefront in Moabit, T1 point). Viewers can pick a person, jump to a step or pause. Below the hero are short "How it works", "Who it's for" and "Deliverables" sections. With `prefers-reduced-motion` the loop starts paused on the full answer.

The public server serves this page at `/` and `/landing/`. **Try the demo** opens the research app at `/app/`. Local API previews serve the landing page at `/landing/` and the app at both `/` and `/app/`.

The people, places, quotes and `example-*.de` sources in the animation are illustrative examples, not research results.
