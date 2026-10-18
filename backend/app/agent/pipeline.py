import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from app.agent.classify import classify
from app.agent.events import EventBus, TraceWriter
from app.agent.interfaces import FactChecker, Ledger, ModelClient
from app.agent.mock import MockResearch
from app.agent.plan import PlanProposal, plan
from app.agent.schemas import ModifierOutput, RiskOutput, WriterOutput
from app.agent.stand_ins import FixtureCellScorer, JsonlLedger
from app.contracts.api import (
    ApproveRequest,
    Classification,
    ResearchRequest,
    RunResult,
    RunState,
)
from app.contracts.cells import CellsFile, Modifier, Risk
from app.contracts.events import ResearchEvent
from app.contracts.evidence import EvidenceEntry
from app.domain.research import (
    BudgetExceededError,
    EvidencePage,
    ResearchBudget,
    ResearchExtract,
    ResearchSearch,
)


def strip_unknown_citations(markdown: str, evidence_ids: set[str]) -> str:
    markdown = re.sub(
        r"\[e([^\]]+)\]",
        lambda match: match.group(0) if f"e{match.group(1)}" in evidence_ids else "",
        markdown,
    )
    return re.sub(r"\[(\d+)\]", "", markdown)


class Orchestrator:
    def __init__(
        self,
        runs_dir: Path,
        bus: EventBus,
        model: ModelClient | None,
        research,
        scorer_factory: Callable = FixtureCellScorer,
        fact_checker: FactChecker | None = None,
        budget_factory: Callable[[], ResearchBudget] | None = None,
    ):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self.model = model
        self.research = research
        self.scorer_factory = scorer_factory
        self.fact_checker = fact_checker
        self.budget_factory = budget_factory or (lambda: ResearchBudget(12, 10, 12, 60000))
        self.budgets: dict[str, ResearchBudget] = {}
        self.exhausted_runs: set[str] = set()

    def _dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def _save_state(self, state: RunState) -> None:
        (self._dir(state.run_id) / "state.json").write_text(
            state.model_dump_json(indent=2), encoding="utf-8"
        )

    def get_state(self, run_id: str) -> RunState | None:
        path = self._dir(run_id) / "state.json"
        return (
            RunState.model_validate_json(path.read_text(encoding="utf-8"))
            if path.exists()
            else None
        )

    def events(self, run_id: str) -> list[ResearchEvent]:
        path = self._dir(run_id) / "trace.jsonl"
        return (
            [
                ResearchEvent.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if path.exists()
            else []
        )

    async def _model_call(
        self,
        run_id,
        writer,
        budget,
        role,
        operation,
        context,
        output_model,
        evidence=(),
        stage="research",
    ):
        if not self.model or run_id in self.exhausted_runs:
            return None
        start = datetime.now(timezone.utc)
        try:
            if not getattr(self.model, "records_budget", False):
                budget.consume_inference_call()
                budget.require_tokens()
            result, tokens = await self.model.structured(
                role, operation, context, output_model, evidence
            )
            if not getattr(self.model, "records_budget", False):
                budget.record_tokens(tokens)
            await writer.emit(
                stage,
                role,
                "llm",
                f"{role} structured call complete",
                f"Structured {operation} response from the configured model.",
                model=self.model.model_name(role),
                tokens=tokens,
                seconds=(datetime.now(timezone.utc) - start).total_seconds(),
            )
            return result
        except BudgetExceededError as exc:
            self.exhausted_runs.add(run_id)
            await writer.emit(
                "budget",
                role,
                "budget_exhausted",
                str(exc),
                "Further calls are disabled at the configured budget.",
                data=budget.snapshot(),
            )
            return None
        except Exception as exc:
            await writer.emit(
                stage,
                role,
                "llm",
                f"{role} call failed: {type(exc).__name__}",
                "Using deterministic fallback after model failure.",
                model=self.model.model_name(role),
            )
            return None

    async def start(self, request: ResearchRequest) -> RunState:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self._dir(run_id)
        run_dir.mkdir(parents=True)
        writer = TraceWriter(run_dir, run_id, self.bus)
        budget = self.budgets[run_id] = self.budget_factory()
        if self.model and getattr(self.model, "records_budget", False):
            self.model.gateway.budget = budget
        if self.research and hasattr(self.research, "budget"):
            self.research.budget = budget
        started = datetime.now(timezone.utc)
        classification = await self._model_call(
            run_id,
            writer,
            budget,
            "classifier",
            "plan",
            {"question": request.question, "profiles": request.profiles or {}},
            Classification,
            stage="classify",
        )
        if classification is None:
            classification = await classify(request.question, request.profiles)
        else:
            classification = Classification.model_validate(classification.model_dump())
        await writer.emit(
            "classify",
            "Classifier",
            "classify",
            classification.why,
            "Classify the question without guessing an unspecified area.",
        )
        state = RunState(
            run_id=run_id,
            status="needs_clarification"
            if classification.needs_clarification
            else "awaiting_approval",
            question=request.question,
            created_at=started,
            classification=classification,
        )
        if not classification.needs_clarification:
            proposed = await self._model_call(
                run_id,
                writer,
                budget,
                "planner",
                "plan",
                {"classification": classification.model_dump()},
                PlanProposal,
                stage="plan",
            )
            state.plan = await plan(classification)
            if proposed:
                adjustments = []
                for adjustment in proposed.weight_adjustments:
                    layer = next(
                        (item for item in state.plan.layers if item.id == adjustment.layer_id),
                        None,
                    )
                    if layer and adjustment.reason.strip() and adjustment.to_weight >= 0:
                        layer.weight = adjustment.to_weight
                        adjustments.append(adjustment)
                total_weight = sum(layer.weight for layer in state.plan.layers)
                if total_weight:
                    for layer in state.plan.layers:
                        layer.weight /= total_weight
                state.plan.weight_adjustments = adjustments
            await writer.emit(
                "plan",
                "Planner",
                "plan",
                "Research plan prepared.",
                "Template defaults with optional reasoned weight adjustments.",
            )
            for paid in state.plan.paid_sources:
                await writer.emit(
                    "plan", "Planner", "skip", paid.name, "key not provided — skipped"
                )
            (run_dir / "plan.json").write_text(
                state.plan.model_dump_json(indent=2), encoding="utf-8"
            )
        self._save_state(state)
        return state

    async def approve(self, run_id: str, edits: ApproveRequest | None = None) -> RunState:
        state = self.get_state(run_id)
        if state is None:
            raise LookupError("unknown run")
        if state.status != "awaiting_approval" or state.plan is None:
            raise ValueError("run is not awaiting approval")
        edits = edits or ApproveRequest()
        edited = bool(edits.layers or edits.sub_questions)
        for update in edits.layers or []:
            layer = next((item for item in state.plan.layers if item.id == update.id), None)
            if layer:
                if update.enabled is not None:
                    layer.enabled = update.enabled
                if update.weight is not None and update.weight >= 0:
                    layer.weight = update.weight
        if edits.layers:
            total = sum(item.weight for item in state.plan.layers if item.enabled)
            if total:
                for item in state.plan.layers:
                    if item.enabled:
                        item.weight /= total
        for update in edits.sub_questions or []:
            question = next(
                (item for item in state.plan.sub_questions if item.id == update.id), None
            )
            if question:
                question.enabled = update.enabled
        state.status = "running"
        writer = TraceWriter(self._dir(run_id), run_id, self.bus)
        await writer.emit(
            "approve",
            "User",
            "approve",
            "Plan approved.",
            "edited by user" if edited else "Approved without edits.",
        )
        (self._dir(run_id) / "plan.json").write_text(
            state.plan.model_dump_json(indent=2), encoding="utf-8"
        )
        self._save_state(state)
        return state

    async def _search_page(self, run_id, writer, budget, query, domains) -> EvidencePage:
        started = datetime.now(timezone.utc)
        if isinstance(self.research, MockResearch):
            budget.consume_search()
        page = await self.research.search(
            ResearchSearch(query=query, include_domains=tuple(domains), max_results=3)
        )
        await writer.emit(
            "research",
            "Web researcher",
            "tavily:search",
            query,
            "Domain-restricted first.",
            seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            data={"results": len(page.evidence)},
        )
        if not page.evidence and domains:
            if isinstance(self.research, MockResearch):
                budget.consume_search()
            page = await self.research.search(ResearchSearch(query=query, max_results=3))
            await writer.emit(
                "research",
                "Web researcher",
                "tavily:search",
                query,
                "Domain-restricted first; widened because empty.",
                data={"results": len(page.evidence)},
            )
        return page

    async def execute(self, run_id: str) -> None:
        state = self.get_state(run_id)
        if not state or state.status != "running":
            return
        run_dir = self._dir(run_id)
        writer = TraceWriter(run_dir, run_id, self.bus)
        budget = self.budgets.setdefault(run_id, self.budget_factory())
        ledger = JsonlLedger(run_dir / "evidence.jsonl")
        try:
            layers = [item for item in state.plan.layers if item.enabled]
            scorer = self.scorer_factory()
            requested_area = state.classification.area or "Friedrichshain-Kreuzberg"
            cells = scorer.build_cells(requested_area, layers, ledger)
            if cells.area.name != requested_area:
                await writer.emit(
                    "research",
                    "Data researcher",
                    "fixture_fallback",
                    f"Using the Friedrichshain-Kreuzberg fixture for {requested_area}.",
                    "No area-specific synthetic fixture is available.",
                )
            await writer.emit(
                "research",
                "Data researcher",
                "tool:fixture",
                "Synthetic PLR fixture loaded.",
                "Offline scorer fixture; no live data requests.",
            )
            sorted_cells = sorted(
                cells.cells, key=lambda cell: cell.score_present or 0, reverse=True
            )
            stop_research = self.research is None or run_id in self.exhausted_runs
            if stop_research:
                await writer.emit(
                    "research",
                    "Web researcher",
                    "skip",
                    "Web research unavailable.",
                    "TAVILY_API_KEY not provided — web research skipped",
                )
            if self.research:
                for cell in sorted_cells[:5]:
                    enabled_questions = [item for item in state.plan.sub_questions if item.enabled]
                    for subq in enabled_questions[:2]:
                        query = f"{subq.text} {cell.name} Berlin"
                        try:
                            page = await self._search_page(
                                run_id, writer, budget, query, subq.domains
                            )
                            if not page.evidence:
                                continue
                            for item in page.evidence:
                                quote = (item.passage or item.snippet or "")[:300]
                                if quote:
                                    ledger.add(
                                        EvidenceEntry(
                                            evidence_id="pending",
                                            method="web",
                                            url=item.canonical_url,
                                            title=item.title,
                                            quote=quote,
                                            tier=self._tier(item.canonical_url),
                                            retrieved_at=item.retrieved_at,
                                            published_at=item.published_at,
                                            cell_id=cell.id,
                                            claim_type="web",
                                            query=query,
                                        )
                                    )
                            urls = list(
                                dict.fromkeys(item.canonical_url for item in page.evidence)
                            )[:1]
                            if isinstance(self.research, MockResearch):
                                budget.consume_extract_urls(len(urls))
                            started = datetime.now(timezone.utc)
                            extracted = await self.research.extract(
                                ResearchExtract(urls=tuple(urls), query=query)
                            )
                            await writer.emit(
                                "research",
                                "Web researcher",
                                "tavily:extract",
                                query,
                                "Extract verbatim passages from the leading result URLs.",
                                seconds=(datetime.now(timezone.utc) - started).total_seconds(),
                                data={"urls": len(urls)},
                            )
                            for item in extracted.evidence:
                                quote = (item.passage or item.snippet or "")[:300]
                                if not quote:
                                    continue
                                ledger.add(
                                    EvidenceEntry(
                                        evidence_id="pending",
                                        method="web",
                                        url=item.canonical_url,
                                        title=item.title,
                                        quote=quote,
                                        tier=self._tier(item.canonical_url),
                                        retrieved_at=item.retrieved_at,
                                        published_at=item.published_at,
                                        cell_id=cell.id,
                                        claim_type="web",
                                        query=query,
                                    )
                                )
                        except BudgetExceededError as exc:
                            stop_research = True
                            await writer.emit(
                                "budget",
                                "Web researcher",
                                "budget_exhausted",
                                str(exc),
                                "Stop further research calls and continue with deterministic review and writing.",
                                data=budget.snapshot(),
                            )
                            break
                    if stop_research:
                        break
            cells = scorer.rescore(cells, state.plan.layers)
            sorted_cells = sorted(
                cells.cells, key=lambda cell: cell.score_present or 0, reverse=True
            )
            # Future claims are accepted only when the source quote itself states a future year.
            leader = sorted_cells[0]
            top_evidence = [
                entry
                for entry in ledger.all()
                if entry.cell_id == leader.id and entry.method == "web"
            ]
            proposal = None
            if top_evidence and not stop_research:
                proposal = await self._model_call(
                    run_id,
                    writer,
                    budget,
                    "web_researcher",
                    "synthesize",
                    {
                        "cell_id": leader.id,
                        "evidence": [entry.model_dump(mode="json") for entry in top_evidence],
                    },
                    ModifierOutput,
                    stage="research",
                )
            if proposal is None:
                proposal = ModifierOutput(
                    modifiers=[
                        {"evidence_id": entry.evidence_id, "label": "Planned development"}
                        for entry in top_evidence
                        if re.search(r"\b20\d{2}\b", entry.quote or "")
                    ]
                )
            if proposal:
                known = {entry.evidence_id: entry for entry in top_evidence}
                for item in proposal.modifiers:
                    entry = known.get(item.evidence_id)
                    if not entry or not re.search(r"\b20\d{2}\b", entry.quote or ""):
                        continue
                    years = [int(year) for year in re.findall(r"\b20\d{2}\b", entry.quote)]
                    if not years or max(years) <= date.today().year:
                        continue
                    magnitude_match = re.search(
                        r"(\d[\d.]*)\s*(Wohnungen|WE|Arbeitsplätze)",
                        entry.quote or "",
                        re.IGNORECASE,
                    )
                    magnitude = (
                        {"units": int(magnitude_match.group(1).replace(".", ""))}
                        if magnitude_match
                        else None
                    )
                    leader.modifiers.append(
                        Modifier(
                            label=item.label,
                            quote=entry.quote,
                            evidence_id=entry.evidence_id,
                            localization="address"
                            if re.search(
                                r"\b[\wäöüß.-]+(?:straße|str\.|platz)\s+\d+", entry.quote, re.I
                            )
                            else "plr",
                            magnitude=magnitude,
                        )
                    )
            if run_id in self.exhausted_runs:
                stop_research = True
            cells = scorer.rescore(cells, state.plan.layers)
            await writer.emit(
                "research",
                "Scorer",
                "rescore",
                "Present and future scores computed.",
                "Renormalize enabled layer weights across available values.",
            )
            leader = max(cells.cells, key=lambda cell: cell.score_present or 0)
            evidence_for_leader = [entry for entry in ledger.all() if entry.cell_id == leader.id]
            risk_result = None
            if not stop_research:
                risk_result = await self._model_call(
                    run_id,
                    writer,
                    budget,
                    "skeptic",
                    "review",
                    {
                        "cell_id": leader.id,
                        "evidence": [
                            entry.model_dump(mode="json") for entry in evidence_for_leader
                        ],
                    },
                    RiskOutput,
                    stage="skeptic",
                )
            if risk_result is None:
                risk_result = RiskOutput(
                    risks=[
                        {
                            "evidence_id": entry.evidence_id,
                            "label": "Commercial rent pressure",
                            "severity": "high",
                            "claim_type": "web",
                        }
                        for entry in evidence_for_leader
                        if "Erdgeschossmieten" in (entry.quote or "")
                    ]
                )
            if risk_result:
                for proposal in risk_result.risks:
                    entry = ledger.get(proposal.evidence_id)
                    if entry and entry.quote:
                        leader.risks.append(
                            Risk(
                                label=proposal.label,
                                quote=entry.quote,
                                evidence_id=entry.evidence_id,
                                severity=proposal.severity,
                                claim_type=proposal.claim_type,
                            )
                        )
                        await writer.emit(
                            "skeptic",
                            "Skeptic",
                            "challenge",
                            proposal.label,
                            "Risk quote copied verbatim from the evidence ledger.",
                            data={"evidence_id": entry.evidence_id, "severity": proposal.severity},
                        )
            if any(risk.severity == "high" for risk in leader.risks):
                before_rank = (
                    sorted(
                        cells.cells, key=lambda cell: cell.score_present or 0, reverse=True
                    ).index(leader)
                    + 1
                )
                leader.demoted = True
                cells = scorer.rescore(cells, state.plan.layers)
                ordered_after = sorted(
                    cells.cells, key=lambda cell: cell.score_present or 0, reverse=True
                )
                after_rank = ordered_after.index(leader) + 1
                await writer.emit(
                    "rescore",
                    "Scorer",
                    "rescore",
                    "High-risk cell demoted.",
                    "A high-severity skeptic risk subtracts 0.15 after normalization.",
                    data={
                        "cell_id": leader.id,
                        "before_rank": before_rank,
                        "after_rank": after_rank,
                        "reason": "high risk",
                    },
                )
            if self.fact_checker:
                checked = await self.fact_checker.check(ledger.all())
                for entry in checked:
                    if entry.status == "unsupported":
                        await writer.emit(
                            "fact-check",
                            "Fact checker",
                            "reject",
                            "Evidence unsupported.",
                            "Unsupported evidence is excluded from claims.",
                            data={"evidence_id": entry.evidence_id},
                        )
                if checked:
                    # Preserve stable ledger IDs while persisting fact-check statuses.
                    (run_dir / "evidence.jsonl").write_text(
                        "".join(item.model_dump_json() + "\n" for item in checked), encoding="utf-8"
                    )
                    ledger = JsonlLedger(run_dir / "evidence.jsonl")
            output = None
            if not stop_research:
                output = await self._model_call(
                    run_id,
                    writer,
                    budget,
                    "writer",
                    "synthesize",
                    {
                        "evidence": [entry.model_dump(mode="json") for entry in ledger.all()],
                        "cells": cells.model_dump(mode="json"),
                    },
                    WriterOutput,
                    stage="write",
                )
            markdown = self._render(cells, ledger, output)
            markdown = strip_unknown_citations(markdown, ledger.ids())
            (run_dir / "report.md").write_text(markdown, encoding="utf-8")
            (run_dir / "cells.json").write_text(cells.model_dump_json(indent=2), encoding="utf-8")
            await writer.emit(
                "write",
                "Writer",
                "write",
                "Report and cells written.",
                "All citations are checked against the run evidence ledger.",
            )
            state.status = "done"
            self._save_state(state)
            await writer.emit(
                "done",
                "Orchestrator",
                "done",
                "Research run completed.",
                "All pipeline stages completed.",
            )
        except Exception as exc:
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            self._save_state(state)
            await writer.emit(
                "error", "Orchestrator", "error", state.error, "Unhandled pipeline error."
            )

    @staticmethod
    def _tier(url: str) -> str:
        host = url.split("/", 3)[2].lower()
        if host == "berlin.de" or host.endswith(".berlin.de"):
            return "official"
        if host == "ihk.de" or host.endswith(".ihk.de"):
            return "institutional"
        if any(
            domain in host for domain in ("tagesspiegel", "rbb24", "morgenpost", "berliner-zeitung")
        ):
            return "press"
        return "weak"

    @staticmethod
    def _render(cells: CellsFile, ledger: Ledger, output: WriterOutput | None) -> str:
        lines = ["## Summary (written for the default layer mix)", ""]
        if output:
            for sentence in output.sentences:
                citations = " ".join(f"[{item}]" for item in sentence.evidence_ids)
                lines.append(
                    f"{sentence.text} {citations} <!-- claim_type: {sentence.claim_type} -->".strip()
                )
        else:
            lines.append(
                "Synthetic fixture scores are proxies, not observed market measurements. <!-- claim_type: proxy -->"
            )
        lines.extend(["", "## Findings", ""])
        ordered = sorted(cells.cells, key=lambda cell: cell.score_present or 0, reverse=True)
        for index, cell in enumerate(ordered[:3], start=1):
            lines.append(f"{index}. **{cell.name}** — score {cell.score_present:.3f}")
            for layer in cells.layers:
                value = cell.values.get(layer.id)
                if value is not None:
                    lines.append(f"   - {layer.label}: {value:.2f} [{layer.evidence_id}]")
            for modifier in cell.modifiers:
                lines.append(
                    f"   - Future: {modifier.label} — “{modifier.quote}” [{modifier.evidence_id}]"
                )
            for risk in cell.risks:
                lines.append(
                    f"   - Risk ({risk.severity}): {risk.label} — “{risk.quote}” [{risk.evidence_id}]"
                )
        return "\n".join(lines) + "\n"

    def result(self, run_id: str) -> RunResult | None:
        state = self.get_state(run_id)
        if not state:
            return None
        run_dir = self._dir(run_id)
        cells_path, report_path, evidence_path = (
            run_dir / "cells.json",
            run_dir / "report.md",
            run_dir / "evidence.jsonl",
        )
        cells = (
            CellsFile.model_validate_json(cells_path.read_text()) if cells_path.exists() else None
        )
        evidence = (
            [
                EvidenceEntry.model_validate_json(line)
                for line in evidence_path.read_text().splitlines()
                if line
            ]
            if evidence_path.exists()
            else []
        )
        return RunResult(
            run_id=run_id,
            status=state.status,
            cells=cells,
            report_md=report_path.read_text() if report_path.exists() else None,
            evidence=evidence,
            trace=self.events(run_id),
        )
