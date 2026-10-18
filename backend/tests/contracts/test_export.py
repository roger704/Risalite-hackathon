"""Research run export contracts and safety behavior."""

import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain import ConnectorError
from app.export import (
    Mix,
    RunBundle,
    load_run,
    rank_cells,
    render_html,
    render_markdown,
    sources_bibtex,
    sources_csv,
)
from app.export.citations import replace_citations
from app.export.report import _trace_summary

RUNS = Path(__file__).resolve().parents[1] / "fixtures" / "runs"


def coffee():
    return load_run(RUNS / "demo-coffee")


def test_default_layers_off_future_bonus_and_high_risk_ranking():
    bundle = coffee()
    cells = bundle.cells
    assert cells is not None
    assert rank_cells(cells)[0].cell["id"] == "B"
    assert (
        rank_cells(cells, Mix(layers_on=frozenset({"demand_density", "competition", "rent"})))[
            0
        ].cell["id"]
        == "A"
    )
    assert rank_cells(cells, Mix(mode="future"))[0].cell["id"] == "A"

    high_risk = {
        **cells,
        "cells": [
            {**cell, "risks": [{"severity": "high"}]} if cell["id"] == "B" else cell
            for cell in cells["cells"]
        ],
    }
    ranked = rank_cells(high_risk)
    assert ranked[0].cell["id"] == "A"
    assert next(item.score for item in ranked if item.cell["id"] == "B") == pytest.approx(0.92)


@pytest.mark.parametrize(
    "mix,code",
    [
        (Mix(layers_on=frozenset({"missing"})), "UNKNOWN_LAYER"),
        (Mix(layers_on=frozenset()), "INVALID_REQUEST"),
    ],
)
def test_invalid_mix_layers_use_domain_errors(mix, code):
    with pytest.raises(ConnectorError) as error:
        rank_cells(coffee().cells, mix)
    assert error.value.code == code


def test_citations_are_numbered_and_unsupported_or_unknown_ids_are_removed():
    bundle = coffee()
    assert [entry["n"] for entry in bundle.evidence] == [1, 2, None, 3]
    rewritten = replace_citations(
        "Kept [e1], partial [e2], unsupported [e3], missing [e999].", bundle.evidence
    )
    assert rewritten == "Kept [1], partial [2], unsupported , missing ."


def test_citations_preserve_bracket_text_and_markdown_links():
    evidence = coffee().evidence
    assert replace_citations("see [e1] and [sic]", evidence) == "see [1] and [sic]"
    assert replace_citations("[docs](https://x.test)", evidence) == "[docs](https://x.test)"
    assert replace_citations("[e99]", evidence) == ""


def test_markdown_sections_top_three_sources_and_default_mix_label():
    report = render_markdown(coffee())
    sections = [
        "## 1. Approved plan",
        "## 2. Summary (default mix)",
        "## 3. Findings",
        "## 4. Layers & weights",
        "## 5. Sources",
        "## 6. Trace summary",
    ]
    assert [report.index(section) for section in sections] == sorted(
        report.index(section) for section in sections
    )
    assert "Mix: default" in report
    assert "Summary (default mix)" in report
    findings = report.split("## 3. Findings", 1)[1].split("## 4. Layers", 1)[0]
    assert "1. Bergmannkiez" in findings
    assert "2. Admiralskiez" in findings
    assert "3. Donaustraße" in findings
    assert "Could not verify" in report
    assert "Rejected unsupported late-night assertion." in report
    assert "[e3]" not in report


def test_markdown_table_escapes_pipes_and_newlines():
    bundle = coffee()
    assert bundle.cells is not None
    bundle.cells["cells"][0]["name"] = "Café | Hof\nWest"
    report = render_markdown(bundle)
    assert "| A | Café \\| Hof<br>West |" in report


def test_html_escapes_data_links_only_http_and_renders_optional_svg():
    bundle = coffee()
    html = render_html(bundle)
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert 'href="javascript:alert(1)"' not in html
    assert "javascript:alert(1)" in html
    assert "<svg " in html
    assert "@media print" in html
    assert "@page{size:A4;margin:16mm}" in html
    assert "window.print()" in html

    without_geojson = replace(bundle, geojson=None)
    assert "<svg " not in render_html(without_geojson)


def test_html_malformed_http_url_is_escaped_text_not_a_link():
    bundle = coffee()
    bundle.evidence[0]["url"] = "https://[bad"

    html = render_html(bundle)

    assert "https://[bad" in html
    assert 'href="https://[bad"' not in html


def test_sources_csv_has_exact_header_formula_guard_and_unsupported_blank_number():
    bundle = coffee()
    output = sources_csv(bundle)
    rows = list(csv.DictReader(io.StringIO(output)))
    assert output.splitlines()[0] == (
        "n,evidence_id,title,url,method,tier,confidence,status,quote,retrieved_at,published_at"
    )
    assert len(rows) == 4
    assert rows[1]["quote"].startswith("'=HYPERLINK(")
    assert rows[1]["title"] == "Käse & Gemüse"
    assert rows[2]["n"] == ""
    assert rows[0]["title"].startswith("Café &")


