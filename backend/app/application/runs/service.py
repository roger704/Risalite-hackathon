"""Live run lifecycle: create → (clarify) → plan → approve → background pipeline → result."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.evidence.ledger import EvidenceLedger

from .events import EventLog
from .pipeline import Context, Deps, RunFailure, execute
from .planning import (
    CLARIFY_AREA,
    ambiguous,
    build_plan,
    candidates,
    clarify_question,
    classify,
    match_areas,
    pick_from_answer,
)

TERMINAL = {"done", "failed"}
MAX_RUNS = 500


class RunError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RunService:
    def __init__(self, deps: Deps, runs_dir: Path):
        self.deps = deps
        self.runs_dir = Path(runs_dir)
        self.runs: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, EventLog] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    # access ---------------------------------------------------------------------------
    def get(self, run_id: str, authorization: str | None) -> dict[str, Any]:
        run = self.runs.get(run_id)
        token = authorization.removeprefix("Bearer ") if authorization else ""
        if not run or not secrets.compare_digest(run["token_hash"], token_hash(token)):
            raise RunError(404, "Run not found")
        return run

    def log(self, run_id: str) -> EventLog:
        return self.logs[run_id]

    def response(self, run: dict[str, Any], token: str) -> dict[str, Any]:
        return {
            "run_id": run["run_id"],
            "run_token": token,
            "status": run["status"],
            "clarify_question": run.get("clarify_question"),
            "plan": run.get("plan"),
            "mode": "live",
            "question": run["question"],
            "role": run.get("role"),
            "profile": run.get("profile"),
            "shape": run["shape"],
            "classification": run["classification"],
        }

    def _persist(self, run: dict[str, Any]) -> None:
        run_dir = self.runs_dir / run["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in run.items() if k not in {"result", "prepared"}}
        (run_dir / "run.json").write_text(
            json.dumps(keep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        if run.get("plan"):
            (run_dir / "plan.json").write_text(
                json.dumps(
                    {"plan": run["plan"], "approval": run.get("approval")},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _evict(self) -> None:
        while len(self.runs) >= MAX_RUNS:
            idle = [
                r
                for r in self.runs.values()
                if r["status"] in TERMINAL | {"needs_clarification", "awaiting_approval"}
            ]
            if not idle:
                raise RunError(503, "run store is full")
            oldest = min(idle, key=lambda r: r["created_at"])
            self.runs.pop(oldest["run_id"])
            self.logs.pop(oldest["run_id"], None)

    # create / clarify -----------------------------------------------------------------
    async def create(
        self, question: str, role: str | None, profile: str | None, template_hint: str | None
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise RunError(422, "question is required")
        self._evict()
        classification = classify(question, template_hint)
        if classification["shape"] == "point" and self.deps.t1 is None:
            raise RunError(
                501,
                "T1 point/address research is not available: app.application.t1 is not installed",
            )
        run_id, token = str(uuid4()), secrets.token_urlsafe(24)
        run = {
            "run_id": run_id,
            "token_hash": token_hash(token),
            "question": question,
            "role": role,
            "profile": profile,
            "shape": classification["shape"],
            "classification": classification,
            "status": "needs_clarification",
            "created_at": time.time(),
        }
        self.runs[run_id] = run
        self.logs[run_id] = EventLog(self.runs_dir / run_id, run_id)
        self.logs[run_id].emit("classify", "classifier", classification, why=classification["why"])
        try:
            if run["shape"] == "point":
                self._plan_point(run)
            else:
                self._resolve(run, question)
        except RunError:
            self.runs.pop(run_id, None)
            raise
        self._persist(run)
        return self.response(run, token)

    def _plan_point(
        self,
        run: dict[str, Any],
        query: str | None = None,
        *,
        lat: float | None = None,
        lon: float | None = None,
    ) -> None:
        """T1: resolve the address and prepare hypotheses; ambiguous/unknown → clarify."""
        log = self.logs[run["run_id"]]
        query = query or run["classification"].get("address") or run["question"]
        readers = self.deps.t1_readers() if self.deps.t1_readers else None
        try:
            prepared = self.deps.t1.prepare_point(
                query,
                lat=lat,
                lon=lon,
                operator_profile=_profile(run.get("profile")),
                radius_m=500,
                readers=readers,
                ledger=EvidenceLedger(self.runs_dir / run["run_id"]),
            )
        except ValueError as exc:
            prepared = {"clarify": [], "reason": str(exc)}
        if "clarify" in prepared:
            options = list(prepared.get("clarify") or [])
            run["status"] = "needs_clarification"
            run["pending_points"] = options
            run["plan"] = None
            labels = "; ".join(f"{i}) {_point_label(o)}" for i, o in enumerate(options, 1))
            run["clarify_question"] = (
                f"Which address do you mean? {labels}. Answer with the number or the full address."
                if options
                else f"I could not find that address ({prepared.get('reason', 'no match')}). Please give street, house number and postcode."
            )
            log.emit(
                "clarify",
                "orchestrator",
                {
                    "question": run["clarify_question"],
                    "candidates": [_point_label(o) for o in options],
                },
                why="Never guess the address",
            )
            return
        run["prepared"] = prepared
        run.pop("pending_points", None)
        run["classification"]["address"] = prepared.get("space", {}).get("address") or run[
            "classification"
        ].get("address")
        run["plan"] = {
            "sub_questions": [],
            "hypotheses": [
                {
                    k: h.get(k)
                    for k in (
                        "id",
                        "name",
                        "prescore",
                        "preferred",
                        "why",
                        "confidence",
                        "dimensions",
                        "metrics",
                    )
                }
                for h in prepared.get("hypotheses", [])
            ],
            "layers": [],
            "paid_sources": [],
            "area_candidates": [],
            "template_id": prepared.get("template_id"),
            "space": prepared.get("space"),
        }
        run["status"] = "awaiting_approval"
        run["clarify_question"] = None
        log.emit(
            "plan",
            "planner",
            {
                "hypotheses": [h.get("id") for h in run["plan"]["hypotheses"]],
                "address": run["classification"]["address"],
            },
            why="Hypotheses pre-scored on data",
        )

    def _resolve(self, run: dict[str, Any], text: str) -> None:
        units = self.deps.port.units()
        hits = match_areas(text, units)
        log = self.logs[run["run_id"]]
        options = ambiguous(hits)
        if not hits or options:
            run["status"] = "needs_clarification"
            run["pending_areas"] = [u.area_id for u in options]
            run["clarify_question"] = clarify_question(options) if options else CLARIFY_AREA
            run["plan"] = None
            log.emit(
                "clarify",
                "orchestrator",
                {"question": run["clarify_question"], "candidates": run["pending_areas"]},
                why="Never guess the area",
            )
            return
        self._plan_area(run, hits[0], hits)

    def _plan_area(self, run: dict[str, Any], selected: Any, hits: list[Any]) -> None:
        units = self.deps.port.units()
        template = self.deps.scoring.load_template(run["classification"]["template"])
        unmapped = self.deps.port.unmapped_layers(template)
        run["plan"] = build_plan(
            run["shape"], selected, candidates(selected, hits, units), template, unmapped
        )
        run["classification"]["area"] = selected.name
        run["status"] = "awaiting_approval"
        run["clarify_question"] = None
        run.pop("pending_areas", None)
        log = self.logs[run["run_id"]]
        log.emit("area", "orchestrator", run["plan"]["area"], why="Resolved to a stable LOR id")
        log.emit(
            "plan",
            "planner",
            {
                "template": template.id,
                "layers": [layer["id"] for layer in run["plan"]["layers"]],
                "unmapped": unmapped,
            },
        )

    def clarify(self, run: dict[str, Any], answer: str, token: str) -> dict[str, Any]:
        if run["status"] != "needs_clarification":
            raise RunError(409, "Run is not awaiting clarification")
        if run["shape"] == "point":
            self._clarify_point(run, answer)
            run["created_at"] = time.time()
            self._persist(run)
            return self.response(run, token)
        units = {u.area_id: u for u in self.deps.port.units()}
        options = [units[a] for a in run.get("pending_areas", []) if a in units]
        picked = pick_from_answer(answer, options) if options else None
        if picked is None and answer.strip() in units:
            picked = units[answer.strip()]
        if picked is not None:
            self._plan_area(run, picked, options or [picked])
        else:
            run["question"] = f"{run['question']} {answer.strip()}"
            hits = match_areas(answer, list(units.values())) or match_areas(
                run["question"], list(units.values())
            )
            if hits and not ambiguous(hits):
                self._plan_area(run, hits[0], hits)
            else:
                self._resolve(run, answer if hits else run["question"])
        run["created_at"] = time.time()
        self._persist(run)
        return self.response(run, token)

    def _clarify_point(self, run: dict[str, Any], answer: str) -> None:
        options = run.get("pending_points") or []
        value = answer.strip()
        picked = None
        if value.isdigit() and 1 <= int(value) <= len(options):
            picked = options[int(value) - 1]
        else:
            picked = next(
                (
                    o
                    for o in options
                    if value.casefold()
                    in {_point_label(o).casefold(), str(o.get("stable_id", "")).casefold()}
                ),
                None,
            )
        if picked is not None and picked.get("lat") is not None:
            self._plan_point(
                run,
                picked.get("address") or _point_label(picked),
                lat=picked["lat"],
                lon=picked["lon"],
            )
        else:
            run["question"] = f"{run['question']} {value}"
            self._plan_point(run, value)

    def search_areas(self, query: str) -> list[dict[str, Any]]:
        return self.deps.port.search_areas(query)

    # approve --------------------------------------------------------------------------
    def approve(
        self, run: dict[str, Any], body: dict[str, Any], provided: set[str]
    ) -> dict[str, Any]:
        if run["status"] != "awaiting_approval":
            raise RunError(409, "Run is not awaiting approval")
        plan = run["plan"] or {}
        subs = body.get("subquestions")
        if subs is not None:
            if len(subs) > 3:
                raise RunError(422, "At most three subquestions may be approved")
            known = {s["id"] for s in plan.get("sub_questions", [])}
            if any(s not in known for s in subs):
                raise RunError(422, "Unknown subquestion id")
        layer_ids = {layer["id"] for layer in plan.get("layers", [])}
        for layer in body.get("layers") or []:
            if layer["id"] not in layer_ids:
                raise RunError(422, f"Unknown layer id: {layer['id']}")
        if run["shape"] == "point" and "hypothesis_ids" in provided:
            planned = {h["id"] for h in plan.get("hypotheses", [])}
            ids = body.get("hypothesis_ids") or []
            if not ids or any(h not in planned for h in ids):
                raise RunError(
                    422, "Approved hypothesis_ids must be non-empty and present in the plan"
                )
        area_id = body.get("area_id")
        if area_id is not None and run["shape"] != "point":
            units = {u.area_id: u for u in self.deps.port.units()}
            if area_id not in units:
                raise RunError(422, f"Unknown area_id: {area_id}")
            if area_id != plan["area"]["area_id"]:
                hits = [
                    units[c["area_id"]]
                    for c in plan.get("area_candidates", [])
                    if c["area_id"] in units
                ]
                self._plan_area(run, units[area_id], hits)
                self._persist(run)
                return {
                    **self.response(run, ""),
                    "status": "awaiting_approval",
                    "plan": run["plan"],
                }
        run["approval"] = body
        run["status"] = "approved"
        run["approved_at"] = time.time()
        self.logs[run["run_id"]].emit(
            "approved", "orchestrator", body, why="The user approved the research plan."
        )
        self._persist(run)
        self.tasks[run["run_id"]] = asyncio.create_task(self._execute(run))
        return {"status": "approved"}

    async def _execute(self, run: dict[str, Any]) -> None:
        run_dir = self.runs_dir / run["run_id"]
        log = self.logs[run["run_id"]]
        ctx = Context(run, run_dir, log, EvidenceLedger(run_dir), self.deps)
        run["status"] = "running"
        try:
            result = await execute(ctx)
            (run_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
            )
            run["result"] = result
            run["status"] = "done"
        except Exception as exc:
            message = str(exc) if isinstance(exc, RunFailure) else f"{type(exc).__name__}: {exc}"
            run["error"] = message
            log.emit("error", "orchestrator", {"error": message}, why="Run failed")
            log.emit("status", "orchestrator", {"status": "failed"})
            run["status"] = "failed"
        finally:
            self._persist(run)
            self.tasks.pop(run["run_id"], None)

    # result ---------------------------------------------------------------------------
    def result(self, run: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if run["status"] == "done":
            return 200, run["result"]
        if run["status"] == "failed":
            return 200, {
                "run_id": run["run_id"],
                "status": "failed",
                "error": run.get("error"),
                "report_md": "",
                "shape": run["shape"],
                "question": run["question"],
                "plan": run.get("plan"),
                "trace": list(self.logs[run["run_id"]].events),
            }
        return 202, {"status": run["status"]}


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _profile(profile: str | dict | None) -> dict | None:
    if profile is None or isinstance(profile, dict):
        return profile
    text = profile.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {"description": text}
        except ValueError:
            pass
    return {"description": text} if text else None


def _point_label(option: dict[str, Any]) -> str:
    return str(
        option.get("label")
        or option.get("address")
        or " ".join(
            str(option.get(k) or "") for k in ("street", "house_number", "postcode")
        ).strip()
    )
