"""T3 Kita gap per PLR with supply-completeness range (Deep_Research_Spec §5.8.3)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_COVERAGE_TARGET = 0.9
INCOMPLETE_IMPUTED_SHARE = 0.20


@dataclass(frozen=True)
class KitaGap:
    gap_low: float | None
    gap_high: float | None
    imputed_places: float
    incomplete: bool

    @property
    def is_range(self) -> bool:
        return self.imputed_places > 0


def capacity_percentile(capacities: Sequence[float], q: float = 0.75) -> float:
    """Linear-interpolated percentile of known Berlin facility capacities (default p75)."""
    ordered = sorted(float(value) for value in capacities if value is not None)
    if not ordered:
        raise ValueError("no facility capacities")
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def kita_gap(
    children_0_6: float | None,
    places_known: float,
    missing_facilities: int = 0,
    imputed_capacity: float = 0.0,
    *,
    coverage_target: float = DEFAULT_COVERAGE_TARGET,
    incomplete_share: float = INCOMPLETE_IMPUTED_SHARE,
) -> KitaGap:
    """gap = (children 0–6 × coverage target − places) / children 0–6.

    `gap_high` uses known places only, `gap_low` imputes each facility without capacity at
    `imputed_capacity`; the PLR is `incomplete` if imputed places exceed `incomplete_share`
    of the estimated supply. Feed `gap_low` as the raw `kita_gap` layer value.
    """
    imputed = max(0, missing_facilities) * imputed_capacity
    supply = places_known + imputed
    incomplete = supply > 0 and imputed / supply > incomplete_share
    if not children_0_6 or children_0_6 <= 0:
        return KitaGap(None, None, imputed, incomplete)
    demand = children_0_6 * coverage_target
    gap_high = (demand - places_known) / children_0_6
    gap_low = (demand - supply) / children_0_6
    return KitaGap(gap_low, gap_high, imputed, incomplete)