def test_sources_csv_guards_formulas_after_leading_whitespace():
    bundle = coffee()
    bundle.evidence[0]["title"] = "  =1+1"
    bundle.evidence[0]["quote"] = "\n@x"

    rows = list(csv.DictReader(io.StringIO(sources_csv(bundle))))

    assert rows[0]["title"] == "'  =1+1"
    assert rows[0]["quote"] == "'\n@x"


def test_markdown_sources_autolink_http_and_escape_other_schemes():
    bundle = RunBundle(
        run={"run_id": "source-url-test", "shape": "point", "question": "Fixture-free URL test"},
        cells=None,
        geojson=None,
        candidates={"hypotheses": []},
        evidence=[
            {
                "n": 1,
                "evidence_id": "e1",
                "title": "HTTP query",
                "url": "https://example.test/wfs?CQL_FILTER=nace LIKE '56%25'<tag>",
                "method": "wfs",
                "tier": "1",
                "confidence": 1,
                "status": "verified",
                "quote": "Example quote.",
            },
            {
                "n": None,
                "evidence_id": "e2",
                "title": "Unsupported URL",
                "url": "javascript:alert(1)",
                "method": "web",
                "tier": "3",
                "confidence": 0,
                "status": "unsupported",
                "quote": "Unverified.",
            },
        ],
        trace=[],
    )
    report = render_markdown(bundle)
    assert "<https://example.test/wfs?CQL_FILTER=nace%20LIKE%20'56%25'%3Ctag%3E>" in report
    assert "javascript\\:alert\\(1\\)" in report
    assert "<javascript:" not in report


def test_bibtex_omits_unsupported_entries_escapes_fields_and_sanitizes_keys():
    bundle = coffee()
    bundle.run["run_id"] = "demo/coffée"
    bibtex = sources_bibtex(bundle)
    assert bibtex.count("@misc{") == 3
    assert "@misc{risalite-demo-coff-e-e1," in bibtex
    assert r"Café \& Brötchen\_" in bibtex
    assert "%7B" in bibtex and "%7D" in bibtex and "%20" in bibtex
    assert "verification" not in bibtex
    assert "urldate = {2026-10-18}" in bibtex
    assert "year = {2026}" in bibtex


def test_point_markdown_sorts_final_rank_and_shows_for_against_unknowns():
    bundle = load_run(RUNS / "demo-point")
    report = render_markdown(bundle)
    html = render_html(bundle)
    assert (
        report.index("1. First choice")
        < report.index("2. Second choice")
        < report.index("3. Third choice")
    )
    assert "**FOR**" in report
    assert "**AGAINST**" in report
    assert "**UNKNOWNS**" in report
    assert "Suggested validation: Visit at closing time." in report
    assert "- Space Address: Oranienstraße, Berlin" in report
    assert "- Space PLR ID: 02000402" in report
    assert "- Space PLR name: Kreuzberg Nord" in report
    assert "- Space Size: 120 m²" in report
    assert "- Operator Concept: independent café" in report
    assert "<dt>Space Address</dt><dd>Oranienstraße, Berlin</dd>" in html
    assert "<dt>Operator Concept</dt><dd>independent café</dd>" in html


def test_trace_summary_counts_measured_rejections_but_not_bare_notifications():
    bundle = RunBundle(
        run={"models": {}},
        cells=None,
        geojson=None,
        candidates={"hypotheses": []},
        evidence=[],
        trace=[
            {"role": "analyst", "model": "model-a", "tokens": 12, "seconds": 1.5},
            {"role": "analyst", "type": "claim_reject", "text": "Bare notification."},
            {
                "role": "fact_checker",
                "type": "claim_reject",
                "model": "model-b",
                "tokens": 40,
                "seconds": 0.5,
                "text": "Rejected claim.",
            },
        ],
    )

    roles, rejected = _trace_summary(bundle)

    assert sorted(roles, key=lambda r: r["role"]) == [
        {"role": "analyst", "model": "model-a", "calls": 1, "tokens": 12, "seconds": 1.5},
        {"role": "fact_checker", "model": "model-b", "calls": 1, "tokens": 40, "seconds": 0.5},
    ]
    assert len(rejected) == 2


def test_markdown_point_meta_keeps_multiline_values_inside_bullet():
    bundle = load_run(RUNS / "demo-point")
    bundle.candidates["operator"]["concept"] = "Coffee shop\n\n## 5. Sources"

    report = render_markdown(bundle)

    assert "- Operator Concept: Coffee shop ## 5. Sources" in report
    assert report.count("\n## 5. Sources") == 1
