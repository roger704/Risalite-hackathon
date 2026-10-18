"""Signed, graded and capped future modifiers (Deep_Research_Spec §5.8.4)."""

from __future__ import annotations

import math

MAGNITUDES: dict[str, float] = {"small": 0.03, "medium": 0.06, "large": 0.10}
BONUS_CAP = 0.15
MAX_COMPLETION_YEAR = 2031


def _size(unit: str | None, amount: float) -> str:
    if unit == "dwellings":
        return "small" if amount < 200 else "medium" if amount < 1000 else "large"
    if unit == "kita_places":
        return "small" if amount < 50 else "medium" if amount <= 150 else "large"
    if unit == "children_0_6":
        return "small" if amount < 50 else "medium" if amount < 200 else "large"
    return "small"


def has_number(amount: float | None) -> bool:
    return amount is not None and not (isinstance(amount, float) and math.isnan(amount))


def bonus_size(unit: str | None, amount: float | None) -> tuple[str, float]:
    """(size label, unsigned magnitude); no number or unknown unit → small."""
    if not has_number(amount):
        return "small", MAGNITUDES["small"]
    size = _size(unit, abs(float(amount)))
    return size, MAGNITUDES[size]


def signed_bonus(direction: str, magnitude: float) -> float:
    return magnitude if direction == "+" else -magnitude


def capped_sum(bonuses: list[float], cap: float = BONUS_CAP) -> float:
    total = sum(bonuses)
    return max(-cap, min(cap, total))


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def future_score(
    present: float | None, bonuses: list[float], cap: float = BONUS_CAP
) -> float | None:
    """score_future = clamp01(score_present + capped Σ bonus)."""
    if present is None:
        return None
    return clamp01(present + capped_sum(bonuses, cap))
