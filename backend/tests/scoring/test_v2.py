import json
import math
from pathlib import Path

import pytest

from app.contracts.cells import CellsDocument
from app.scoring.future import future_score, signed_bonus
from app.scoring.kita import capacity_percentile, kita_gap
from app.scoring.normalize import min_max
from app.scoring.ranking import weighted_score
from app.scoring.v2 import (
    CellInput,
    FutureModifier,
    ScoredCell,
    bonus_size,
    load_template,
    percentile_rank,
    rank,
    score_area,
    to_cells_document,
    top3,
)

GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text(encoding="utf-8"))
TOL = GOLDEN["conventions"]["tolerance"]


def _golden_cells(case: dict) -> list[ScoredCell]:
    weights = {layer["id"]: layer["weight"] for layer in case["layers"]}
    template = load_template(case["template"]) if "template" in case else None
    cells = []
    for raw in case["cells"]:
        present = weighted_score(raw["values"], weights)
        bonuses = []
        for modifier in raw.get("modifiers", []):
            size, magnitude = bonus_size(modifier["unit"], modifier["amount"])
            if template is not None:
                assert template.future_effect[modifier["kind"]] == modifier["effect"]
            bonus = signed_bonus(modifier["effect"], magnitude)
            assert size == modifier["size"]
            assert bonus == pytest.approx(modifier["bonus"], abs=TOL)
            bonuses.append(modifier["bonus"])
        cells.append(
            ScoredCell(
                cell_id=raw["id"],
                name=raw["id"],
                values=raw["values"],
                raw_values={},
                score_present=present,
                score_berlin=None,
                score_future=future_score(present, bonuses, case["bonus_cap"]),
                incomplete=raw.get("incomplete", False),
                demoted_by=list(raw.get("demoted_by", [])),
            )
        )
    return cells


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda case: case["id"])
def test_golden_vectors(case):
    cells = _golden_cells(case)
    expected = case["expected"]
    if "present_order" in expected:
        present = rank(cells, "present", tie_band=case["tie_band"])
        assert [cell.cell_id for cell in present] == expected["present_order"]
        assert {cell.cell_id: cell.tier for cell in cells} == expected["present_tiers"]
    ranked = rank(cells, case["mode"], tie_band=case["tie_band"])
    assert [cell.cell_id for cell in ranked] == expected["order"]
    assert [cell.cell_id for cell in top3(ranked)] == expected["top3"]
    for cell in cells:
        want = expected["cells"][cell.cell_id]
        assert cell.score_present == pytest.approx(want["score_present"], abs=TOL)
        assert cell.score_future == pytest.approx(want["score_future"], abs=TOL)
        assert cell.tier == want["tier"], cell.cell_id


def test_rank_high_risk_sets_demoted_by_and_keeps_scores():
    cells = _golden_cells(next(c for c in GOLDEN["cases"] if c["id"] == "demotion_last"))
    before = {cell.cell_id: cell.score_present for cell in cells}
    ranked = rank(cells, high_risk={"A": ["f_1"]})
    assert [cell.cell_id for cell in ranked] == ["B", "A", "C"]
    assert ranked[1].demoted_by == ["f_1"]
    assert {cell.cell_id: cell.score_present for cell in cells} == before


# --- engine level (raw values → percentiles → scores) -------------------------------------


def _coffee_cell(cell_id: str, residents: float = 10_000, **raw: float | None) -> CellInput:
    base: dict[str, float | None] = {
        "residents_block_density": 10_000.0,
        "young_adult_share": 0.4,
        "footfall_proxy": 20.0,
        "transit": 500.0,
        "centre_proximity": 800.0,
        "cafe_saturation": 5.0,
    }
    base.update(raw)
    return CellInput(cell_id=cell_id, name=f"PLR {cell_id}", residents=residents, raw=base)


def _coffee_area() -> list[CellInput]:
    return [
        _coffee_cell("01", residents_block_density=20_000, cafe_saturation=2, transit=2000),
        _coffee_cell("02", residents_block_density=15_000, cafe_saturation=4, footfall_proxy=40),
        _coffee_cell(
            "03", residents_block_density=12_000, cafe_saturation=6, young_adult_share=0.5
        ),
        _coffee_cell("04", residents_block_density=9_000, cafe_saturation=8, centre_proximity=200),
        _coffee_cell("05", residents_block_density=7_000, cafe_saturation=10, transit=None),
        _coffee_cell("06", residents_block_density=5_000, cafe_saturation=12),
    ]


