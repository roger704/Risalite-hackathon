"""Deterministic offline research (``cfg.fake``): synthetic ledger entries and findings."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.contracts.evidence import Finding
from app.evidence.ledger import EvidenceLedger, utc_now
from app.research.events import Events

FAKE_URL = "https://example.org/risalite-fake/"


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def fake_entry(ledger: EvidenceLedger, slug: str, title: str, passage: str) -> dict[str, Any]:
    return ledger.append(
        {
            "evidence_id": f"ev_{_digest('fake', slug)[:20]}",
            "method": "tavily-extract",
            "url": f"{FAKE_URL}{slug}",
            "original_url": f"{FAKE_URL}{slug}",
            "canonical_url": f"{FAKE_URL}{slug}",
            "title": title,
            "passage": passage,
            "kind": "extracted_page",
            "extraction_status": "ok",
            "provider": "fake",
            "tier": "weak",
            "confidence": "low",
            "retrieved_at": utc_now(),
            "status": "unverified",
            "fake": True,
        }
    )


async def fake_finding(
    events: Events,
    entry: dict[str, Any],
    *,
    claim: str,
    quote: str,
    claim_type: str,
    localization: str,
    **extra: Any,
) -> Finding:
    if quote not in entry["passage"]:
        raise ValueError("fake quote must be a substring of the fake passage")
    finding = Finding(
        finding_id=f"f_{_digest(events.agent, entry['evidence_id'], claim)[:20]}",
        claim=claim,
        quote=quote,
        evidence_ids=[entry["evidence_id"]],
        claim_type=claim_type,
        localization=localization,
        agent=events.agent,
        status="accepted",
        match="verified",
        fake=True,
        **{k: v for k, v in extra.items() if v is not None},
    )
    await events(
        "search",
        why=f"fake search for {claim_type} evidence",
        detail={"query": entry["title"], "results": 1, "evidence_ids": [entry["evidence_id"]]},
    )
    await events(
        "finding_accepted",
        why=claim,
        detail={
            "finding_id": finding.finding_id,
            "claim": claim,
            "claim_type": claim_type,
            "localization": localization,
            "evidence_ids": finding.evidence_ids,
            "match": "verified",
        },
    )
    return finding


def _name(item: dict[str, Any], default: str) -> str:
    return str(item.get("name") or item.get("label") or item.get("id") or default)


async def area(
    events: Events, ledger: EvidenceLedger, area: dict, cells: list[dict]
) -> list[Finding]:
    area_name = _name(area, "Berlin")
    first = cells[0] if cells else {}
    cell_id = str(first.get("id") or "area")
    cell_name = _name(first, area_name)
    housing = (
        f"Im Planungsraum {cell_name} ({area_name}) sollen bis 2028 rund 350 neue Wohnungen "
        "entstehen. Der Baubeginn ist für das kommende Jahr geplant."
    )
    market = (
        f"Die Gastronomie in {area_name} gilt als dicht; Gewerbemieten für kleine Ladenlokale "
        "liegen laut Maklerberichten bei 25 bis 35 Euro pro Quadratmeter."
    )
    e1 = fake_entry(
        ledger,
        f"area-{area.get('area_id', area_name)}-housing",
        f"Wohnungsbau {cell_name}",
        housing,
    )
    e2 = fake_entry(
        ledger,
        f"area-{area.get('area_id', area_name)}-market",
        f"Gewerbemieten {area_name}",
        market,
    )
    return [
        await fake_finding(
            events,
            e1,
            claim=f"About 350 new dwellings are planned in {cell_name} by 2028.",
            quote="sollen bis 2028 rund 350 neue Wohnungen entstehen",
            claim_type="future",
            localization=cell_id,
            kind="housing",
            unit="dwellings",
            amount=350.0,
            completion_year=2028,
        ),
        await fake_finding(
            events,
            e2,
            claim=f"Small retail units in {area_name} rent for about 25-35 EUR/m².",
            quote="Gewerbemieten für kleine Ladenlokale liegen laut Maklerberichten bei 25 bis 35 Euro",
            claim_type="estimate",
            localization="area",
        ),
    ]


async def cell(events: Events, ledger: EvidenceLedger, cell: dict) -> list[Finding]:
    cell_id = str(cell.get("id") or "cell")
    name = _name(cell, cell_id)
    project = (
        f"Am Rand von {name} baut eine Genossenschaft 120 Wohnungen, die Fertigstellung ist für "
        "2027 angekündigt. Im Erdgeschoss sind Gewerbeflächen vorgesehen."
    )
    context = (
        f"In {name} gibt es nur wenige Cafés, dafür viele Bäckereien. Anwohner klagen über eine "
        "langwierige Baustelle an der Hauptstraße bis Ende 2026."
    )
    e1 = fake_entry(ledger, f"cell-{cell_id}-project", f"Neubau {name}", project)
    e2 = fake_entry(ledger, f"cell-{cell_id}-context", f"Kiez {name}", context)
    return [
        await fake_finding(
            events,
            e1,
            claim=f"A cooperative is building 120 dwellings next to {name}, due in 2027.",
            quote="baut eine Genossenschaft 120 Wohnungen, die Fertigstellung ist für 2027 angekündigt",
            claim_type="future",
            localization=cell_id,
            kind="housing",
            unit="dwellings",
            amount=120.0,
            completion_year=2027,
        ),
        await fake_finding(
            events,
            e2,
            claim=f"{name} has few cafés but many bakeries.",
            quote="gibt es nur wenige Cafés, dafür viele Bäckereien",
            claim_type="fact",
            localization=cell_id,
        ),
        await fake_finding(
            events,
            e2,
            claim=f"A long construction site on the main street affects {name} until end of 2026.",
            quote="langwierige Baustelle an der Hauptstraße bis Ende 2026",
            claim_type="risk",
            localization=cell_id,
        ),
    ]


async def hypothesis(
    events: Events, ledger: EvidenceLedger, space: dict, hypothesis: dict, localization: str
) -> list[Finding]:
    address = str(space.get("address") or "Berlin")
    business = _name(hypothesis, "business")
    hid = str(hypothesis.get("id") or business)
    rent = (
        f"Für Ladenflächen rund um {address} werden derzeit 22 bis 30 Euro pro Quadratmeter "
        "verlangt. Die Fläche stand zuletzt drei Monate leer."
    )
    operator = (
        f"Ein ähnliches Konzept ({business}) in der Nachbarschaft läuft seit vier Jahren "
        "erfolgreich und hat sein Angebot erweitert."
    )
    e1 = fake_entry(ledger, f"t1-{hid}-{address}-rent", f"Gewerbemiete {address}", rent)
    e2 = fake_entry(ledger, f"t1-{hid}-{address}-operator", f"{business} Nachbarschaft", operator)
    return [
        await fake_finding(
            events,
            e1,
            claim=f"Retail space around {address} rents for about 22-30 EUR/m².",
            quote="werden derzeit 22 bis 30 Euro pro Quadratmeter verlangt",
            claim_type="estimate",
            localization=localization,
            dimension="cost",
            stance="neutral",
            hypothesis_id=hid,
        ),
        await fake_finding(
            events,
            e2,
            claim=f"A similar {business} nearby has operated successfully for four years.",
            quote="läuft seit vier Jahren erfolgreich",
            claim_type="fact",
            localization="area",
            dimension="operator_fit",
            stance="for",
            hypothesis_id=hid,
        ),
    ]


async def skeptic(events: Events, ledger: EvidenceLedger, cell: dict) -> list[Finding]:
    cell_id = str(cell.get("id") or "cell")
    name = _name(cell, cell_id)
    text = (
        f"Zwei Cafés in {name} haben im letzten Jahr geschlossen; die Betreiber nannten "
        "gestiegene Mieten als Grund."
    )
    entry = fake_entry(ledger, f"skeptic-{cell_id}", f"Schließungen {name}", text)
    return [
        await fake_finding(
            events,
            entry,
            claim=f"Two cafés in {name} closed last year, citing rising rents.",
            quote="haben im letzten Jahr geschlossen; die Betreiber nannten gestiegene Mieten als Grund",
            claim_type="risk",
            localization=cell_id,
        )
    ]
