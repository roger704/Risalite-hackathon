from app.contracts.evidence import Finding
from app.research.writer import CellSection, Report, Sentence, render


def _finding(localization, evidence_id, **extra):
    return Finding(
        finding_id=f"f_{evidence_id}",
        claim="A supported claim.",
        quote="A supported quote.",
        evidence_ids=[evidence_id],
        claim_type="fact",
        localization=localization,
        agent="researcher",
        status="accepted",
        verification="verified",
        **extra,
    )


def test_top_item_citations_are_localized_but_summary_is_global():
    result = {
        "cells": {"cells": [{"id": "02010101", "name": "Askanischer Platz"}]},
    }
    report = Report(
        summary=[Sentence(text="Area summary.", evidence_ids=["ev_area"])],
        cells=[
            CellSection(
                cell_id="02010101",
                reasons=[
                    Sentence(text="Area-only reason.", evidence_ids=["ev_area"]),
                    Sentence(text="Dataset reason.", evidence_ids=["ds_population"]),
                    Sentence(text="Cell reason.", evidence_ids=["ev_cell"]),
                ],
            )
        ],
    )

    rendered = render(
        report,
        result,
        {"ev_area", "ev_cell", "ds_population"},
        "Test report",
        findings=[
            _finding("area", "ev_area"),
            _finding("02010101", "ev_cell"),
        ],
    )

    assert "Area summary [ev_area]." in rendered
    assert "Area-only reason." not in rendered
    assert "Dataset reason [ds_population]." in rendered
    assert "Cell reason [ev_cell]." in rendered


def test_t1_item_accepts_matching_hypothesis_address_finding():
    result = {
        "space": {"cell_id": "02010101"},
        "candidates": {"hypotheses": [{"id": "h_1", "name": "Coffee operator"}]},
    }
    report = Report(
        cells=[
            CellSection(
                cell_id="h_1",
                reasons=[Sentence(text="Address finding.", evidence_ids=["ev_address"])],
            )
        ]
    )

    rendered = render(
        report,
        result,
        {"ev_address"},
        "Test report",
        findings=[_finding("address", "ev_address", hypothesis_id="h_1")],
    )

    assert "Address finding [ev_address]." in rendered