def test_coffee_v2_template_matches_spec():
    template = load_template("coffee_v2")
    layers = {layer.id: layer for layer in template.layers}
    assert {k: (v.direction, v.weight, v.transform) for k, v in layers.items()} == {
        "residents_block_density": ("+", 0.25, "identity"),
        "young_adult_share": ("+", 0.10, "identity"),
        "footfall_proxy": ("+", 0.15, "log1p"),
        "transit": ("+", 0.15, "log1p"),
        "centre_proximity": ("-", 0.10, "identity"),
        "cafe_saturation": ("-", 0.25, "identity"),
    }
    assert sum(layer.weight for layer in template.layers) == pytest.approx(1.0)
    assert {k for k, v in layers.items() if v.required} == {
        "residents_block_density",
        "cafe_saturation",
    }
    assert template.min_population == 2000
    assert template.tie_band == 0.02
    assert "cafe_crosscheck" not in layers
    assert [check["id"] for check in template.crosschecks] == ["cafe_crosscheck"]
    assert template.future_effect["housing_growth"] == "+"
    assert template.future_effect["cafe_opening"] == "-"


def test_kita_gap_v1_template_matches_spec():
    template = load_template("kita_gap_v1")
    assert [(layer.id, layer.direction, layer.required) for layer in template.layers] == [
        ("kita_gap", "+", True)
    ]
    assert template.future_effect == {
        "kita_opening": "-",
        "bplan_kita_site": "-",
        "kita_closure": "+",
        "children_forecast_increase": "+",
    }
    assert template.future_units["children_forecast_increase"] == "children_0_6"
    assert template.params["coverage_target"] == 0.9
    assert template.params["demand"]["id"] == "children_0_6"
    assert template.params["supply"]["id"] == "permitted_places_known_sum"


def test_load_template_rejects_unknown_and_paths():
    with pytest.raises(FileNotFoundError):
        load_template("nope_v9")
    with pytest.raises(ValueError):
        load_template("../datasets/osm_pois")


def test_percentile_rank_mid_rank_and_ties():
    assert percentile_rank(3, [1, 2, 3, 4, 5]) == pytest.approx(0.5)
    assert percentile_rank(1, [1, 2, 3, 4, 5]) == pytest.approx(0.1)
    assert percentile_rank(2, [1, 2, 2, 4]) == pytest.approx(0.5)
    assert percentile_rank(7, [7, 7, 7]) == 0.5
    assert percentile_rank(10, [1, 2]) == 1.0
    assert min_max(5, [5, 5]) == 0.5


def test_bonus_size_thresholds():
    assert bonus_size("dwellings", 199) == ("small", 0.03)
    assert bonus_size("dwellings", 200) == ("medium", 0.06)
    assert bonus_size("dwellings", 999) == ("medium", 0.06)
    assert bonus_size("dwellings", 1000) == ("large", 0.10)
    assert bonus_size("kita_places", 49) == ("small", 0.03)
    assert bonus_size("kita_places", 100) == ("medium", 0.06)
    assert bonus_size("kita_places", 150) == ("medium", 0.06)
    assert bonus_size("kita_places", 151) == ("large", 0.10)
    assert bonus_size("children_0_6", 199) == ("medium", 0.06)
    assert bonus_size("children_0_6", 200) == ("large", 0.10)
    assert bonus_size("dwellings", None) == ("small", 0.03)  # no number → small
    assert bonus_size("dwellings", math.nan) == ("small", 0.03)


def test_score_present_is_weighted_mean_of_percentiles_without_rescale():
    template = load_template("coffee_v2")
    result = score_area(_coffee_area(), template)
    weights = {layer.id: layer.weight for layer in template.layers}
    by_id = {cell.cell_id: cell for cell in result.cells}
    for cell in result.cells:
        assert cell.score_present == pytest.approx(weighted_score(cell.values, weights))
    # density +: highest raw → highest percentile; saturation −: lowest raw → best
    assert by_id["01"].values["residents_block_density"] == pytest.approx(11 / 12)
    assert by_id["01"].values["cafe_saturation"] == pytest.approx(11 / 12)
    assert by_id["06"].values["cafe_saturation"] == pytest.approx(1 / 12)
    # ties averaged: five equal shares share the mid-rank 2.5 / 6
    tied = [cell.values["young_adult_share"] for cell in result.cells if cell.cell_id != "03"]
    assert tied == [pytest.approx(2.5 / 6)] * 5
    assert max(c.score_present for c in result.cells) < 1.0
    assert min(c.score_present for c in result.cells) > 0.0
    assert [cell.cell_id for cell in result.cells][0] == "01"
    assert result.layer_reference["transit"]["normalization"] == "percentile"


