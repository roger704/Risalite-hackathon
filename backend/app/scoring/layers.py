import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import geopandas as gpd
from shapely.geometry import shape
from shapely.ops import unary_union

from app.contracts import Evidence


@dataclass
class LayerResult:
    layer_id: str
    values: dict[str, float | None]
    units: str
    method: str
    source_url: str
    snapshot_id: str
    evidence: Evidence


def _snapshot(service, loaded, dataset_id):
    if dataset_id in loaded:
        return loaded[dataset_id]
    listing = {item["dataset_id"]: item for item in service.list_datasets()}
    snapshot_id = listing.get(dataset_id, {}).get("latest_snapshot_id")
    if not snapshot_id:
        raise ValueError(f"No local snapshot is available for {dataset_id}")
    manifest = service.catalog.get(dataset_id, snapshot_id)
    loaded[dataset_id] = (manifest, service._load_records(manifest))
    return loaded[dataset_id]


def _evidence_id(layer_id, snapshot_id, plr_ids, area):
    payload = snapshot_id + area + ",".join(sorted(plr_ids))
    return f"s2-{layer_id}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def _layer_result(
    service,
    layer_id,
    values,
    units,
    method,
    dataset_id,
    manifest,
    plr_ids,
    area,
    quote,
    *,
    details=None,
):
    source_url = service.configs[dataset_id].resource_url
    evidence_id = _evidence_id(layer_id, manifest.snapshot_id, plr_ids, area)
    evidence = Evidence(
        id=evidence_id,
        method="snapshot",
        title=f"{dataset_id} {layer_id}",
        url=source_url,
        quote=quote,
        tier="official" if dataset_id not in {"osm_pois"} else "community",
        confidence="high",
        source_snapshot_id=manifest.snapshot_id,
        retrieved_at=manifest.retrieved_at.isoformat(),
        units=units,
        details=details,
    )
    return LayerResult(layer_id, values, units, method, source_url, manifest.snapshot_id, evidence)


