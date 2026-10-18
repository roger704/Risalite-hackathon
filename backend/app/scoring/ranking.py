"""Weighted mean, tiers, rank rule and stability on plain values (§5.5, §5.8.6, §9.7)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

TIE_EPSILON = 1e-9
PERTURBATIONS = 200
PERTURBATION_SPREAD = 0.30
PERTURBATION_SEED = 20261018


def weighted_score(
    values: Mapping[str, float | None], weights: Mapping[str, float]
) -> float | None:
    """Weighted mean of directional values; missing layers dropped and weights re-normalized."""
    total = 0.0
    weight_sum = 0.0
    for layer_id, weight in weights.items():
        value = values.get(layer_id)
        if weight <= 0 or value is None:
            continue
        total += weight * value
        weight_sum += weight
    return total / weight_sum if weight_sum > 0 else None


@dataclass(frozen=True)
class RankItem:
    id: str
    score: float | None
    eligible: bool
    demoted: bool


def base_order(items: Sequence[RankItem]) -> list[int]:
    """Indices sorted by (eligible desc, score desc, id)."""
    return sorted(
        range(len(items)),
        key=lambda i: (
            not items[i].eligible,
            items[i].score is None,
            -(items[i].score or 0.0),
            items[i].id,
        ),
    )


def _demote(group: list[int], items: Sequence[RankItem]) -> list[int]:
    keys: list[float] = []
    for position, index in enumerate(group):
        key: float = position
        if items[index].demoted:
            successor = next(
                (j for j in range(position + 1, len(group)) if not items[group[j]].demoted),
                None,
            )
            if successor is not None:
                key = successor + 0.5
        keys.append(key)
    order = sorted(range(len(group)), key=lambda position: (keys[position], position))
    return [group[position] for position in order]


def rank_order(items: Sequence[RankItem]) -> list[int]:
    """§5.5 rank rule; demotion is applied within the eligible and the ineligible group
    separately, so an ineligible cell never moves ahead of an eligible one."""
    base = base_order(items)
    eligible = [i for i in base if items[i].eligible]
    ineligible = [i for i in base if not items[i].eligible]
    return _demote(eligible, items) + _demote(ineligible, items)


def tiers(items: Sequence[RankItem], band: float) -> dict[int, int]:
    """Tier per item index on the base order: within `band` of the previous cell → same tier;
    the first ineligible cell always opens a new tier."""
    result: dict[int, int] = {}
    tier = 0
    previous: RankItem | None = None
    for index in base_order(items):
        item = items[index]
        tied = (
            previous is not None
            and previous.eligible == item.eligible
            and previous.score is not None
            and item.score is not None
            and abs(previous.score - item.score) <= band + TIE_EPSILON
        )
        if not tied:
            tier += 1
        result[index] = tier
        previous = item
    return result


def top_ids(scores: Mapping[str, float | None], k: int = 3) -> list[str]:
    present = [(cell_id, score) for cell_id, score in scores.items() if score is not None]
    present.sort(key=lambda item: (-item[1], item[0]))
    return [cell_id for cell_id, _ in present[:k]]


def top3_shares(
    values: Mapping[str, Mapping[str, float | None]],
    weights: Mapping[str, float],
    *,
    runs: int = PERTURBATIONS,
    spread: float = PERTURBATION_SPREAD,
    seed: int = PERTURBATION_SEED,
) -> dict[str, float]:
    """Share of `runs` weight perturbations (each weight × U(1 − spread, 1 + spread)) in which
    a cell is in the top-3. `values` holds only the eligible cells."""
    counts = {cell_id: 0 for cell_id in values}
    if not values:
        return {}
    rng = random.Random(seed)
    active = sorted(layer_id for layer_id, weight in weights.items() if weight > 0)
    for _ in range(runs):
        perturbed = {
            layer_id: weights[layer_id] * rng.uniform(1 - spread, 1 + spread) for layer_id in active
        }
        scores = {cell_id: weighted_score(cell, perturbed) for cell_id, cell in values.items()}
        for cell_id in top_ids(scores):
            counts[cell_id] += 1
    return {cell_id: count / runs for cell_id, count in counts.items()}