def test_missing_optional_layer_renormalizes_and_lowers_confidence():
    template = load_template("coffee_v2")
    result = score_area(_coffee_area(), template)
    cell = next(cell for cell in result.cells if cell.cell_id == "05")
    assert cell.values["transit"] is None
    weights = {layer.id: layer.weight for layer in template.layers if layer.id != "transit"}
    expected = sum(weights[k] * cell.values[k] for k in weights) / sum(weights.values())
    assert cell.score_present == pytest.approx(expected)
    assert "optional layer missing: transit" in cell.confidence_reasons


def test_minimum_population_and_required_layer_exclusion():
    template = load_template("coffee_v2")
    cells = _coffee_area() + [
        _coffee_cell("90", residents=1_999, residents_block_density=99_000, cafe_saturation=0),
        _coffee_cell("91", cafe_saturation=None),
        CellInput("92", "No residents", None, {"residents_block_density": 1.0}),
    ]
    result = score_area(cells, template)
    assert {cell.cell_id for cell in result.cells} == {"01", "02", "03", "04", "05", "06"}
    reasons = {item["cell_id"]: item["reason"] for item in result.excluded}
    assert reasons["90"].startswith("residents below 2,000")
    assert reasons["91"] == "required layer missing: cafe_saturation"
    assert reasons["92"] == "residents unknown"
    # excluded cells never enter the normalization set
    alone = score_area(_coffee_area(), template)
    assert [c.score_present for c in result.cells] == [c.score_present for c in alone.cells]


def test_osm_and_ihk_never_summed_crosscheck_only_affects_confidence():
    template = load_template("coffee_v2")
    plain = score_area(_coffee_area(), template)
    noisy_cells = _coffee_area()
    for cell in noisy_cells:
        cell.raw["cafe_crosscheck"] = 99.0
        cell.raw["ihk_cafes"] = 50.0
    crosscheck = {"01": (3, 10), "02": (4, 6), "03": (1, 9)}
    checked = score_area(noisy_cells, template, crosscheck=crosscheck)
    assert [(c.cell_id, c.score_present) for c in checked.cells] == [
        (c.cell_id, c.score_present) for c in plain.cells
    ]
    assert all("cafe_crosscheck" not in c.values for c in checked.cells)
    by_id = {cell.cell_id: cell for cell in checked.cells}
    assert "OSM/IHK café counts deviate (OSM 3, IHK 10)" in by_id["01"].confidence_reasons
    assert by_id["01"].unknowns and "IHK register 10" in by_id["01"].unknowns[0]["text"]
    assert not any("OSM/IHK" in r for r in by_id["02"].confidence_reasons)  # ratio 1.5
    assert not any("OSM/IHK" in r for r in by_id["03"].confidence_reasons)  # OSM < 3


def test_confidence_steps_down_per_reason_with_floor_low():
    template = load_template("coffee_v2")
    cells = _coffee_area()
    cells[4].residents = 2_500
    cells[4].fallback_layers = ["residents_block_density"]
    result = score_area(
        cells,
        template,
        extra_confidence_reasons={"06": ["verified medium risk"], "02": ["custom reason"]},
        risks={"06": [{"severity": "medium", "text": "rent jump", "evidence_id": "ev_r"}]},
    )
    by_id = {cell.cell_id: cell for cell in result.cells}
    assert by_id["05"].confidence == "low"
    assert by_id["05"].confidence_reasons[:3] == [
        "optional layer missing: transit",
        "fallback layer used: residents_block_density",
        "residents below 3,000",
    ]
    assert by_id["06"].confidence_reasons.count("verified medium risk") == 1
    assert by_id["06"].confidence == "medium"
    assert by_id["02"].confidence_reasons == ["custom reason"]
    assert by_id["02"].confidence == "medium"


