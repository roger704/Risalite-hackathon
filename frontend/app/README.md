# SpotWise application

This is the single application frontend, served at `/` by FastAPI. It combines the demo's glass panels, animated workflow, and interactive map with the research API. There is no frontend build step.

The workflow is **Ask → review and approve the plan → research → explore results**. Data, evidence, citations, and agent events come from the backend. In live mode, opening a shared question stops at plan approval before starting research.

Set `RISALITE_RUNS=live` for real research with configured providers and imported datasets. Set `RISALITE_RUNS=fixtures` for a clearly labeled replay using synthetic fixtures. See the repository README for backend setup.

The standalone prototype has been consolidated here and removed. The public research app is served at `/app/`; the marketing homepage at `/` links to it. Local API previews also serve this application at `/`, with the marketing page at `/landing/`.

## Understanding Location Battle

The comparison uses the same direction-aware values and weights as the ranking engine. Live normalized values already mean “higher is better,” including low competition and shorter distance; the UI must not invert them again. Legacy fixtures without `values_directional` are adjusted once by the shared scoring helper.

Each bar and number shows a layer's **weighted contribution**, not a vote for the overall winner. Contributions sum to the present score; missing values are excluded and remaining weights are normalized for each location. Future mode shows its capped adjustment separately. The panel names the overall recommendation and its largest layer advantage. If required-data checks or a research-backed risk demotion put a lower numeric score first, the panel explains that rank override and links the risk finding.

Regression coverage includes the reported Viktoriapark/Graefekiez Nord comparison, legacy values, changed weights, missing data, future adjustments and rank overrides in `backend/tests/scoring/test_location_battle.py`.
