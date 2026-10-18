import json
from datetime import datetime, timezone
from pathlib import Path

from app.contracts import AreaInfo, Cell, CellsDocument, Evidence, ExcludedCell, Layer
from app.scoring.scoring import score_cells


def build_cells(
    area,
    layers,
    weights,
    modifiers,
    risks,
    unknowns,
    *,
    config,
    names,
    template,
):
    raw_values = {
        plr_id: {layer.layer_id: layer.values.get(plr_id) for layer in layers}
        for plr_id in area.plr_ids
    }
    cell_modifiers = {}
    for modifier in modifiers or []:
        cell_modifiers.setdefault(modifier.get("cell_id"), []).append(
            {key: value for key, value in modifier.items() if key != "cell_id"}
        )
    cfg_layers = config["layers"]
    scored, excluded, methods = score_cells(
        raw_values,
        cfg_layers,
        weights,
        cell_modifiers,
        config.get("future_modifier_bonus", 0.1),
        names={plr_id: names.get(plr_id, {}).get("name", plr_id) for plr_id in area.plr_ids},
        skew_threshold=config.get("skew_threshold", 1.0),
    )
    layer_docs = []
    for item in cfg_layers:
        found = next((layer for layer in layers if layer.layer_id == item["id"]), None)
        if not found:
            continue
        layer_docs.append(
            Layer(
                id=item["id"],
                label=item["label"],
                direction=item["direction"],
                weight_default=(
                    item["weight_default"] if weights is None else weights.get(item["id"], 0.0)
                ),
                source=found.source_url,
                evidence_id=found.evidence.id,
                why=item.get("why", ""),
                units=found.units,
                method=methods.get(item["id"]),
                required=item.get("required"),
            )
        )
    cell_docs = []
    for plr_id, item in scored.items():
        cell_docs.append(
            Cell(
                id=plr_id,
                name=names.get(plr_id, {}).get("name", plr_id),
                geometry_ref=f"cells.geojson#{plr_id}",
                values=item["values"],
                modifiers=item["modifiers"],
                score_present=item["score_present"],
                score_future=item["score_future"],
                raw_values=item["raw_values"],
                evidence_ids={layer.layer_id: layer.evidence.id for layer in layers},
                risks=risks.get(plr_id, []),
                unknowns=unknowns.get(plr_id, []),
                missing_layers=item["missing_layers"],
            )
        )
    cell_docs.sort(key=lambda cell: (-cell.score_present, cell.id))
    excluded_docs = [ExcludedCell(**item) for item in excluded]
    evidence = [layer.evidence for layer in layers]
    if config.get("_geometry_source_url"):
        evidence.append(
            Evidence(
                id=f"s2-geometry-{config.get('_geometry_snapshot_id', 'unknown')}",
                method="snapshot",
                title="Berlin LOR PLR geometry and names",
                url=config["_geometry_source_url"],
                quote=f"{len(area.plr_ids)} PLR geometries and official names selected for {area.name}",
                tier="official",
                confidence="high",
                source_snapshot_id=config.get("_geometry_snapshot_id"),
                details={
                    "geometry_snapshot_id": config.get("_geometry_geometry_snapshot_id"),
                    "names_snapshot_id": config.get("_geometry_snapshot_id"),
                },
            )
        )
    doc = CellsDocument(
        area=AreaInfo(
            name=area.name,
            level=area.level,
            cell_count=len(cell_docs),
            matched_on=area.matched_on,
            plr_ids=area.plr_ids,
        ),
        layers=layer_docs,
        cells=cell_docs,
        excluded_cells=excluded_docs,
        template=template,
        scoring_version=config.get("version"),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return doc, evidence


def write_run(out_dir, doc, geojson, evidence):
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "cells.json").write_text(
        json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (target / "cells.geojson").write_text(
        json.dumps(geojson, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (target / "evidence.jsonl").write_text(
        "".join(item.to_jsonl_line() + "\n" for item in evidence), encoding="utf-8"
    )
