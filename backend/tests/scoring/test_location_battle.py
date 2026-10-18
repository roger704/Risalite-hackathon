"""Exercise the actual browser comparison with the reported live scoring inputs."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INPUT = Path(__file__).with_name("battle_regression.json")
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const scoring = require(process.argv[1]);
const source = fs.readFileSync(process.argv[2], 'utf8');
const doc = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = source.split('\n').find(line => line.startsWith('const esc =')) + '\n' +
  source.slice(source.indexOf('function renderLocationBattle('), source.indexOf('function renderArea('));
const context = vm.createContext({ SpotWiseScoring: scoring });
vm.runInContext(code, context);
const on = Object.fromEntries(doc.layers.map(l => [l.id, true]));
const weights = Object.fromEntries(doc.layers.map(l => [l.id, l.weight_default]));
function run(d, enabled = on, w = weights, mode = 'present') {
  const ranked = scoring.scoreCells(d, enabled, w, mode);
  return { ranked, html: context.renderLocationBattle(d, ranked, enabled, w, mode),
    rows: ranked.map(r => scoring.contributions(d, r.cell, enabled, w)) };
}
const clone = () => JSON.parse(JSON.stringify(doc));
const live = run(doc);
const legacyDoc = clone(); delete legacyDoc.values_directional;
for (const c of legacyDoc.cells) for (const l of legacyDoc.layers)
  if (l.direction === '-') c.values[l.id] = 1 - c.values[l.id];
const missingDoc = clone(); missingDoc.cells[0].values.transit = null;
const riskDoc = clone(); riskDoc.cells[0].demoted_by = ['f_risk'];
const incompleteDoc = clone(); incompleteDoc.cells[0].incomplete = true;
const futureDoc = clone(); futureDoc.cells[1].modifiers = [{bonus: .4}];
const unsafeDoc = clone(); unsafeDoc.cells[0].name = '<img src=x onerror=alert(1)>';
const previewCode = source.slice(source.indexOf('function previewValue('), source.indexOf('function previewLabel('));
context.R = {cells: doc}; context.previewLayer = 'cafe_saturation';
vm.runInContext(previewCode, context);
const preview = context.previewValue(live.ranked[0]);
process.stdout.write(JSON.stringify({live, legacy: run(legacyDoc), missing: run(missingDoc),
  reweighted: run(doc, {...on, cafe_saturation: false}), risk: run(riskDoc),
  incomplete: run(incompleteDoc), future: run(futureDoc, on, weights, 'future'),
  zero: run(doc, on, Object.fromEntries(doc.layers.map(l => [l.id, 0]))),
  unsafe: run(unsafeDoc), preview}));
"""


@pytest.fixture(scope="module")
def comparison():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the browser comparison check")
    result = subprocess.run(
        [
            node,
            "-e",
            HARNESS,
            str(ROOT / "frontend/app/scoring.js"),
            str(ROOT / "frontend/app/app.js"),
        ],
        input=INPUT.read_text(),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_live_comparison_uses_weighted_scores_without_double_inversion(comparison):
    result = comparison["live"]
    assert [r["cell"]["name"] for r in result["ranked"]] == ["Viktoriapark", "Graefekiez Nord"]
    assert result["ranked"][0]["score"] == pytest.approx(0.713158, abs=1e-6)
    assert result["ranked"][1]["score"] == pytest.approx(0.623684, abs=1e-6)
    left = {row["layer"]["id"]: row for row in result["rows"][0]}
    right = {row["layer"]["id"]: row for row in result["rows"][1]}
    assert left["cafe_saturation"]["value"] == 0.868421
    assert left["cafe_saturation"]["contribution"] == pytest.approx(0.21710525)
    assert right["cafe_saturation"]["contribution"] == pytest.approx(0.03289475)
    assert left["centre_proximity"]["contribution"] > right["centre_proximity"]["contribution"]
    assert (
        sum(a["contribution"] > b["contribution"] for a, b in zip(left.values(), right.values()))
        == 2
    )
    assert "Overall recommendation: Viktoriapark" in result["html"]
    assert "Low café competition (+0.184" in result["html"]
    assert "leads by 0.089" in result["html"]
    assert "25% of score" in result["html"]
    assert comparison["preview"] == 0.868421


@pytest.mark.parametrize("case", ["live", "legacy", "missing", "reweighted", "future"])
def test_visible_contributions_add_up_to_the_same_present_score(comparison, case):
    result = comparison[case]
    for entry, rows in zip(result["ranked"], result["rows"]):
        assert sum(row["contribution"] or 0 for row in rows) == pytest.approx(
            entry["score_present"]
        )
        assert sum(row["effectiveWeight"] for row in rows) == pytest.approx(1)
    if case == "legacy":
        assert result["html"] == comparison["live"]["html"]
    if case == "missing":
        assert "missing</span>" in result["html"]
        assert "% left /" in result["html"]
    if case == "reweighted":
        assert result["ranked"][0]["cell"]["name"] == "Graefekiez Nord"
    if case == "future":
        assert result["ranked"][0]["cell"]["name"] == "Graefekiez Nord"
        assert "Future adjustment" in result["html"]
        assert "0.150" in result["html"]


@pytest.mark.parametrize(
    "case,reason", [("risk", "risk-based rank demotion"), ("incomplete", "missing required data")]
)
def test_lower_score_recommendation_explains_actual_rank_override(comparison, case, reason):
    result = comparison[case]
    assert result["ranked"][0]["score"] < result["ranked"][1]["score"]
    assert reason in result["html"]
    if case == "risk":
        assert 'href="#finding-f_risk"' in result["html"]


def test_comparison_handles_no_weights_and_escapes_names(comparison):
    assert "No score is available" in comparison["zero"]["html"]
    assert "<b>—</b>" in comparison["zero"]["html"]
    assert "<img" not in comparison["unsafe"]["html"]
    assert "&lt;img" in comparison["unsafe"]["html"]
