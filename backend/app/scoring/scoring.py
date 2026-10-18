from collections import defaultdict
from math import sqrt


def _sample_skewness(values: list[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    second = sum((value - mean) ** 2 for value in values) / n
    if second == 0:
        return 0.0
    third = sum((value - mean) ** 3 for value in values) / n
    return sqrt(n * (n - 1)) / (n - 2) * third / (second**1.5)


def normalize(
    values: dict[str, float], method: str = "auto", skew_threshold: float = 1.0
) -> tuple[dict[str, float], str]:
    clean = {key: float(value) for key, value in values.items() if value is not None}
    ordered = sorted(clean.items(), key=lambda item: item[1])
    used = method
    if method == "auto":
        used = (
            "percentile"
            if len(clean) >= 5 and abs(_sample_skewness(list(clean.values()))) > skew_threshold
            else "minmax"
        )
    if used == "minmax":
        low = min(clean.values(), default=0.0)
        high = max(clean.values(), default=0.0)
        if high == low:
            return {key: 0.5 for key in clean}, used
        return {key: (value - low) / (high - low) for key, value in clean.items()}, used
    if used != "percentile":
        raise ValueError(f"Unsupported normalization method: {used}")
    count = len(ordered)
    if count < 2:
        return {key: 0.5 for key, _ in ordered}, used
    result = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        percentile = ((index + end - 1) / 2) / (count - 1)
        for key, _ in ordered[index:end]:
            result[key] = percentile
        index = end
    return result, used


def score_cells(
    values: dict[str, dict[str, float | None]],
    layers_cfg: list[dict],
    weights: dict[str, float] | None = None,
    modifiers: dict[str, list[dict]] | None = None,
    bonus: float = 0.10,
    *,
    names: dict[str, str] | None = None,
    skew_threshold: float = 1.0,
) -> tuple[dict[str, dict], list[dict], dict[str, str]]:
    names = names or {}
    modifiers = modifiers or {}
    layer_ids = {layer["id"] for layer in layers_cfg}
    if values and set(values).issubset(layer_ids):
        values = {
            plr_id: {
                layer_id: layer_values.get(plr_id) for layer_id, layer_values in values.items()
            }
            for plr_id in sorted({key for group in values.values() for key in group})
        }
    active = []
    for layer in layers_cfg:
        layer_id = layer["id"]
        weight = layer.get("weight_default", 0.0) if weights is None else weights.get(layer_id, 0.0)
        if weight > 0:
            active.append((layer, float(weight)))

    excluded = {}
    for cell_id, cell_values in values.items():
        missing = [
            layer
            for layer, _ in active
            if layer.get("required", False) and cell_values.get(layer["id"]) is None
        ]
        if missing:
            details = [
                f"{layer['id']} (population suppressed/missing)"
                if layer["id"] in {"demand_density", "young_adult_share"}
                else f"{layer['id']} (source value missing)"
                for layer in missing
            ]
            excluded[cell_id] = {
                "id": cell_id,
                "name": names.get(cell_id, cell_id),
                "reason": "missing required value: " + ", ".join(details),
            }

    included = [cell_id for cell_id in values if cell_id not in excluded]
    normalized = {}
    methods = {}
    missing_layers = defaultdict(list)
    for layer, _ in active:
        layer_id = layer["id"]
        present = {
            cell_id: values[cell_id].get(layer_id)
            for cell_id in included
            if values[cell_id].get(layer_id) is not None
        }
        normalized[layer_id], methods[layer_id] = normalize(
            present, layer.get("normalization", "auto"), skew_threshold
        )
        for cell_id in included:
            if values[cell_id].get(layer_id) is None:
                missing_layers[cell_id].append(layer_id)

    raw_scores = {}
    for cell_id in included:
        terms = []
        for layer, weight in active:
            value = normalized[layer["id"]].get(cell_id)
            if value is not None:
                direction = 1 if layer.get("direction", "+") == "+" else -1
                terms.append((weight, direction * value))
        denom = sum(weight for weight, _ in terms)
        raw_scores[cell_id] = (
            sum(weight * value for weight, value in terms) / denom if denom else 0.0
        )

    low = min(raw_scores.values(), default=0.0)
    high = max(raw_scores.values(), default=0.0)
    result = {}
    for cell_id in included:
        present = 0.5 if high == low else (raw_scores[cell_id] - low) / (high - low)
        cell_modifiers = modifiers.get(cell_id, [])
        future_bonus = sum(
            float(item.get("bonus", bonus))
            for item in cell_modifiers
            if item.get("type") == "future" and item.get("accepted") is not False
        )
        result[cell_id] = {
            "values": {
                layer["id"]: round(normalized[layer["id"]][cell_id], 4)
                for layer, _ in active
                if cell_id in normalized[layer["id"]]
            },
            "raw_values": dict(values[cell_id]),
            "missing_layers": missing_layers[cell_id],
            "modifiers": cell_modifiers,
            "score_present": round(present, 4),
            "score_future": round(min(1.0, present + future_bonus), 4),
            "raw_score": raw_scores[cell_id],
        }
    return result, list(excluded.values()), methods
