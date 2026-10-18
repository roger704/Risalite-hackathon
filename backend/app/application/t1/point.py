"""T1 LocalLens point preparation: address → PLR → catchment → deterministic data dimensions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.application.address_search import parse_query
from app.application.t1.catalog import Catalog, CatalogType, load_catalog
from app.application.t1.catchment import Catchment, resolve_catchment
from app.application.t1.readers import Poi, PointReaders, ReferencePlr
from app.evidence.ledger import EvidenceLedger, dataset_entry
from app.scoring import v2

CONFIDENCE = ("high", "medium", "low")
MAX_CLARIFY = 10


def default_readers() -> PointReaders:
    from app.application.service import DataService
    from app.application.t1.readers import SnapshotReaders

    return SnapshotReaders(DataService())


def percentile(value: float | None, reference: Sequence[float]) -> float | None:
    if value is None or not reference:
        return None
    return min(1.0, max(0.0, float(v2.percentile_rank(float(value), list(reference)))))


def _score(fraction: float | None, neutral: float) -> float:
    if fraction is None:
        return neutral
    return round(min(10.0, max(0.0, 10.0 * fraction)), 1)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _lower(level: str, steps: int = 1) -> str:
    return CONFIDENCE[min(len(CONFIDENCE) - 1, CONFIDENCE.index(level) + steps)]


def _confidence(reasons: list[tuple[str, bool]]) -> tuple[str, list[str]]:
    """reasons: (text, cap_low). One step down per reason; cap_low forces `low`."""
    level = "high"
    for _, cap in reasons:
        level = "low" if cap else _lower(level)
    return level, [text for text, _ in reasons]


def _label(record: Any) -> str:
    number = (
        f" {record.house_number}{record.house_number_suffix or ''}" if record.house_number else ""
    )
    postcode = f", {record.postcode}" if record.postcode else ""
    return f"{record.street_name}{number}{postcode} Berlin"


def _candidate(record: Any) -> dict[str, Any]:
    lon, lat = record.geometry["coordinates"][:2]
    return {
        "address": _label(record),
        "lat": lat,
        "lon": lon,
        "postcode": record.postcode,
        "district_name": record.district_name,
        "locality_name": record.locality_name,
        "plr_id": record.area_code,
        "plr_name": record.area_name,
        "stable_id": record.record_id,
    }


def resolve_address(query: str, readers: PointReaders) -> dict[str, Any]:
    """{'resolved': candidate} or {'clarify': [candidates], 'reason': ...}."""
    index = readers.address_index()
    if index is None:
        return {"clarify": [], "reason": "address snapshot unavailable"}
    try:
        parsed = parse_query(query)
    except ValueError:
        return {"clarify": [], "reason": "query has no searchable street"}
    total, page, ambiguous, groups = index.search(parsed, limit=MAX_CLARIFY)
    if total == 0:
        return {"clarify": [], "reason": "no matching address"}
    candidates = [_candidate(record) | {"match_type": match} for record, match in page]
    if total > 1 or ambiguous:
        return {
            "clarify": candidates,
            "reason": "ambiguous address",
            "total": total,
            "groups": groups,
        }
    return {"resolved": candidates[0]}


class _Evidence:
    def __init__(self, readers: PointReaders, ledger: EvidenceLedger | None, point: dict[str, Any]):
        self.readers = readers
        self.ledger = ledger
        self.point = point
        self.entries: dict[str, dict[str, Any]] = {}

    def add(self, key: str, role: str, layer_id: str, value: Any, quote: str) -> str | None:
        source = self.readers.source(role)
        if source is None:
            return None
        entry = dataset_entry(
            dataset_id=source["dataset_id"],
            snapshot_id=source["snapshot_id"],
            layer_id=layer_id,
            source_url=source.get("source_url") or "",
            value=value,
            query=dict(self.point),
            quote=quote,
            title=f"T1 {layer_id}",
        )
        entry["value"] = value
        if self.ledger is not None:
            entry = self.ledger.append(entry)
        self.entries[key] = entry
        return entry["evidence_id"]

    def ids(self, *keys: str) -> list[str]:
        if self.ledger is None:
            return []
        return [self.entries[key]["evidence_id"] for key in keys if key in self.entries]


def _transit_sum(values: dict[str, float] | None, windows: Sequence[str]) -> float | None:
    if values is None or not windows:
        return None
    return float(sum(values.get(name, 0.0) for name in windows))


def _rate(count: float, residents: float, floor: int) -> float:
    return count * 10_000 / max(residents, floor)


class _Point:
    def __init__(
        self,
        catalog: Catalog,
        catchment: Catchment,
        residents: float | None,
        profile: dict[str, Any],
        transit: dict[str, float] | None,
        centre_m: float | None,
        reference: list[ReferencePlr],
    ):
        self.catalog = catalog
        self.catchment = catchment
        self.residents = residents
        self.profile = profile
        self.transit = transit
        self.centre_m = centre_m
        self.reference = reference
        self.floor = catalog.min_denominator
        self.low = residents is not None and residents < catalog.low_resident_threshold
        self.denominator = max(residents or 0.0, self.floor)

    def _ref(self, getter) -> list[float]:
        values = (getter(plr) for plr in self.reference)
        return [float(value) for value in values if value is not None]

    def demand(self, item: CatalogType) -> tuple[float, str, list[str], list[str]]:
        neutral = self.catalog.neutral_score
        if self.residents is None:
            return (
                neutral,
                "Catchment residents unknown (census grid unavailable).",
                ["residents"],
                [],
            )
        density = self.residents / self.catchment.area_km2
        p_density = percentile(
            density, self._ref(lambda r: r.residents / r.area_km2 if r.area_km2 else None)
        )
        parts = [f"{self.residents:,.0f} catchment residents ({density:,.0f}/km²"]
        parts[0] += f", Berlin PLR percentile {p_density:.2f})" if p_density is not None else ")"
        used = ["residents"]
        if self.low:
            parts.append(
                f"fewer than {self.catalog.low_resident_threshold:,} residents: demand uses residents only"
            )
            return _score(p_density, neutral), "; ".join(parts) + ".", used, []
        fractions = [p_density] if p_density is not None else []
        missing = []
        for driver in item.demand_drivers:
            if driver == "residents":
                continue
            if driver == "young_adult_share":
                share = self.profile.get("young_adult_share")
                p = percentile(share, self._ref(lambda r: r.young_adult_share))
                label = f"18–45 share {share:.2f} in the PLR" if share is not None else ""
            elif driver == "transit":
                value = _transit_sum(self.transit, item.transit_windows)
                p = percentile(
                    value, self._ref(lambda r: _transit_sum(r.transit, item.transit_windows))
                )
                label = (
                    f"{value:,.0f} departures ({'+'.join(item.transit_windows)})"
                    if value is not None
                    else ""
                )
            else:
                p = percentile(self.centre_m, self._ref(lambda r: r.centre_distance_m))
                p = None if p is None else 1 - p
                label = (
                    f"{self.centre_m:,.0f} m to the nearest centre"
                    if self.centre_m is not None
                    else ""
                )
            if p is None:
                missing.append(driver)
                continue
            fractions.append(p)
            used.append(driver)
            parts.append(f"{label} (p={p:.2f})")
        if missing:
            parts.append("not available: " + ", ".join(missing))
        return _score(_mean(fractions), neutral), "; ".join(parts) + ".", used, missing

    def competition(self, item: CatalogType, count: int) -> tuple[float, str, float]:
        rate = _rate(count, self.residents or 0.0, self.floor)
        cats = item.competitors
        ref = [
            _rate(sum(plr.counts.get(c, 0) for c in cats), plr.residents, self.floor)
            for plr in self.reference
        ]
        p = percentile(rate, ref)
        why = (
            f"{count} competitors ({', '.join(cats)}) in the catchment = {rate:.1f} per 10k residents "
            f"(denominator max(residents, {self.floor:,}) = {self.denominator:,.0f})"
        )
        why += (
            f"; Berlin PLR percentile {p:.2f}, gap = 1 − p."
            if p is not None
            else "; no Berlin reference."
        )
        return _score(None if p is None else 1 - p, self.catalog.neutral_score), why, rate

    def footfall(self, item: CatalogType, count: int) -> tuple[float, str, bool]:
        parts, fractions = [], []
        if item.complements:
            density = count / self.catchment.area_km2
            ref = [
                sum(plr.counts.get(c, 0) for c in item.complements) / plr.area_km2
                for plr in self.reference
                if plr.area_km2
            ]
            p = percentile(density, ref)
            parts.append(f"{count} complements ({', '.join(item.complements)}) = {density:.1f}/km²")
            if p is not None:
                fractions.append(p)
                parts[-1] += f" (p={p:.2f})"
        windows = tuple(self.catalog.transit_windows)
        value = _transit_sum(self.transit, windows)
        p = percentile(value, self._ref(lambda r: _transit_sum(r.transit, windows)))
        if value is None:
            parts.append("transit departures not available")
        else:
            parts.append(
                f"{value:,.0f} scheduled departures {'+'.join(windows)} at stations in the catchment"
            )
            if p is not None:
                fractions.append(p)
                parts[-1] += f" (p={p:.2f})"
        return (
            _score(_mean(fractions), self.catalog.neutral_score),
            "; ".join(parts) + ".",
            value is not None,
        )


def _preferences(profile: dict | None, catalog: Catalog) -> tuple[set[str], set[str]]:
    if not profile:
        return set(), set()
    preferred = {str(v) for v in profile.get("preferred_types") or ()}
    excluded = {str(v) for v in profile.get("excluded_types") or ()}
    experience = profile.get("experience") or ""
    if isinstance(experience, (list, tuple)):
        experience = " ".join(str(v) for v in experience)
    text = f"{experience} {profile.get('description') or ''}".casefold()
    for item in catalog.types:
        if any(word in text for word in item.operator_keywords):
            preferred.add(item.id)
    return preferred - excluded, excluded


def prepare_point(
    query: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    operator_profile: dict | None = None,
    radius_m: int = 500,
    readers: PointReaders | None = None,
    ledger: EvidenceLedger | None = None,
    catalog: Catalog | None = None,
    top_n: int = 3,
) -> dict:
    catalog = catalog or load_catalog()
    readers = readers or default_readers()
    address_record: dict[str, Any] | None = None
    if lat is None or lon is None:
        if not query or not query.strip():
            raise ValueError("either an address query or lat/lon is required")
        resolved = resolve_address(query, readers)
        if "resolved" not in resolved:
            return resolved
        address_record = resolved["resolved"]
        lat, lon = address_record["lat"], address_record["lon"]
        address = address_record["address"]
    else:
        address = query.strip() if query and query.strip() else f"{lat:.6f}, {lon:.6f}"

    plr = None
    if address_record and address_record.get("plr_id"):
        plr = {"plr_id": address_record["plr_id"], "name": address_record.get("plr_name")}
    if plr is None:
        plr = readers.plr_at(lon, lat)
    minutes = float(catalog.catchment.get("isochrone_minutes", 7))
    catchment = resolve_catchment(lon, lat, radius_m, minutes)
    census = readers.catchment_residents(catchment.geometry)
    residents = None if census is None else float(census["value"])
    windows = catalog.transit_windows
    transit = readers.transit_departures(catchment.geometry, windows)
    centre_m = readers.centre_distance_m(lon, lat)
    min_pop = int(catalog.catchment.get("reference_min_population", 2000))
    reference = [
        plr_ref for plr_ref in readers.berlin_reference(windows) if plr_ref.residents >= min_pop
    ]
    profile = readers.plr_profile(plr["plr_id"]) if plr else {}
    point = _Point(catalog, catchment, residents, profile, transit, centre_m, reference)
    evidence = _Evidence(
        readers, ledger, {"lat": round(lat, 6), "lon": round(lon, 6), **catchment.describe()}
    )

    space_id = None
    if address_record:
        space_id = evidence.add(
            "address",
            "addresses",
            "address_point",
            {"stable_id": address_record["stable_id"], "lat": lat, "lon": lon},
            f"{address} at {lat:.6f}, {lon:.6f}",
        )
    if plr:
        evidence.add("plr", "boundary", "plr_lookup", plr, f"Point lies in PLR {plr['plr_id']}")
    if residents is not None:
        evidence.add(
            "residents",
            "census",
            "catchment_residents",
            round(residents, 1),
            f"{residents:,.0f} census residents in the catchment",
        )
    if transit is not None:
        evidence.add(
            "transit", "transit", "catchment_transit", transit, f"Scheduled departures {transit}"
        )
    if centre_m is not None:
        evidence.add(
            "centre",
            "centres",
            "centre_distance",
            round(centre_m, 1),
            f"{centre_m:,.0f} m to centre",
        )

    point_reasons: list[tuple[str, bool]] = []
    if residents is None:
        point_reasons.append(("catchment residents unknown", True))
    elif point.low:
        point_reasons.append(
            (
                f"low_resident_catchment: {residents:,.0f} residents < {catalog.low_resident_threshold:,}",
                True,
            )
        )
    elif residents < int(catalog.catchment.get("confidence_resident_threshold", 3000)):
        point_reasons.append((f"catchment residents {residents:,.0f} < 3,000", False))
    if not reference:
        point_reasons.append(("Berlin PLR reference unavailable", True))
    if transit is None:
        point_reasons.append(("optional layer missing: transit", False))
    if catchment.note and "failed" in catchment.note:
        point_reasons.append(("fallback layer used: radius instead of walking isochrone", False))

    preferred, excluded = _preferences(operator_profile, catalog)
    skipped = [
        {
            "id": item.id,
            "reason": f"mapping {item.mapping}: needs an {catalog.category_mapping} extension",
        }
        for item in catalog.types
        if not catalog.is_offered(item)
    ]
    skipped += [{"id": t, "reason": "excluded by operator profile"} for t in sorted(excluded)]
    weights = {d: catalog.dimension_weights.get(d, 1.0) for d in catalog.data_dimensions}
    considered = []
    for item in catalog.offered():
        if item.id in excluded:
            continue
        competitors = sorted(
            readers.pois(item.competitors, catchment.geometry), key=lambda p: p.record_id
        )
        complements = readers.pois(item.complements, catchment.geometry) if item.complements else []
        d_score, d_why, used, missing = point.demand(item)
        c_score, c_why, rate = point.competition(item, len(competitors))
        f_score, f_why, has_transit = point.footfall(item, len(complements))
        comp_key, compl_key = f"competitors:{item.id}", f"complements:{item.id}"
        comp_id = evidence.add(
            comp_key,
            "places",
            f"t1_competitors:{item.id}",
            {"count": len(competitors), "record_ids": [p.record_id for p in competitors]},
            f"{len(competitors)} {'/'.join(item.competitors)} POIs in the catchment",
        )
        evidence.add(
            compl_key,
            "places",
            f"t1_complements:{item.id}",
            {"count": len(complements)},
            f"{len(complements)} {'/'.join(item.complements)} POIs in the catchment",
        )
        demand_keys = (
            ["residents"]
            + (["transit"] if "transit" in used else [])
            + (["plr"] if "young_adult_share" in used else [])
            + (["centre"] if "centre_proximity" in used else [])
        )
        dimensions = [
            {
                "name": "Demand",
                "score": d_score,
                "why": d_why,
                "evidence_ids": evidence.ids(*demand_keys),
            },
            {
                "name": "Competition gap",
                "score": c_score,
                "why": c_why,
                "evidence_ids": evidence.ids(comp_key, "residents"),
            },
            {
                "name": "Footfall/Transit",
                "score": f_score,
                "why": f_why,
                "evidence_ids": evidence.ids(compl_key, *(["transit"] if has_transit else [])),
            },
        ]
        total_w = sum(weights.values()) or 1.0
        prescore = round(sum(weights[d["name"]] * d["score"] for d in dimensions) / total_w, 2)
        reasons = list(point_reasons)
        reasons += [(f"optional layer missing: {driver}", False) for driver in missing]
        confidence, confidence_reasons = _confidence(reasons)
        considered.append(
            {
                "id": item.id,
                "name": item.label,
                "prescore": prescore,
                "preferred": item.id in preferred,
                "dimensions": dimensions,
                "metrics": {
                    "competitors": len(competitors),
                    "competitors_per_10k": round(rate, 2),
                    "complements": len(complements),
                    "residents": residents,
                    "denominator": point.denominator,
                },
                "confidence": confidence,
                "confidence_reasons": confidence_reasons,
                "space_checks": list(item.space_checks),
                "operator_checks": list(item.operator_checks),
                "why": ("operator profile match; " if item.id in preferred else "")
                + f"data pre-score {prescore:.1f}/10 (demand {d_score}, gap {c_score}, footfall {f_score})",
                "_competitors": [(p, comp_id) for p in competitors],
            }
        )
    considered.sort(key=lambda h: (not h["preferred"], -h["prescore"], h["id"]))
    chosen = considered[:top_n]
    competitors_out = [
        _competitor(h["id"], poi, eid) for h in chosen for poi, eid in h.pop("_competitors")
    ]
    for h in considered[top_n:]:
        h.pop("_competitors")
    level, reasons_out = _confidence(point_reasons)
    return {
        "template_id": catalog.id,
        "space": {
            "address": address,
            "lat": lat,
            "lon": lon,
            "cell_id": plr["plr_id"] if plr else None,
            "cell_name": plr.get("name") if plr else None,
            "evidence_id": space_id if ledger is not None else None,
        },
        "operator": operator_profile,
        "radius_m": radius_m,
        "catchment": {
            **catchment.describe(),
            "residents": residents,
            "residents_source": None if census is None else census.get("method"),
            "residents_complete": None if census is None else census.get("complete"),
            "denominator": point.denominator,
            "reference_plr_count": len(reference),
            "transit_departures": transit,
            "centre_distance_m": centre_m,
        },
        "low_resident_catchment": point.low,
        "confidence": level,
        "confidence_reasons": reasons_out,
        "hypotheses": chosen,
        "considered": [
            {"id": h["id"], "prescore": h["prescore"], "preferred": h["preferred"]}
            for h in considered
        ],
        "skipped_types": skipped,
        "competitors": competitors_out,
    }


def _competitor(hypothesis_id: str, poi: Poi, evidence_id: str | None) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "lat": poi.lat,
        "lon": poi.lon,
        "name": poi.name,
        "category": poi.category,
        "record_id": poi.record_id,
        "evidence_id": evidence_id or poi.record_id,
    }
