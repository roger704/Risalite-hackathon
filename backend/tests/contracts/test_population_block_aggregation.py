"""Hand-computed aggregation contracts for population blocks."""

import pytest

from app.application.population_blocks import aggregate_blocks_to_areas
from app.domain import POPULATION_BLOCK_AGE_BANDS, PopulationBlock, ValueStatus


def block(
    block_id,
    *,
    area_code="X",
    area_ha=1.0,
    residents=0,
    density=0.0,
    status=ValueStatus.OBSERVED,
    flags=(),
    snapshot_id="blocks-snapshot",
    reference_period="2023-12-31",
    boundary_edition="2021",
):
    suppressed = status is ValueStatus.SUPPRESSED
    return PopulationBlock(
        source_id="berlin_population_density_blocks",
        snapshot_id=snapshot_id,
        source_record_id=block_id,
        record_id=f"population_block:{block_id}",
        geometry={
            "type": "MultiPolygon",
            "coordinates": [[[[13.35, 52.5], [13.36, 52.5], [13.36, 52.51], [13.35, 52.5]]]],
        },
        block_id=block_id,
        area_ha=area_ha,
        residents=None if suppressed else residents,
        residents_status=status,
        density_per_ha=None if suppressed else density,
        reference_period=reference_period,
        residents_by_age={} if suppressed else dict.fromkeys(POPULATION_BLOCK_AGE_BANDS, 0),
        area_code=area_code,
        hierarchy="planungsraum" if area_code else None,
        boundary_edition=boundary_edition if area_code else None,
        area_overlap_share=1.0 if area_code else None,
        quality_flags=flags,
    )


def test_aggregates_observed_suppressed_and_unassigned_blocks():
    records = [
        block("observed-a", area_ha=2, residents=100, density=50),
        block("observed-b", area_ha=1, residents=300, density=300),
        block("suppressed", area_ha=1, status=ValueStatus.SUPPRESSED),
        block(
            "unassigned",
            area_code=None,
            area_ha=1,
            residents=25,
            density=25,
        ),
    ]
    result = aggregate_blocks_to_areas(records)
    aggregate = result.areas["X"]
    assert aggregate.block_count == 3
    assert aggregate.observed_block_count == 2
    assert aggregate.suppressed_block_count == 1
    assert aggregate.residents_observed == 400
    assert aggregate.residents_upper_bound == 402
    assert aggregate.block_area_ha == 4.0
    assert aggregate.observed_block_area_ha == 3.0
    assert aggregate.gross_density_per_ha == 100.0
    assert aggregate.net_density_per_ha == 133.3333
    assert aggregate.residents_weighted_density_per_ha == 237.5
    assert result.unassigned_block_count == 1
    assert result.unassigned_residents_observed == 25


def test_zero_observed_population_keeps_density_missing():
    records = [
        block("suppressed-a", status=ValueStatus.SUPPRESSED),
        block("suppressed-b", status=ValueStatus.SUPPRESSED, area_ha=2),
    ]
    result = aggregate_blocks_to_areas(records)
    aggregate = result.areas["X"]
    assert aggregate.residents_observed == 0
    assert aggregate.residents_upper_bound == 4
    assert aggregate.gross_density_per_ha is None
    assert aggregate.net_density_per_ha is None
    assert aggregate.residents_weighted_density_per_ha is None


@pytest.mark.parametrize(
    "records",
    [
        [block("a"), block("b", snapshot_id="other")],
        [block("a"), block("b", reference_period="2024-12-31")],
        [object()],
    ],
)
def test_rejects_mixed_or_non_block_records(records):
    with pytest.raises(ValueError):
        aggregate_blocks_to_areas(records)


def test_rejects_mixed_area_definitions():
    records = [block("a"), block("b", boundary_edition="2023")]
    with pytest.raises(ValueError, match="Population blocks mix hierarchy/boundary_edition values"):
        aggregate_blocks_to_areas(records)
