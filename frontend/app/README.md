# SpotWise application

This is the single application frontend, served at `/` by FastAPI. It combines the demo's glass panels, animated workflow, and interactive map with the research API. There is no frontend build step.

The workflow is **Ask → review and approve the plan → research → explore results**. Data, evidence, citations, and agent events come from the backend. In live mode, opening a shared question stops at plan approval before starting research.

Set `RISALITE_RUNS=live` for real research with configured providers and imported datasets. Set `RISALITE_RUNS=fixtures` for a clearly labeled replay using synthetic fixtures. See the repository README for backend setup.

The standalone prototype has been consolidated here and removed. The public research app is served at `/app/`; the marketing homepage at `/` links to it. Local API previews also serve this application at `/`, with the marketing page at `/landing/`.
