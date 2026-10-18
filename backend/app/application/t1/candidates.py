"""Build `candidates.json` (T1) from a prepared point, accepted findings and the fit call."""

from __future__ import annotations

from typing import Any

from app.application.t1.catalog import Catalog, load_catalog
from app.contracts.candidates import CandidatesDocument
from app.contracts.evidence import Finding
from app.evidence.ledger import EvidenceLedger

FIT_DIMENSIONS = ("Cost", "Operator fit", "Space fit")
FIT_ALIASES = {
    "cost": "Cost",
    "operator_fit": "Operator fit",
    "operator fit": "Operator fit",
    "space_fit": "Space fit",
    "space fit": "Space fit",
}
ACCEPTED = {"verified", "partial"}


def _as_dict(finding: Finding | dict[str, Any]) -> dict[str, Any]:
    return finding.model_dump() if isinstance(finding, Finding) else dict(finding)


def accepted_ids(finding: dict[str, Any]) -> list[str]:
    """Verified → all ids; partial → only the matching ids (§5.4)."""
    if finding.get("verification") == "partial":
        return list(finding.get("matched_ids") or finding.get("evidence_ids") or [])
    return list(finding.get("evidence_ids") or [])


def _hypothesis_of(finding: dict[str, Any], ids: set[str]) -> str | None:
    for key in ("hypothesis_id", "subquestion_id"):
        if finding.get(key) in ids:
            return finding[key]
    localization = str(finding.get("localization") or "")
    if localization.startswith("hypothesis:") and localization.split(":", 1)[1] in ids:
        return localization.split(":", 1)[1]
    tail = str(finding.get("agent") or "").rsplit(":", 1)[-1]
    return tail if tail in ids else None


def _stance(finding: dict[str, Any]) -> str:
    if finding.get("stance") in {"for", "against"}:
        return finding["stance"]
    if finding.get("claim_type") == "risk" or str(finding.get("agent") or "").startswith("skeptic"):
        return "against"
    return "for"


def _fit_entry(fit: dict[str, Any], hypothesis_id: str, name: str) -> dict[str, Any] | None:
    block = fit.get(hypothesis_id) or {}
    for key, value in block.items():
        if FIT_ALIASES.get(str(key).casefold(), key) == name:
            return value
    return None


class _Tiers:
    def __init__(self, ledger: EvidenceLedger | None):
        self.entries = {e["evidence_id"]: e for e in ledger.entries()} if ledger else None

    def known(self, ids: list[str]) -> list[str]:
        return ids if self.entries is None else [i for i in ids if i in self.entries]

    def is_web(self, evidence_id: str) -> bool:
        if self.entries is None:
            return not evidence_id.startswith("ds_")
        return self.entries[evidence_id].get("method") != "dataset"

    def weak_only(self, ids: list[str]) -> bool:
        if self.entries is None or not ids:
            return False
        return all((self.entries[i].get("tier") or "weak") == "weak" for i in ids)


