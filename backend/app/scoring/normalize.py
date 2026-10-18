"""Layer transforms and normalization for scoring v2 (Deep_Research_Spec §5.8.1)."""

from __future__ import annotations

import math
from collections.abc import Sequence

SATURATION_DENOMINATOR_FLOOR = 2000.0
MIN_PERCENTILE_CELLS = 5


def percentile_rank(value: float, reference: Sequence[float]) -> float:
    """Mid-rank percentile of value within reference, 0..1 (shared with T1).

    p = (count below + 0.5 * count equal) / n; ties are averaged, a constant layer gives 0.5.
    """
    if not reference:
        return 0.5
    below = sum(1 for item in reference if item < value)
    equal = sum(1 for item in reference if item == value)
    return (below + 0.5 * equal) / len(reference)


def min_max(value: float, reference: Sequence[float]) -> float:
    """Min–max position of value within reference (used when n < 5); constant → 0.5."""
    if not reference:
        return 0.5
    low, high = min(reference), max(reference)
    if high <= low:
        return 0.5
    return min(1.0, max(0.0, (value - low) / (high - low)))


def p99(values: Sequence[float]) -> float:
    """Nearest-rank 99th percentile."""
    ordered = sorted(values)
    index = max(0, math.ceil(0.99 * len(ordered)) - 1)
    return ordered[index]


def transform(name: str, value: float | None, residents: float | None) -> float | None:
    """Per-value part of a layer transform (`cap_p99` is applied over a set, see `cap_set`)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    value = float(value)
    if name in ("identity", "none", "cap_p99"):
        return value
    if name == "log1p":
        return math.log1p(max(value, 0.0))
    if name == "per_10k_residents":
        denominator = max(residents or 0.0, SATURATION_DENOMINATOR_FLOOR)
        return value / denominator * 10_000
    raise ValueError(f"unknown transform: {name}")


def cap_set(name: str, values: Sequence[float]) -> float | None:
    """Upper cap for a value set (only `cap_p99`)."""
    if name != "cap_p99" or not values:
        return None
    return p99(values)


def directional(p: float, direction: str) -> float:
    return p if direction == "+" else 1.0 - p
