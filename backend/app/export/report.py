"""Portable Markdown and print-ready HTML reports for research runs."""

import html
import math
import re
import string
from typing import Any
from urllib.parse import urlsplit

from shapely.affinity import scale
from shapely.geometry import shape

from app.export.citations import citation, replace_citations
from app.export.mix import Mix, RankedCell, rank_cells
from app.export.run import RunBundle

_COLOR_STOPS = (
    (0.0, (255, 247, 214)),
    (0.25, (180, 226, 193)),
    (0.5, (65, 174, 160)),
    (0.75, (18, 104, 121)),
    (1.0, (12, 52, 86)),
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _cell(value: Any) -> str:
    return (
        _text(value)
        .replace("|", r"\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _num(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _text(value)
    return f"{number:.{digits}f}" if math.isfinite(number) else ""


def _mix_line(bundle: RunBundle, mix: Mix | None) -> str:
    if mix is None or (mix.layers_on is None and mix.mode is None):
        return "Mix: default"
    layers = (bundle.cells or {}).get("layers", [])
    selected = (
        set(mix.layers_on) if mix.layers_on is not None else {layer["id"] for layer in layers}
    )
    labels = [layer.get("label", layer["id"]) for layer in layers if layer["id"] in selected]
    mode = mix.mode or (bundle.cells or {}).get("mode_default", "present")
    mode_text = "present + future" if mode == "future" else "present"
    return f"Mix: custom — layers: {', '.join(labels)}; mode: {mode_text}"


def _trace_summary(bundle: RunBundle) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    rejected = []
    for event in bundle.trace:
        role = _text(event.get("role") or "unknown")
        item = grouped.setdefault(
            role,
            {"role": role, "model": "", "calls": 0, "tokens": 0, "seconds": 0.0},
        )
        item["calls"] += 1
        item["tokens"] += _integer(event.get("tokens"))
        item["seconds"] += _number(event.get("seconds"))
        if event.get("model"):
            item["model"] = event["model"]
        if "reject" in _text(event.get("type")).lower():
            rejected.append(event)
    models = bundle.run.get("models", {})
    for role, item in grouped.items():
        if isinstance(models, dict) and models.get(role):
            item["model"] = models[role]
    return list(grouped.values()), rejected


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _citation_list(bundle: RunBundle, ids: list[str] | None) -> str:
    return " ".join(
        filter(None, (citation(bundle.evidence, evidence_id) for evidence_id in ids or []))
    )


def _area_findings_markdown(bundle: RunBundle, mix: Mix | None) -> list[str]:
    cells_doc = bundle.cells or {}
    ranked = rank_cells(cells_doc, mix)
    lines = ["### Top 3 cells"]
    layers = cells_doc.get("layers", [])
    for item in ranked[:3]:
        cell = item.cell
        lines.extend(
            [
                f"#### {item.rank}. {_text(cell.get('name'))} — {_num(item.score)}",
                f"Cell ID: `{_text(cell.get('id'))}`",
            ]
        )
        for layer in layers:
            layer_id = layer["id"]
            if mix is not None and mix.layers_on is not None and layer_id not in mix.layers_on:
                continue
            lines.append(
                f"- {_text(layer.get('label', layer_id))}: "
                f"{_num(cell.get('values', {}).get(layer_id))} "
                f"{citation(bundle.evidence, layer.get('evidence_id'))}"
            )
        for modifier in cell.get("modifiers", []):
            lines.append(
                f"- **{_text(modifier.get('label'))}**: “{_text(modifier.get('quote'))}” "
                f"{citation(bundle.evidence, modifier.get('evidence_id'))}; "
                f"+{_num(modifier.get('bonus', 0.10))} applied in future mode"
            )
            if "magnitude" in modifier:
                lines.append(f"  - Magnitude: `{_text(modifier['magnitude'])}`")
        for risk in cell.get("risks", []):
            lines.append(
                f"- Risk ({_text(risk.get('severity', 'unspecified'))}): {_text(risk.get('text'))} "
                f"{citation(bundle.evidence, risk.get('evidence_id'))}"
            )
            if risk.get("quote"):
                lines.append(f"  - Quote: “{_text(risk['quote'])}”")
        for unknown in cell.get("unknowns", []):
            lines.append(f"- Unknown: {_text(unknown.get('text'))}")
            if unknown.get("suggested_validation"):
                lines.append(f"  - Suggested validation: {_text(unknown['suggested_validation'])}")
    lines.extend(
        [
            "",
            "### Full ranking",
            "",
            "| Rank | ID | Name | Score |",
            "|---:|---|---|---:|",
        ]
    )
    lines.extend(
        f"| {item.rank} | {_cell(item.cell.get('id'))} | {_cell(item.cell.get('name'))} | {_num(item.score)} |"
        for item in ranked
    )
    return lines


def _point_findings_markdown(bundle: RunBundle) -> list[str]:
    hypotheses = sorted(
        (bundle.candidates or {}).get("hypotheses", []),
        key=lambda hypothesis: hypothesis.get("rank_final", float("inf")),
    )
    lines = ["### Hypotheses"]
    for hypothesis in hypotheses:
        lines.extend(
            [
                f"#### {hypothesis.get('rank_final', '—')}. {_text(hypothesis.get('name'))} "
                f"— {_num(hypothesis.get('score'))}/10",
                "",
                "| Dimension | Score | Why | Evidence |",
                "|---|---:|---|---|",
            ]
        )
        for dimension in hypothesis.get("dimensions", []):
            lines.append(
                f"| {_cell(dimension.get('name'))} | {_cell(dimension.get('score'))} | "
                f"{_cell(dimension.get('why'))} | {_cell(_citation_list(bundle, dimension.get('evidence_ids')))} |"
            )
        for key, label in (("for", "FOR"), ("against", "AGAINST"), ("unknowns", "UNKNOWNS")):
            lines.extend(["", f"**{label}**"])
            for item in hypothesis.get(key, []):
                text = _text(item.get("text"))
                cites = _citation_list(bundle, item.get("evidence_ids"))
                lines.append(f"- {text} {cites}".rstrip())
                if item.get("suggested_validation"):
                    lines.append(f"  - Suggested validation: {_text(item['suggested_validation'])}")
    return lines


def _layers_markdown(bundle: RunBundle) -> list[str]:
    lines = [
        "| Layer | Direction | Weight | Why | Evidence |",
        "|---|:---:|---:|---|---|",
    ]
    for layer in (bundle.cells or {}).get("layers", []):
        lines.append(
            f"| {_cell(layer.get('label', layer.get('id')))} | {_cell(layer.get('direction'))} | "
            f"{_cell(layer.get('weight_default'))} | {_cell(layer.get('why'))} | "
            f"{_cell(citation(bundle.evidence, layer.get('evidence_id')))} |"
        )
    return ["### Layers & weights", "", *lines]


def _sources_markdown(bundle: RunBundle) -> list[str]:
    lines = []
    for entry in bundle.evidence:
        number = entry.get("n")
        prefix = f"[{number}]" if number is not None else "[—]"
        lines.extend(
            [
                f"### {prefix} {_text(entry.get('title'))} — "
                f"{_markdown_source_url(entry.get('url'))}",
                "",
                f"{_text(entry.get('method'))} · tier {_text(entry.get('tier'))} · "
                f"confidence {_text(entry.get('confidence'))} · {_text(entry.get('status'))}",
                "",
                f"> {_text(entry.get('quote'))}",
                "",
            ]
        )
    unsupported = [entry for entry in bundle.evidence if entry.get("status") == "unsupported"]
    if unsupported:
        lines.extend(["### Could not verify", ""])
        for entry in unsupported:
            lines.append(
                f"- {_text(entry.get('title'))} — {_markdown_source_url(entry.get('url'))}: "
                f"“{_text(entry.get('quote'))}”"
            )
    return lines


def _markdown_source_url(value: Any) -> str:
    address = _text(value)
    try:
        is_http = urlsplit(address).scheme.lower() in {"http", "https"}
    except ValueError:
        is_http = False
    if is_http:
        encoded = address.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
        return f"<{encoded}>"
    punctuation = re.compile(f"([{re.escape(string.punctuation)}])")
    return punctuation.sub(r"\\\1", address)


def _trace_markdown(bundle: RunBundle) -> list[str]:
    roles, rejected = _trace_summary(bundle)
    lines = [
        "| Role | Model | Calls | Tokens | Seconds |",
        "|---|---|---:|---:|---:|",
    ]
    for item in roles:
        lines.append(
            f"| {_cell(item['role'])} | {_cell(item['model'])} | {item['calls']} | "
            f"{item['tokens']} | {_num(item['seconds'])} |"
        )
    lines.append(
        f"| **Total** | | **{sum(item['calls'] for item in roles)}** | "
        f"**{sum(item['tokens'] for item in roles)}** | "
        f"**{_num(sum(item['seconds'] for item in roles))}** |"
    )
    if rejected:
        lines.extend(["", "### Fact-checker rejections"])
        lines.extend(
            f"- {_text(event.get('text'))} — {_text(event.get('why'))}".rstrip(" —")
            for event in rejected
        )
    return lines


def render_markdown(bundle: RunBundle, mix: Mix | None = None, share_url: str | None = None) -> str:
    run = bundle.run
    area = (bundle.cells or {}).get("area", {})
    lines = [
        "# Risalite report",
        "",
        f"**Question:** {_text(run.get('question'))}",
        "",
        "## Meta",
        "",
        f"- Shape: {_text(run.get('shape'))}",
        f"- Area: {_text(area.get('name')) if area else ''}",
        f"- Role: {_text(run.get('role'))}",
        f"- Created at: {_text(run.get('created_at'))}",
        f"- {_mix_line(bundle, mix)}",
    ]
    if share_url:
        lines.append(f"- Share: {share_url}")
    lines.extend(["", "## 1. Approved plan", ""])
    plan = run.get("plan", {})
    for subquestion in plan.get("subquestions", []):
        status = "✓" if subquestion.get("approved") else "not approved"
        lines.append(
            f"- {status} — {_text(subquestion.get('text'))} "
            f"(source class: {_text(subquestion.get('source_class'))}; "
            f"layers: {', '.join(map(str, subquestion.get('layers', [])))})"
        )
    for source in plan.get("paid_sources", []):
        lines.append(
            f"- Paid source: {_text(source.get('name'))} — {_text(source.get('used_for'))} "
            f"({_text(source.get('status'))})"
        )
    lines.extend(
        [
            "",
            "## 2. Summary (default mix)",
            "",
            replace_citations(_text(run.get("summary")), bundle.evidence),
            "",
            "## 3. Findings",
            "",
        ]
    )
    if bundle.candidates is not None:
        lines.extend(_point_findings_markdown(bundle))
    else:
        lines.extend(_area_findings_markdown(bundle, mix))
    if bundle.cells is not None:
        lines.extend(["", "## 4. Layers & weights", ""])
        lines.extend(_layers_markdown(bundle))
    lines.extend(["", "## 5. Sources", ""])
    lines.extend(_sources_markdown(bundle))
    lines.extend(["", "## 6. Trace summary", ""])
    lines.extend(_trace_markdown(bundle))
    return "\n".join(lines).rstrip() + "\n"


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _link(url: Any, label: Any | None = None) -> str:
    address = _text(url)
    if urlsplit(address).scheme.lower() in {"http", "https"}:
        return f'<a href="{_escape(address)}">{_escape(address if label is None else label)}</a>'
    return _escape(address if label is None else label)


def _color(value: float) -> str:
    value = max(0.0, min(1.0, value))
    for (left, c1), (right, c2) in zip(_COLOR_STOPS, _COLOR_STOPS[1:]):
        if left <= value <= right:
            ratio = (value - left) / (right - left)
            rgb = tuple(round(a + (b - a) * ratio) for a, b in zip(c1, c2, strict=True))
            return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    rgb = _COLOR_STOPS[-1][1]
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _svg_heatmap(bundle: RunBundle, ranked: list[RankedCell]) -> str:
    collection = bundle.geojson or {}
    features = collection.get("features", [])
    if not features:
        return ""
    all_geometries = []
    mean_latitudes = []
    for feature in features:
        geometry = shape(feature["geometry"])
        all_geometries.append((feature, geometry))
        mean_latitudes.extend(point[1] for point in _coordinates(geometry))
    mean_lat = sum(mean_latitudes) / len(mean_latitudes)
    cosine = math.cos(math.radians(mean_lat))
    projected = [
        (feature, scale(geometry, xfact=cosine, yfact=1, origin=(0, 0)))
        for feature, geometry in all_geometries
    ]
    min_x = min(geometry.bounds[0] for _, geometry in projected)
    min_y = min(geometry.bounds[1] for _, geometry in projected)
    max_x = max(geometry.bounds[2] for _, geometry in projected)
    max_y = max(geometry.bounds[3] for _, geometry in projected)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    width, margin = 640, 12
    scale_factor = (width - 2 * margin) / span_x
    height = max(120, span_y * scale_factor + 2 * margin)
    score_by_id = {str(item.cell.get("id")): item.score for item in ranked}
    rank_by_id = {str(item.cell.get("id")): item.rank for item in ranked[:3]}
    paths = []
    labels = []
    for feature, geometry in projected:
        properties = feature.get("properties", {})
        cell_id = str(properties.get("id", ""))
        score = score_by_id.get(cell_id, 0.5)
        for polygon in _polygons(geometry):
            path_data = []
            rings = [polygon.exterior, *polygon.interiors]
            for ring in rings:
                coordinates = list(ring.coords)
                path_data.append(
                    "M "
                    + " L ".join(
                        f"{margin + (x - min_x) * scale_factor:.2f} "
                        f"{height - margin - (y - min_y) * scale_factor:.2f}"
                        for x, y in coordinates
                    )
                    + " Z"
                )
            paths.append(
                f'<path d="{" ".join(path_data)}" fill="{_color(score)}" '
                f'title="{_escape(cell_id)} — {_escape(_num(score))}" '
                f'fill-rule="evenodd"/>'
            )
        rank = rank_by_id.get(cell_id)
        if rank:
            centroid = geometry.centroid
            cx = margin + (centroid.x - min_x) * scale_factor
            cy = height - margin - (centroid.y - min_y) * scale_factor
            labels.append(
                f'<g aria-label="Rank {rank}"><circle cx="{cx:.2f}" cy="{cy:.2f}" r="10" '
                f'fill="white" stroke="#263238"/><text x="{cx:.2f}" y="{cy + 4:.2f}" '
                f'text-anchor="middle" font-size="10" fill="#15232a">#{rank}</text></g>'
            )
    return (
        '<figure class="heatmap"><svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Cell score heatmap" viewBox="0 0 {width} {height}" width="100%">'
        + '<g stroke="white" stroke-width="1">'
        + "".join(paths)
        + "</g>"
        + "".join(labels)
        + f'<defs><linearGradient id="score-legend"><stop offset="0%" stop-color="{_color(0)}"/>'
        f'<stop offset="100%" stop-color="{_color(1)}"/></linearGradient></defs>'
        '<rect x="12" y="8" width="120" height="8" fill="url(#score-legend)"/>'
        '<text x="12" y="27" font-size="10">0</text><text x="132" y="27" '
        'font-size="10" text-anchor="end">1</text></svg><figcaption>Score heatmap, 0 to 1</figcaption></figure>'
    )


def _coordinates(geometry):
    if geometry.geom_type == "Polygon":
        yield from geometry.exterior.coords
    elif geometry.geom_type == "MultiPolygon":
        for polygon in geometry.geoms:
            yield from polygon.exterior.coords


def _polygons(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type == "MultiPolygon":
        yield from geometry.geoms


def _html_area_findings(bundle: RunBundle, mix: Mix | None) -> str:
    cells_doc = bundle.cells or {}
    ranked = rank_cells(cells_doc, mix)
    layers = cells_doc.get("layers", [])
    blocks = []
    if bundle.geojson:
        blocks.append(_svg_heatmap(bundle, ranked))
    for item in ranked[:3]:
        cell = item.cell
        details = []
        for layer in layers:
            layer_id = layer["id"]
            if mix is not None and mix.layers_on is not None and layer_id not in mix.layers_on:
                continue
            details.append(
                f"<li>{_escape(layer.get('label', layer_id))}: "
                f"{_escape(_num(cell.get('values', {}).get(layer_id)))} "
                f"{_escape(citation(bundle.evidence, layer.get('evidence_id')))}</li>"
            )
        for modifier in cell.get("modifiers", []):
            details.append(
                f"<li><strong>{_escape(modifier.get('label'))}</strong>: "
                f"<q>{_escape(modifier.get('quote'))}</q> "
                f"{_escape(citation(bundle.evidence, modifier.get('evidence_id')))}; "
                f"+{_escape(_num(modifier.get('bonus', 0.10)))} applied in future mode"
                + (
                    f"<div>Magnitude: <code>{_escape(modifier['magnitude'])}</code></div>"
                    if "magnitude" in modifier
                    else ""
                )
                + "</li>"
            )
        for risk in cell.get("risks", []):
            details.append(
                f"<li>Risk ({_escape(risk.get('severity', 'unspecified'))}): "
                f"{_escape(risk.get('text'))} "
                f"{_escape(citation(bundle.evidence, risk.get('evidence_id')))}"
                + (
                    f"<blockquote>{_escape(risk['quote'])}</blockquote>"
                    if risk.get("quote")
                    else ""
                )
                + "</li>"
            )
        for unknown in cell.get("unknowns", []):
            details.append(
                f"<li>Unknown: {_escape(unknown.get('text'))}"
                + (
                    f"<div>Suggested validation: {_escape(unknown['suggested_validation'])}</div>"
                    if unknown.get("suggested_validation")
                    else ""
                )
                + "</li>"
            )
        blocks.append(
            f'<article class="finding"><h3>{item.rank}. {_escape(cell.get("name"))} '
            f"— {_escape(_num(item.score))}</h3><p>Cell ID: <code>{_escape(cell.get('id'))}</code></p>"
            f"<ul>{''.join(details)}</ul></article>"
        )
    rows = "".join(
        f"<tr><td>{item.rank}</td><td>{_escape(item.cell.get('id'))}</td>"
        f"<td>{_escape(item.cell.get('name'))}</td><td>{_escape(_num(item.score))}</td></tr>"
        for item in ranked
    )
    blocks.append(
        "<h3>Full ranking</h3><table><thead><tr><th>Rank</th><th>ID</th><th>Name</th>"
        f"<th>Score</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    return "".join(blocks)


def _html_point_findings(bundle: RunBundle) -> str:
    hypotheses = sorted(
        (bundle.candidates or {}).get("hypotheses", []),
        key=lambda hypothesis: hypothesis.get("rank_final", float("inf")),
    )
    blocks = []
    for hypothesis in hypotheses:
        dimensions = "".join(
            f"<tr><td>{_escape(item.get('name'))}</td><td>{_escape(item.get('score'))}</td>"
            f"<td>{_escape(item.get('why'))}</td><td>{_escape(_citation_list(bundle, item.get('evidence_ids')))}</td></tr>"
            for item in hypothesis.get("dimensions", [])
        )
        sections = []
        for key, label in (("for", "FOR"), ("against", "AGAINST"), ("unknowns", "UNKNOWNS")):
            bullets = []
            for item in hypothesis.get(key, []):
                bullets.append(
                    f"<li>{_escape(item.get('text'))} "
                    f"{_escape(_citation_list(bundle, item.get('evidence_ids')))}"
                    + (
                        f"<div>Suggested validation: {_escape(item['suggested_validation'])}</div>"
                        if item.get("suggested_validation")
                        else ""
                    )
                    + "</li>"
                )
            sections.append(f"<h4>{label}</h4><ul>{''.join(bullets)}</ul>")
        blocks.append(
            f'<article class="finding"><h3>{_escape(hypothesis.get("rank_final"))}. '
            f"{_escape(hypothesis.get('name'))} — {_escape(_num(hypothesis.get('score')))}/10</h3>"
            "<table><thead><tr><th>Dimension</th><th>Score</th><th>Why</th><th>Evidence</th></tr></thead>"
            f"<tbody>{dimensions}</tbody></table>{''.join(sections)}</article>"
        )
    return "".join(blocks)


def _html_sources(bundle: RunBundle) -> str:
    blocks = []
    unsupported = []
    for entry in bundle.evidence:
        if entry.get("status") == "unsupported":
            unsupported.append(entry)
        n = entry.get("n")
        title = _escape(entry.get("title"))
        blocks.append(
            f'<article class="source"><h3>[{_escape(n) if n is not None else "—"}] {title} — '
            f"{_link(entry.get('url'))}</h3><p>{_escape(entry.get('method'))} · tier "
            f"{_escape(entry.get('tier'))} · confidence {_escape(entry.get('confidence'))} · "
            f"{_escape(entry.get('status'))}</p><blockquote>{_escape(entry.get('quote'))}</blockquote></article>"
        )
    if unsupported:
        appendix = "".join(
            f"<li>{_escape(entry.get('title'))} — {_link(entry.get('url'))}: "
            f"<q>{_escape(entry.get('quote'))}</q></li>"
            for entry in unsupported
        )
        blocks.append(f"<h3>Could not verify</h3><ul>{appendix}</ul>")
    return "".join(blocks)


def _html_trace(bundle: RunBundle) -> str:
    roles, rejected = _trace_summary(bundle)
    total_calls = sum(item["calls"] for item in roles)
    total_tokens = sum(item["tokens"] for item in roles)
    total_seconds = sum(item["seconds"] for item in roles)
    rows = "".join(
        f"<tr><td>{_escape(item['role'])}</td><td>{_escape(item['model'])}</td>"
        f"<td>{item['calls']}</td><td>{item['tokens']}</td><td>{_escape(_num(item['seconds']))}</td></tr>"
        for item in roles
    )
    rows += (
        f"<tr><th>Total</th><td></td><th>{total_calls}</th><th>{total_tokens}</th>"
        f"<th>{_escape(_num(total_seconds))}</th></tr>"
    )
    rejected_list = "".join(
        f"<li>{_escape(event.get('text'))} — {_escape(event.get('why'))}</li>" for event in rejected
    )
    return (
        "<table><thead><tr><th>Role</th><th>Model</th><th>Calls</th><th>Tokens</th>"
        f"<th>Seconds</th></tr></thead><tbody>{rows}</tbody></table>"
        + (f"<h3>Fact-checker rejections</h3><ul>{rejected_list}</ul>" if rejected else "")
    )


def render_html(bundle: RunBundle, mix: Mix | None = None, share_url: str | None = None) -> str:
    run = bundle.run
    area = (bundle.cells or {}).get("area", {})
    question = _escape(run.get("question"))
    summary = _escape(replace_citations(_text(run.get("summary")), bundle.evidence)).replace(
        "\n", "<br>\n"
    )
    meta = [
        ("Shape", run.get("shape")),
        ("Area", area.get("name") if area else ""),
        ("Role", run.get("role")),
        ("Created at", run.get("created_at")),
        ("", _mix_line(bundle, mix)),
    ]
    meta_html = "".join(
        f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>"
        if label
        else f"<dt></dt><dd>{_escape(value)}</dd>"
        for label, value in meta
    )
    if share_url:
        meta_html += f"<dt>Share</dt><dd>{_link(share_url)}</dd>"
    plan = run.get("plan", {})
    plan_items = [
        f"<li>{'✓' if item.get('approved') else 'not approved'} — {_escape(item.get('text'))} "
        f"(source class: {_escape(item.get('source_class'))}; layers: "
        f"{_escape(', '.join(map(str, item.get('layers', []))))})</li>"
        for item in plan.get("subquestions", [])
    ]
    plan_items.extend(
        f"<li>Paid source: {_escape(item.get('name'))} — {_escape(item.get('used_for'))} "
        f"({_escape(item.get('status'))})</li>"
        for item in plan.get("paid_sources", [])
    )
    plan_html = "<ul>" + "".join(plan_items) + "</ul>"
    findings_html = (
        _html_point_findings(bundle)
        if bundle.candidates is not None
        else _html_area_findings(bundle, mix)
    )
    layers_html = ""
    if bundle.cells is not None:
        layer_rows = "".join(
            f"<tr><td>{_escape(layer.get('label', layer.get('id')))}</td>"
            f"<td>{_escape(layer.get('direction'))}</td><td>{_escape(layer.get('weight_default'))}</td>"
            f"<td>{_escape(layer.get('why'))}</td>"
            f"<td>{_escape(citation(bundle.evidence, layer.get('evidence_id')))}</td></tr>"
            for layer in bundle.cells.get("layers", [])
        )
        layers_html = (
            "<section><h2>4. Layers &amp; weights</h2><table><thead><tr><th>Layer</th><th>Direction</th>"
            f"<th>Weight</th><th>Why</th><th>Evidence</th></tr></thead><tbody>{layer_rows}</tbody></table></section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Risalite report</title><style>"
        "body{font:16px/1.5 system-ui,sans-serif;color:#18252b;max-width:960px;margin:2rem auto;padding:0 1.2rem}"
        "h1,h2,h3{line-height:1.2}h2{margin-top:2rem;border-bottom:1px solid #cbd5d9;padding-bottom:.35rem}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #cbd5d9;padding:.45rem;text-align:left}"
        "th{background:#f0f4f5}.finding,.source{border:1px solid #d5dfe2;border-radius:5px;padding:1rem;margin:1rem 0}"
        "blockquote{border-left:3px solid #95a7ad;margin:1rem 0;padding:.2rem .8rem;color:#34474e}"
        "dt{font-weight:700}dd{margin:0 0 .3rem}.heatmap{max-width:640px}.heatmap svg{height:auto}"
        ".no-print{margin:1rem 0;padding:.6rem 1rem}.sources{page-break-before:always;break-before:page}"
        "@media print{body{max-width:none;margin:0;padding:0}.no-print{display:none}a{color:inherit;text-decoration:none}"
        ".finding,.source{break-inside:avoid;page-break-inside:avoid}h2,h3{break-after:avoid}}"
        "@page{size:A4;margin:16mm}"
        '</style></head><body><button class="no-print" onclick="window.print()">Print / Save as PDF</button>'
        "<header><h1>Risalite report</h1>"
        f"<p><strong>Question:</strong> {question}</p><h2>Meta</h2><dl>{meta_html}</dl></header>"
        f"<section><h2>1. Approved plan</h2>{plan_html}</section>"
        f"<section><h2>2. Summary (default mix)</h2><p>{summary}</p></section>"
        f"<section><h2>3. Findings</h2>{findings_html}</section>"
        f'{layers_html}<section class="sources"><h2>5. Sources</h2>{_html_sources(bundle)}</section>'
        f"<section><h2>6. Trace summary</h2>{_html_trace(bundle)}</section>"
        "</body></html>"
    )