def compute_layers(
    service,
    plr_ids: list[str],
    config: dict,
    *,
    records: dict[str, list] | None = None,
    geometries: gpd.GeoDataFrame | None = None,
    names: dict[str, dict[str, str]] | None = None,
) -> list[LayerResult]:
    loaded = {}
    if records is not None:
        for dataset_id, record_list in records.items():
            if isinstance(record_list, tuple) and len(record_list) == 2:
                loaded[dataset_id] = record_list
            else:
                dataset_config = service.configs[dataset_id]
                manifest = SimpleNamespace(
                    snapshot_id=f"injected-{dataset_id}",
                    retrieved_at=datetime.now(timezone.utc),
                    reference_period=dataset_config.reference_period,
                )
                loaded[dataset_id] = (manifest, record_list)
    names = names or {}
    if geometries is None:
        lor_manifest, lor_records = _snapshot(service, loaded, "berlin_lor")
        from app.scoring.geometry import load_plr_geometries

        geometries = load_plr_geometries(lor_records)
    subset = geometries[geometries["id"].isin(plr_ids)].copy().to_crs(25833)
    polygons = dict(zip(subset["id"], subset.geometry))
    layers = []
    by_id = {item["id"]: item for item in config["layers"]}
    categories = config.get("categories", {})

    if "demand_density" in by_id or "young_adult_share" in by_id:
        population_manifest, population_records = _snapshot(service, loaded, "berlin_population")
        observations = {}
        for record in population_records:
            if record.area_code in plr_ids:
                observations.setdefault(record.area_code, {})[
                    (record.metric_code, record.age_min, record.age_max_exclusive)
                ] = record
        density = {}
        young_share = {}
        for plr_id in plr_ids:
            obs = observations.get(plr_id, {})
            total_record = obs.get(("population_total", None, None))
            total = total_record.value if total_record else None
            area_km2 = polygons[plr_id].area / 1_000_000
            density[plr_id] = (
                float(total) / area_km2 if total is not None and area_km2 > 0 else None
            )
            bands = (
                obs.get(("population_age_band", 18, 27)),
                obs.get(("population_age_band", 27, 45)),
            )
            young_share[plr_id] = (
                (float(bands[0].value) + float(bands[1].value)) / float(total)
                if total not in (None, 0) and all(band and band.value is not None for band in bands)
                else None
            )
        if "demand_density" in by_id:
            vals = [value for value in density.values() if value is not None]
            quote = (
                f"{len(plr_ids)} PLRs; residents/km² = population_total "
                f"({population_manifest.reference_period}) / PLR area in EPSG:25833; "
                f"min {min(vals):.2f}, max {max(vals):.2f}"
                if vals
                else f"{len(plr_ids)} PLRs; population_total divided by PLR area in EPSG:25833; no observed values"
            )
            layers.append(
                _layer_result(
                    service,
                    "demand_density",
                    density,
                    "residents/km²",
                    "population_total / area_m2 × 1,000,000",
                    "berlin_population",
                    population_manifest,
                    plr_ids,
                    config.get("_area_name", ""),
                    quote,
                )
            )
        if "young_adult_share" in by_id:
            vals = [value for value in young_share.values() if value is not None]
            quote = (
                f"{len(plr_ids)} PLRs; share = residents age [18,27) + [27,45) / population_total "
                "(exclusive upper bound); "
                f"min {min(vals):.4f}, max {max(vals):.4f}"
                if vals
                else f"{len(plr_ids)} PLRs; age bands [18,45) with exclusive upper bound; no observed values"
            )
            layers.append(
                _layer_result(
                    service,
                    "young_adult_share",
                    young_share,
                    "share of residents",
                    "population age bands [18,27) + [27,45) / population_total",
                    "berlin_population",
                    population_manifest,
                    plr_ids,
                    config.get("_area_name", ""),
                    quote,
                )
            )

    if "competition_osm" in by_id:
        manifest, places = _snapshot(service, loaded, "osm_pois")
        selected = [
            place
            for place in places
            if getattr(place, "canonical_category", None) in categories.get("osm", [])
            and getattr(place, "geometry", None)
        ]
        points = gpd.GeoDataFrame(
            {"record_id": [item.record_id for item in selected]},
            geometry=[shape(item.geometry) for item in selected],
            crs="EPSG:4326",
        ).to_crs(25833)
        joined = gpd.sjoin(
            points,
            subset[["id", "geometry"]],
            how="inner",
            predicate="within",
        )
        joined = joined.drop_duplicates("record_id", keep="first")
        counts = joined.groupby("id").size().to_dict()
        values = {plr_id: float(counts.get(plr_id, 0)) for plr_id in plr_ids}
        layers.append(
            _layer_result(
                service,
                "competition_osm",
                values,
                "cafés (OSM)",
                "count points within PLR polygon (EPSG:25833)",
                "osm_pois",
                manifest,
                plr_ids,
                config.get("_area_name", ""),
                f"{len(plr_ids)} PLRs; count of OSM records in configured categories within PLR polygons; min {min(values.values()):.0f}, max {max(values.values()):.0f}",
            )
        )

    if "competition_ihk" in by_id:
        manifest, businesses = _snapshot(service, loaded, "ihk_businesses")
        categories_ihk = categories.get("ihk", [])
        selected = [
            item
            for item in businesses
            if getattr(item, "canonical_category", None) in categories_ihk
        ]
        counts = {}
        unassigned = 0
        for item in selected:
            code = getattr(item, "area_code", None)
            if code is None:
                unassigned += 1
            elif code in plr_ids:
                counts[code] = counts.get(code, 0) + 1
        values = {plr_id: float(counts.get(plr_id, 0)) for plr_id in plr_ids}
        layers.append(
            _layer_result(
                service,
                "competition_ihk",
                values,
                "registered cafés (IHK)",
                "count category-matched businesses by supplied IHK area_code",
                "ihk_businesses",
                manifest,
                plr_ids,
                config.get("_area_name", ""),
                f"{len(plr_ids)} PLRs; IHK category counts by supplied area_code (not coordinates); min {min(values.values()):.0f}, max {max(values.values()):.0f}; {unassigned} selected records have null area_code",
                details={"unassigned": unassigned},
            )
        )

    if "transit" in by_id:
        manifest, transit_records = _snapshot(service, loaded, "vbb_gtfs")
        settings = service.configs["vbb_gtfs"].parser_options
        start, end = "07:00:00", "10:00:00"
        services = [
            item
            for item in transit_records
            if getattr(item, "record_type", None) == "stop_service"
            and item.window_start == start
            and item.window_end == end
        ]
        stops = {
            item.stop_id: shape(item.geometry)
            for item in transit_records
            if getattr(item, "record_type", None) == "transit_stop"
        }
        selected_services = [item for item in services if item.stop_id in stops]
        points = {item.stop_id: stops[item.stop_id] for item in selected_services}
        buffers = {stop_id: point.buffer(75) for stop_id, point in points.items()}
        clusters = []
        if buffers:
            merged = unary_union(list(buffers.values()))
            geoms = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
            for geom in geoms:
                members = [
                    stop_id for stop_id, buffer in buffers.items() if geom.intersects(buffer)
                ]
                clusters.append(members)
        departures = {item.stop_id: item.departures for item in selected_services}
        values = {}
        hub_counts = {}
        for plr_id in plr_ids:
            polygon = polygons[plr_id].buffer(400)
            counted = [
                members
                for members in clusters
                if any(polygon.intersects(points[member]) for member in members)
            ]
            values[plr_id] = float(
                sum(departures.get(member, 0) for members in counted for member in members)
            )
            hub_counts[plr_id] = len(counted)
        service_date = settings.get("service_date", "configured service date")
        layers.append(
            _layer_result(
                service,
                "transit",
                values,
                "scheduled departures 07–10 (service date from config)",
                "cluster stops within 150 m; sum scheduled departures for hubs within 400 m",
                "vbb_gtfs",
                manifest,
                plr_ids,
                config.get("_area_name", ""),
                f"{len(plr_ids)} PLRs; scheduled supply, not footfall; departures in 07:00–10:00 on {service_date}; hubs clustered within 150 m and counted within 400 m of PLR boundaries",
                details={
                    "service_date": service_date,
                    "cluster_distance_m": 150,
                    "plr_buffer_m": 400,
                },
            )
        )
    return layers


def _compute_one(layer_id, service, plr_ids, config, **kwargs):
    selected = {
        **config,
        "layers": [item for item in config.get("layers", []) if item["id"] == layer_id]
        or [{"id": layer_id}],
    }
    return next(
        layer
        for layer in compute_layers(service, plr_ids, selected, **kwargs)
        if layer.layer_id == layer_id
    )


def demand_density(service, plr_ids, config, **kwargs):
    return _compute_one("demand_density", service, plr_ids, config, **kwargs)


def young_adult_share(service, plr_ids, config, **kwargs):
    return _compute_one("young_adult_share", service, plr_ids, config, **kwargs)


def competition_osm(service, plr_ids, config, **kwargs):
    return _compute_one("competition_osm", service, plr_ids, config, **kwargs)


def competition_ihk(service, plr_ids, config, **kwargs):
    return _compute_one("competition_ihk", service, plr_ids, config, **kwargs)


def transit(service, plr_ids, config, **kwargs):
    return _compute_one("transit", service, plr_ids, config, **kwargs)
