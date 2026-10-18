"""Frontend/backend scoring parity: run frontend/app/scoring.js on the golden vectors in Node.

Skipped when no `node` binary is found (PATH, $NODE or ~/.nvm)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCORING_JS = ROOT / "frontend" / "app" / "scoring.js"
GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text(encoding="utf-8"))
TOL = GOLDEN["conventions"]["tolerance"]

HARNESS = r"""
const api = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const c of cases) {
  const doc = {
    values_directional: true, tie_band: c.tie_band, bonus_cap: c.bonus_cap,
    layers: c.layers.map(l => ({ id: l.id, direction: l.direction })),
    cells: c.cells.map(x => ({ ...x, modifiers: x.modifiers || [] })),
  };
  const on = Object.fromEntries(c.layers.map(l => [l.id, true]));
  const w = Object.fromEntries(c.layers.map(l => [l.id, l.weight]));
  const run = mode => api.scoreCells(doc, on, w, mode).map(r => ({
    id: r.cell.id, present: r.score_present, future: r.score_future, tier: r.tier,
    incomplete: !!r.cell.incomplete,
  }));
  out[c.id] = { mode: run(c.mode), present: run('present') };
}
process.stdout.write(JSON.stringify(out));
"""


def _node() -> str | None:
    found = os.environ.get("NODE") or shutil.which("node")
    if found:
        return found
    candidates = sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/node"))
    return str(candidates[-1]) if candidates else None


NODE = _node()


@pytest.fixture(scope="module")
def js_results() -> dict:
    if NODE is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [NODE, "-e", HARNESS, str(SCORING_JS)],
        input=json.dumps(GOLDEN["cases"]),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda case: case["id"])
def test_scoring_js_matches_golden_vectors(case: dict, js_results: dict) -> None:
    expected = case["expected"]
    result = js_results[case["id"]]
    ranked = result["mode"]
    assert [row["id"] for row in ranked] == expected["order"]
    assert [row["id"] for row in ranked if not row["incomplete"]][:3] == expected["top3"]
    for row in ranked:
        want = expected["cells"][row["id"]]
        assert row["present"] == pytest.approx(want["score_present"], abs=TOL)
        assert row["future"] == pytest.approx(want["score_future"], abs=TOL)
        assert row["tier"] == want["tier"], row["id"]
    if "present_order" in expected:
        present = result["present"]
        assert [row["id"] for row in present] == expected["present_order"]
        assert {row["id"]: row["tier"] for row in present} == expected["present_tiers"]
