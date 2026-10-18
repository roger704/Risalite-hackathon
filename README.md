# Hackathon Project Brief — AI Location Intelligence

**Concept:** An autonomous research agent that discovers the best locations to open a business or public service, combining open data, geospatial analysis, and verifiable deep research.

**Challenge:** Agentic Deep Research with Nebius Token Factory & Tavily

## 1. Vision — What We Want to Build

**"Don't just research a location. Discover where opportunity exists."**

Users ask questions such as:

- Where is the best place to open a coffee shop in Berlin?
- Which neighborhoods need another kindergarten?
- Where would a new gym, restaurant, or coworking space succeed?

Our system autonomously analyzes public datasets, discovers promising locations, investigates them, challenges its own assumptions, and presents evidence-backed recommendations.

**The key differentiator:** We don't just answer questions. We discover opportunities, explain why they exist, and show what could make our recommendations wrong.

## 2. Technology & Data Sources

See [Data sources research](DATA_SOURCES.md) for free datasets, access methods, licensing, limitations, and the recommended integration order.

See the [Connector blueprint](CONNECTOR_BLUEPRINT.md) for adapter designs, shared data contracts, caching, provenance, and implementation handoffs.

**AI & Research**

- **Nebius Token Factory:** LLM inference, planning, reasoning, and agent orchestration.
- **Tavily:** Live web research, source extraction, fact-checking, and investigation of specific locations.
- **Research agents:** Planner, Data Researcher, Web Researcher, and Skeptic/Verifier.

**Free Public Data**

- **OpenStreetMap / Overpass API:** Cafés, kindergartens, restaurants, shops, parks, and amenities.
- **Berlin Open Data:** Population, demographics, neighborhood boundaries, and socioeconomic characteristics.
- **Berlin Kita Registry:** Existing kindergartens and publicly reported facility information.
- **VBB:** Public transport stops, routes, and accessibility.
- **Berlin Planning Data:** Land usage, commercial centers, and planned developments.

**Application**

- Python + FastAPI for backend services.
- GeoPandas / Shapely for spatial calculations.
- React + MapLibre GL for the interactive frontend.
- JSON-based contracts between components.

## 3. How It Works

**Step 1 — Understand the Objective**

The user enters a business type, preferred geographic area, and optional priorities such as customer demand, accessibility, or low competition.

**Step 2 — Discover Opportunities**

The system analyzes population, existing businesses, accessibility, and neighborhood characteristics. A deterministic scoring engine identifies promising locations.

**Step 3 — Autonomous Deep Research**

Nebius plans investigations into the leading candidates. Tavily searches for relevant information such as local developments, competitors, rental conditions, and potential risks.

**Step 4 — Challenge the Findings**

A verifier actively tries to disprove the leading recommendation, identifies evidence gaps, and triggers additional research where necessary.

**Step 5 — Deliver Actionable Results**

The user receives a ranked shortlist, interactive map, supporting evidence, potential risks, and concrete recommendations.

Every factual claim should link to its original source.

## 4. Work Division — Three Parallel Streams

### Team 1 — AI Research & Orchestration

**Mission:** Make the agent think, investigate, and verify.

- Set up the Nebius + Tavily deep research cookbook.
- Implement planning, tool calling, and hypothesis generation.
- Connect structured data tools.
- Build evidence verification and contradiction detection.
- Return structured recommendations with citations.

**Deliverable:** Working research API and visible investigation trace.

### Team 2 — Data & Geospatial Intelligence

**Mission:** Turn public datasets into usable location insights.

- Implement OpenStreetMap, Berlin Open Data, and Kita connectors.
- Normalize and cache relevant data.
- Calculate competition, local demand, and accessibility metrics.
- Build the initial location ranking engine.
- Return geographic coordinates, scoring metrics, and data provenance.

**Deliverable:** Reusable location analysis API for Berlin.

### Team 3 — Frontend & Visualization

**Mission:** Make complex research understandable and impressive.

- Build the user input experience.
- Implement an interactive Berlin map.
- Visualize promising areas and competing businesses.
- Show real-time agent activities and discoveries.
- Present recommendations, evidence, risks, and scores.

**Deliverable:** Interactive application ready for the demo.

**Shared dependency:** Agree on the API request/response format and research-event schema before development begins. All teams work against mock responses until integration.

## 5. Creative Presentation Ideas

**A. Opportunity Heatmap**

Display Berlin as a dynamic heatmap, where promising areas become brighter as new data is evaluated. Users can switch between coffee shop and kindergarten to see how the opportunity landscape changes.

**B. Live Investigation View**

Show the agent's investigation as it happens:

- Investigating neighborhood demographics
- Identifying nearby competitors
- Checking public transport access
- Researching planned developments
- Challenging the leading candidate
- Revising its recommendation

This demonstrates real agentic decision-making rather than a static report.

**C. Research Evidence Graph**

A visual network connects locations, research findings, supporting sources, contradictions, and final recommendations.

Click any claim to inspect its supporting evidence and original URL.

**D. Location Battle**

Compare the top three areas side by side across demand, competition, accessibility, cost uncertainty, and research confidence.

A particularly strong demo moment would be when the initially highest-ranked area loses because the verifier uncovers an important risk.

**E. What-If Simulator**

Let users modify priorities:

- What if low competition matters more than accessibility?
- What if we target families instead of commuters?
- What if rent is higher than expected?

The rankings update based on the available data, while unsupported assumptions remain explicitly labeled.

## 6. Hackathon Scope

**Must Have**

- One end-to-end research workflow using Nebius and Tavily.
- OpenStreetMap and Berlin population data.
- Deterministic location ranking.
- Interactive map with recommended areas.
- Clickable sources and a verification step.
- Working coffee shop use case.

**Nice to Have**

- Kindergarten-specific data and scoring.
- Live research progress.
- Evidence graph and location comparison.
- Interactive scenario analysis.

**Out of Scope**

- Precise commercial revenue predictions.
- Comprehensive rental property availability.
- Automatically verified premises or permits.
- Fully automated location booking or acquisition.

## 7. Two-Minute Demo Story

**0:00–0:20 — The Problem**

"Choosing a location requires combining dozens of fragmented datasets, demographic statistics, local competition, and information that might change tomorrow."

Ask: *Where should I open a coffee shop in Berlin?*

**0:20–0:45 — The Discovery**

Berlin's map comes alive. The system analyzes public datasets and identifies promising neighborhoods.

**0:45–1:15 — The Investigation**

The agent researches the leading candidates using Tavily, follows unexpected findings, and challenges its initial recommendation.

**1:15–1:40 — The Result**

Show the top three locations, their scores, evidence, risks, and source links. Demonstrate how one challenged assumption changes the ranking.

**1:40–2:00 — The Vision**

Switch the use case from *coffee shop* to *kindergarten*.

The same research engine now evaluates childcare demand instead of commercial opportunity.

**Closing line:**

"Today we discover where to open a coffee shop. Tomorrow, the same system could help decide where to build schools, healthcare facilities, or critical infrastructure."

---

## Success Criteria

We succeed if a judge can:

1. Enter a real location-selection question.
2. Watch the agent make autonomous research decisions.
3. See real public data influence the recommendation.
4. Inspect and verify randomly selected citations.
5. Understand why the recommendation was made — and what evidence could change it.

**Our pitch:** An explainable, agent-powered location intelligence platform that transforms freely available public data into researched, verifiable decisions.