def test_top3_share_is_deterministic_and_bounded():
    template = load_template("coffee_v2")
    first = score_area(_coffee_area(), template)
    second = score_area(_coffee_area(), template)
    assert [c.top3_share for c in first.cells] == [c.top3_share for c in second.cells]
    assert all(0.0 <= c.top3_share <= 1.0 for c in first.cells)
    assert sum(c.top3_share for c in first.cells) == pytest.approx(3.0)
    assert first.cells[0].top3_share == 1.0


def test_future_modifiers_via_score_area_kita_opening_and_cap():
    template = load_template("kita_gap_v1")
    cells = [
        CellInput(f"K{i}", f"Kita {i}", 5_000, {"kita_gap": gap})
        for i, gap in enumerate([0.30, 0.25, 0.20, 0.15, 0.10])
    ]
    modifiers = [
        FutureModifier("K0", "kita_opening", "kita_places", 100, "ev_k", "New Kita", "100 Plätze"),
        FutureModifier("K4", "kita_closure", "kita_places", 200, "ev_c1", "Closure", "200 Plätze"),
        FutureModifier("K4", "kita_closure", "kita_places", 160, "ev_c2", "Closure", "160 Plätze"),
        FutureModifier(
            "K3",
            "children_forecast_increase",
            "children_0_6",
            80,
            "ev_f",
            "Forecast",
            "+80",
            completion_year=2035,
        ),
        FutureModifier("K2", "unknown_kind", "kita_places", 10, "ev_x", "?", "?"),
    ]
    result = score_area(cells, template, modifiers=modifiers)
    by_id = {cell.cell_id: cell for cell in result.cells}
    opening = by_id["K0"].modifiers[0]
    assert (opening["size"], opening["bonus"]) == ("medium", -0.06)
    assert opening["magnitude"] == {"value": 100, "unit": "kita_places", "size": "medium"}
    assert by_id["K0"].score_future == pytest.approx(by_id["K0"].score_present - 0.06)
    assert by_id["K4"].future_bonus == pytest.approx(0.15)
    assert by_id["K4"].score_future == pytest.approx(by_id["K4"].score_present + 0.15)
    assert by_id["K3"].modifiers[0]["counted"] is False
    assert by_id["K3"].score_future == by_id["K3"].score_present
    assert by_id["K2"].modifiers == []
    assert any("unknown_kind" in note for note in result.notes)


def test_incomplete_high_scorer_stays_out_of_top3_after_rerank():
    template = load_template("kita_gap_v1")
    gaps = kita_gap(400, 100, missing_facilities=2, imputed_capacity=60)
    assert gaps.incomplete and gaps.gap_low < gaps.gap_high
    cells = [
        CellInput(
            "I", "Imputed", 5_000, {"kita_gap": 0.9}, incomplete=True, gap_low=0.9, gap_high=0.95
        ),
        *[
            CellInput(f"C{i}", f"C{i}", 5_000, {"kita_gap": gap})
            for i, gap in enumerate([0.5, 0.4, 0.3, 0.2])
        ],
    ]
    result = score_area(cells, template)
    ids = [cell.cell_id for cell in result.cells]
    assert ids[-1] == "I"
    assert "I" not in [cell.cell_id for cell in top3(result.cells)]
    incomplete = result.cells[-1]
    assert incomplete.score_present == max(c.score_present for c in result.cells)
    assert incomplete.top3_share == 0.0
    assert incomplete.confidence != "high"
    assert "Kita capacity partly imputed (gap range)" in incomplete.confidence_reasons
    assert incomplete.unknowns[0]["suggested_validation"] == "Confirm capacity with the Jugendamt."
    for mode in ("present", "future"):
        reranked = rank(result.cells, mode, high_risk={"C0": ["f_hi"], "C3": ["f_hi2"]})
        assert "I" not in [cell.cell_id for cell in reranked[:3]]
        assert reranked[-1].cell_id == "I"


def test_kita_gap_helper():
    full = kita_gap(200, 150)
    assert full.gap_low == full.gap_high == pytest.approx((200 * 0.9 - 150) / 200)
    assert not full.incomplete and not full.is_range
    partial = kita_gap(200, 150, missing_facilities=1, imputed_capacity=30)
    assert partial.gap_low == pytest.approx((180 - 180) / 200)
    assert not partial.incomplete  # 30 / 180 ≤ 20 %
    assert kita_gap(0, 10).gap_low is None
    assert capacity_percentile([10, 20, 30, 40, 50]) == 40


