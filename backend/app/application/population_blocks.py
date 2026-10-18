"""Umweltatlas block-based residents (31.12.2023), deliberately not joined with or substituted
for the official ``berlin_population`` snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.application.service import DataService
from app.domain import PopulationBlock, ValueStatus, record_from_dict


@dataclass(frozen=True)
class BlockAreaAggregate:
    area_code: str
    hierarchy: str
    boundary_edition: str
    reference_period: str
    source_snapshot_id: str
    block_count: int
    observed_block_count: int
    suppressed_block_count: int
    ambiguous_block_count: int
    partial_block_count: int
    block_area_ha: float
    observed_block_area_ha: float
    residents_observed: int
    residents_upper_bound: int
    gross_density_per_ha: float | None
    net_density_per_ha: float | None
    residents_weighted_density_per_ha: float | None


@dataclass(frozen=True)
class BlockAggregation:
    areas: dict[str, BlockAreaAggregate]
    unassigned_block_count: int
    unassigned_residents_observed: int


def aggregate_blocks_to_areas(
    records: Iterable[PopulationBlock], *, suppression_max_residents: int = 2
) -> BlockAggregation:
    if type(suppression_max_residents) is not int or suppression_max_residents < 0:
        raise ValueError("suppression_max_residents must be a nonnegative integer")
    items = list(records)
    if any(not isinstance(record, PopulationBlock) for record in items):
        raise ValueError("records must contain only PopulationBlock records")
    snapshots = {record.snapshot_id for record in items}
    reference_periods = {record.reference_period for record in items}
    if len(snapshots) > 1:
        raise ValueError("Population blocks mix snapshot_id values")
    if len(reference_periods) > 1:
        raise ValueError("Population blocks mix reference_period values")
    area_definitions = {
        (record.hierarchy, record.boundary_edition)
        for record in items
        if record.area_code is not None
    }
    if len(area_definitions) > 1:
        raise ValueError("Population blocks mix hierarchy/boundary_edition values")
    grouped: dict[str, list[PopulationBlock]] = {}
    unassigned = []
    for record in items:
        if record.area_code is None:
            unassigned.append(record)
        else:
            grouped.setdefault(record.area_code, []).append(record)

    areas = {}
    for area_code, blocks in sorted(grouped.items()):
        observed = [block for block in blocks if block.residents_status is ValueStatus.OBSERVED]
        suppressed = [block for block in blocks if block.residents_status is ValueStatus.SUPPRESSED]
        area_ha = sum(block.area_ha for block in blocks)
        observed_area_ha = sum(block.area_ha for block in observed)
        residents = sum(block.residents or 0 for block in observed)
        weighted_density_sum = sum(
            (block.residents or 0) * (block.density_per_ha or 0) for block in observed
        )
        denominator = residents
        first = blocks[0]
        areas[area_code] = BlockAreaAggregate(
            area_code=area_code,
            hierarchy=first.hierarchy,
            boundary_edition=first.boundary_edition,
            reference_period=first.reference_period,
            source_snapshot_id=first.snapshot_id,
            block_count=len(blocks),
            observed_block_count=len(observed),
            suppressed_block_count=len(suppressed),
            ambiguous_block_count=sum(
                "plr_assignment_ambiguous" in block.quality_flags for block in blocks
            ),
            partial_block_count=sum(
                "plr_assignment_partial" in block.quality_flags for block in blocks
            ),
            block_area_ha=round(area_ha, 4),
            observed_block_area_ha=round(observed_area_ha, 4),
            residents_observed=residents,
            residents_upper_bound=residents + suppression_max_residents * len(suppressed),
            gross_density_per_ha=(round(residents / area_ha, 4) if area_ha and residents else None),
            net_density_per_ha=(
                round(residents / observed_area_ha, 4) if observed_area_ha and residents else None
            ),
            residents_weighted_density_per_ha=(
                round(weighted_density_sum / denominator, 4) if denominator else None
            ),
        )
    return BlockAggregation(
        areas=areas,
        unassigned_block_count=len(unassigned),
        unassigned_residents_observed=sum(
            block.residents or 0
            for block in unassigned
            if block.residents_status is ValueStatus.OBSERVED
        ),
    )


def block_aggregates(
    service: DataService,
    snapshot_id: str,
    dataset_id: str = "berlin_population_density_blocks",
    *,
    suppression_max_residents: int = 2,
) -> BlockAggregation:
    records = []
    cursor = None
    while True:
        page = service.records(dataset_id, snapshot_id, limit=1000, cursor=cursor)
        records.extend(record_from_dict(record) for record in page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            return aggregate_blocks_to_areas(
                records, suppression_max_residents=suppression_max_residents
            )
