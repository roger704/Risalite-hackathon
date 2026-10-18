# Demo video (target cut)

| File | Content |
|---|---|
| `SpotWise_demo.mp4` | 1:57, 1920×1080, voice-over with burned-in captions |
| `SpotWise_demo_nocaptions.mp4` | same cut without captions |

This is the **target cut**: it was recorded from the original demo prototype (now consolidated into `frontend/app`) with synthetic data and a scripted cursor, plus slides for the deep research flow (`docs/01_Architecture/Deep_Research_Spec.md`, PR #16). Re-record the app segments beat for beat from a real run before submission.

| Time | Beat |
|---|---|
| 0:00–0:20 | Ask "Where is it smart to open a coffee place in Kreuzberg?" → classified as `location`, plan with layers, sub-questions and skipped paid sources → approve |
| 0:20–0:42 | Live research trace: data and web researchers with *why*, skeptic demotes Wrangelkiez, fact-checker rejects a footfall claim, planner re-plans |
| 0:42–1:00 | Dashboard: summary, top-3, heatmap; *Present + future*; untick competition → ranking changes |
| 1:00–1:14 | Citation → sources with verbatim quotes → "Could not verify" |
| 1:14–1:26 | Slide: four stages, data first, web explains |
| 1:26–1:37 | Slide: evidence chain (`web_search` → `extract_page` → `record_finding` → ledger → fact-check → writer) and hard rules |
| 1:37–1:47 | Slide: models per role on Nebius Token Factory + Tavily |
| 1:47–1:57 | Built with parallel Devin sessions → end card |