def _fit_dimension(
    name: str,
    entry: dict[str, Any] | None,
    hypothesis: dict[str, Any],
    prepared: dict[str, Any],
    tiers: _Tiers,
    catalog: Catalog,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    neutral = catalog.neutral_score
    ids = tiers.known([str(i) for i in (entry or {}).get("evidence_ids") or []])
    if name == "Cost":
        ids = [i for i in ids if tiers.is_web(i)]
    operator = prepared.get("operator") or {}
    fields = [f for f in (entry or {}).get("profile_fields") or [] if f in operator]
    if name == "Cost":
        fields = []
    score = (entry or {}).get("score")
    if entry is None or score is None or not (ids or fields):
        validation = {
            "Cost": "Ask the landlord or a broker for the rent and ancillary costs of the space.",
            "Operator fit": "Check with the operator: "
            + ", ".join(hypothesis.get("operator_checks") or ["experience"])
            + ".",
            "Space fit": "Inspect the space for: "
            + ", ".join(hypothesis.get("space_checks") or ["layout"])
            + ".",
        }[name]
        dim = {
            "name": name,
            "score": neutral,
            "why": f"unknown: no {name.lower()} evidence (neutral {neutral:g})",
            "evidence_ids": [],
        }
        unknown = {
            "text": f"{name} for {hypothesis['name']} at {prepared['space']['address']} is unknown.",
            "suggested_validation": validation,
        }
        return dim, unknown
    value = min(10.0, max(0.0, float(score)))
    why = str(entry.get("why") or "")
    if tiers.weak_only(ids) and not fields and value > catalog.weak_tier_cap:
        value = catalog.weak_tier_cap
        why += f" (capped at {catalog.weak_tier_cap:g}: weak-tier evidence only)"
    if fields:
        why += f" [operator profile: {', '.join(fields)}]"
    dim = {"name": name, "score": round(value, 1), "why": why.strip(), "evidence_ids": ids}
    if fields:
        dim["profile_fields"] = fields
    return dim, None


def _risk_dimension(against: list[dict[str, Any]], catalog: Catalog) -> dict[str, Any]:
    neutral = catalog.neutral_score
    if not against:
        return {
            "name": "Regulation / risk",
            "score": neutral,
            "why": "no verified counter-evidence found (neutral)",
            "evidence_ids": [],
        }
    penalty = sum(
        catalog.risk_penalty.get(
            str(c.get("severity") or "unrated"), catalog.risk_penalty.get("unrated", 1.5)
        )
        for c in against
    )
    ids = sorted({i for c in against for i in c["evidence_ids"]})
    return {
        "name": "Regulation / risk",
        "score": round(max(0.0, neutral - penalty), 1),
        "why": f"{len(against)} verified AGAINST finding(s), penalty {penalty:g}",
        "evidence_ids": ids,
    }


def _weighted(
    dimensions: list[dict[str, Any]], weights: dict[str, float], names: set[str] | None = None
) -> float:
    selected = [d for d in dimensions if names is None or d["name"] in names]
    total = sum(weights.get(d["name"], 0.0) for d in selected)
    if total <= 0:
        return 0.0
    return round(sum(weights.get(d["name"], 0.0) * d["score"] for d in selected) / total, 2)


def build_candidates(
    prepared: dict,
    findings: list,
    fit: dict,
    *,
    ledger: EvidenceLedger | None = None,
    catalog: Catalog | None = None,
) -> CandidatesDocument:
    catalog = catalog or load_catalog(prepared.get("template_id") or "t1_catalog_v1")
    tiers = _Tiers(ledger)
    fit = fit or {}
    hypotheses = prepared["hypotheses"]
    ids = {h["id"] for h in hypotheses}
    claims: dict[str, dict[str, list[dict[str, Any]]]] = {
        h: {"for": [], "against": []} for h in ids
    }
    for raw in findings or []:
        finding = _as_dict(raw)
        if finding.get("verification") not in ACCEPTED or finding.get("status") == "rejected":
            continue
        evidence_ids = tiers.known(accepted_ids(finding))
        target = _hypothesis_of(finding, ids)
        if not evidence_ids or target is None:
            continue
        claims[target][_stance(finding)].append(
            {
                "text": finding["claim"],
                "evidence_id": evidence_ids[0],
                "evidence_ids": evidence_ids,
                "finding_id": finding.get("finding_id"),
                "quote": finding.get("quote"),
                "verification": finding.get("verification"),
                "severity": finding.get("severity"),
            }
        )
    data_names = set(catalog.data_dimensions)
    weights = catalog.dimension_weights
    rows = []
    for h in hypotheses:
        dimensions = [dict(d) for d in h["dimensions"] if d["name"] in data_names]
        unknowns: list[dict[str, str]] = []
        if prepared.get("low_resident_catchment"):
            unknowns.append(
                {
                    "text": "Fewer than 2,000 residents in the catchment; daytime footfall is not measured.",
                    "suggested_validation": "Count passers-by at the space on a weekday morning and evening.",
                }
            )
        for name in FIT_DIMENSIONS:
            dim, unknown = _fit_dimension(
                name, _fit_entry(fit, h["id"], name), h, prepared, tiers, catalog
            )
            dimensions.append(dim)
            if unknown:
                unknowns.append(unknown)
        dimensions.append(_risk_dimension(claims[h["id"]]["against"], catalog))
        rows.append(
            {
                "h": h,
                "dimensions": dimensions,
                "data_score": _weighted(dimensions, weights, data_names),
                "score": _weighted(dimensions, weights),
                "unknowns": unknowns,
            }
        )
    for rank, row in enumerate(sorted(rows, key=lambda r: (-r["data_score"], r["h"]["id"])), 1):
        row["rank_initial"] = rank
    for rank, row in enumerate(
        sorted(rows, key=lambda r: (-r["score"], -r["data_score"], r["h"]["id"])), 1
    ):
        row["rank_final"] = rank
    out = []
    for row in sorted(rows, key=lambda r: r["rank_final"]):
        h = row["h"]
        out.append(
            {
                "id": h["id"],
                "name": h["name"],
                "score": row["score"],
                "data_score": row["data_score"],
                "rank_initial": row["rank_initial"],
                "rank_final": row["rank_final"],
                "dimensions": row["dimensions"],
                "for": claims[h["id"]]["for"],
                "against": claims[h["id"]]["against"],
                "unknowns": row["unknowns"],
                "confidence": h.get("confidence"),
                "confidence_reasons": h.get("confidence_reasons", []),
                "why": h.get("why"),
            }
        )
    competitors = [c for c in prepared.get("competitors", []) if c["hypothesis_id"] in ids]
    return CandidatesDocument.model_validate(
        {
            "space": prepared["space"],
            "operator": prepared.get("operator"),
            "hypotheses": out,
            "competitors": competitors,
            "radius_m": prepared.get("radius_m", 500),
            "template_id": catalog.id,
            "catchment": prepared.get("catchment"),
            "low_resident_catchment": prepared.get("low_resident_catchment", False),
            "confidence": prepared.get("confidence"),
            "confidence_reasons": prepared.get("confidence_reasons", []),
        }
    )