def test_score_berlin_against_reference():
    template = load_template("coffee_v2")
    area = _coffee_area()
    reference = area + [
        _coffee_cell(f"R{i}", residents_block_density=1_000 + i * 100, cafe_saturation=20 + i)
        for i in range(10)
    ]
    reference.append(_coffee_cell("Rx", residents=100, residents_block_density=1e9))
    result = score_area(area, template, berlin_reference=reference)
    assert all(cell.score_berlin is not None for cell in result.cells)
    assert all(cell.score_berlin >= cell.score_present - 1e-9 for cell in result.cells)
    assert result.layer_reference["cafe_saturation"]["berlin_cells"] == 16
    assert score_area(area, template).cells[0].score_berlin is None


def test_small_area_uses_min_max_with_note():
    template = load_template("coffee_v2")
    result = score_area(_coffee_area()[:3], template)
    by_id = {cell.cell_id: cell for cell in result.cells}
    assert by_id["01"].values["residents_block_density"] == 1.0
    assert by_id["03"].values["residents_block_density"] == 0.0
    assert result.layer_reference["residents_block_density"]["normalization"] == "min_max"
    assert any("min–max" in note for note in result.notes)


def test_weight_override_untick_layer():
    template = load_template("coffee_v2")
    result = score_area(_coffee_area(), template, weights={"transit": 0})
    cell = next(cell for cell in result.cells if cell.cell_id == "05")
    assert not any("transit" in reason for reason in cell.confidence_reasons)
    with pytest.raises(ValueError):
        score_area(_coffee_area(), template, weights={"rent": 0.2})


def test_to_cells_document_contract_and_extra_fields():
    template = load_template("coffee_v2")
    cells = _coffee_area()
    cells[0].layer_evidence = {"cafe_saturation": "ds_osm_1"}
    cells.append(_coffee_cell("99", residents=500))
    result = score_area(
        cells,
        template,
        modifiers=[
            FutureModifier("02", "housing_growth", "dwellings", 450, "ev_b", "B-Plan", "450 WE"),
            FutureModifier("03", "cafe_opening", None, 2, "ev_c", "Two cafés", "zwei Cafés"),
        ],
        risks={
            "01": [
                {"severity": "high", "text": "closure", "evidence_id": "ev_r", "finding_id": "f_r"}
            ]
        },
    )
    doc = to_cells_document(result, {"area_id": "ortsteil:0202", "name": "Kreuzberg"}, template)
    assert isinstance(doc, CellsDocument)
    payload = json.loads(doc.model_dump_json())
    CellsDocument.model_validate(payload)
    layers = {layer["id"]: layer for layer in payload["layers"]}
    assert layers["cafe_saturation"]["evidence_id"] == "ds_osm_1"
    assert layers["transit"]["evidence_id"] == "ev:layer:transit"
    assert layers["transit"]["transform"] == "log1p"
    assert layers["transit"]["reference"]["area_cells"] == 5
    assert payload["excluded_cells"] == [
        {"id": "99", "name": "PLR 99", "reason": "residents below 2,000 (500)"}
    ]
    assert payload["values_directional"] is True
    first = payload["cells"][0]
    assert first["id"] != "01"  # demoted by the high risk
    demoted = next(cell for cell in payload["cells"] if cell["id"] == "01")
    assert demoted["demoted_by"] == ["f_r"]
    assert demoted["risks"][0]["severity"] == "high"
    for key in (
        "score_berlin",
        "confidence",
        "confidence_reasons",
        "tier",
        "top3_share",
        "incomplete",
        "gap_low",
        "gap_high",
    ):
        assert key in first
    housing = next(cell for cell in payload["cells"] if cell["id"] == "02")
    assert housing["modifiers"][0]["bonus"] == 0.06
    assert housing["modifiers"][0]["magnitude"]["value"] == 450
    cafe = next(cell for cell in payload["cells"] if cell["id"] == "03")
    assert cafe["modifiers"][0]["bonus"] == -0.03
    assert cafe["modifiers"][0]["magnitude"] == {"value": 2, "size": "small"}
    assert payload["area"]["plr_count"] == 7
    assert payload["area"]["cell_count"] == 6
