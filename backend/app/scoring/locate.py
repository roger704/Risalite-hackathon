import json
import re
from math import hypot
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from pyproj import Transformer
from shapely.geometry import shape

from app.contracts import Evidence, LocateResult
from app.scoring.layers import compute_layers
from app.scoring.scoring import normalize


class AddressNotFoundError(ValueError):
    pass


_ADDRESS = re.compile(
    r"^\s*(?P<street>.+?)\s+(?P<number>\d+)(?P<suffix>[A-Za-z]?)"
    r"(?:\s*,?\s*(?P<postcode>\d{5})(?:\s+Berlin)?)?\s*$",
    re.IGNORECASE,
)


def parse_address(address: str) -> dict[str, str | None]:
    match = _ADDRESS.match(address)
    if not match:
        raise ValueError("Address must include a street and house number")
    return {
        "str_name": match.group("street").strip(),
        "hnr": match.group("number"),
        "hnr_zusatz": match.group("suffix") or "",
        "plz": match.group("postcode"),
    }


def _filter(params):
    fields = [("str_name", params["str_name"]), ("hnr", params["hnr"])]
    if params.get("plz"):
        fields.append(("plz", params["plz"]))
    comparisons = "".join(
        f"<fes:PropertyIsEqualTo><fes:ValueReference>{field}</fes:ValueReference>"
        f"<fes:Literal>{escape(value)}</fes:Literal></fes:PropertyIsEqualTo>"
        for field, value in fields
    )
    return (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:And>'
        + comparisons
        + "</fes:And></fes:Filter>"
    )


async def locate(
    address: str,
    *,
    service,
    http=None,
    categories=None,
    radius_m: float = 500,
) -> LocateResult:
    if radius_m < 0:
        raise ValueError("radius_m must be nonnegative")
    params = parse_address(address)
    query = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": "adressen_berlin:adressen_berlin",
        "OUTPUTFORMAT": "application/json",
        "COUNT": "5",
        "FILTER": _filter(params),
    }
    base_url = "https://gdi.berlin.de/services/wfs/adressen_berlin"
    client = http or httpx.AsyncClient()
    close_client = http is None
    try:
        response = await client.get(base_url, params=query)
        response.raise_for_status()
        payload = response.json()
        request_url = str(response.url)
    finally:
        if close_client:
            await client.aclose()
    features = payload.get("features", [])
    if params.get("hnr_zusatz"):
        features = [
            feature
            for feature in features
            if str(feature.get("properties", {}).get("hnr_zusatz", "")).casefold()
            == params["hnr_zusatz"].casefold()
        ]
    if not features:
        raise AddressNotFoundError(f"No matching address found: {address}")
    feature = features[0]
    props = feature.get("properties", {})
    geometry = shape(feature["geometry"])
    source_crs = payload.get("crs", {}).get("properties", {}).get("name", "EPSG:25833")
    if "25833" in source_crs:
        x, y = geometry.x, geometry.y
        transformer = Transformer.from_crs(25833, 4326, always_xy=True)
        lon, lat = transformer.transform(x, y)
    else:
        lon, lat = geometry.x, geometry.y
        transformer = Transformer.from_crs(4326, 25833, always_xy=True)
        x, y = transformer.transform(lon, lat)
    plr_id = props["plr_nr"]
    plr_id = f"{plr_id:08d}" if isinstance(plr_id, int) else str(plr_id)
    names_listing = next(
        item for item in service.list_datasets() if item["dataset_id"] == "berlin_lor_names"
    )
    names_manifest = service.catalog.get("berlin_lor_names", names_listing["latest_snapshot_id"])
    name_records = service._load_records(names_manifest)
    name_map = {item.area_code: {**item.attributes, "name": item.name} for item in name_records}
    selected_bezirk = (
        props.get("bez_name") or name_map.get(plr_id, {}).get("bez", "").split(" - ")[-1]
    )
    district_ids = sorted(
        key
        for key, val in name_map.items()
        if val.get("bez", "").split(" - ")[-1] == selected_bezirk
    )

    layer_config = categories or {}
    if "layers" not in layer_config:
        path = Path(__file__).resolve().parents[2] / "config" / "scoring" / "coffee_v1.json"
        layer_config = json.loads(path.read_text(encoding="utf-8"))
    layer_config["_area_name"] = selected_bezirk
    layer_results = compute_layers(service, district_ids, layer_config, names=name_map)
    raw_values = {layer.layer_id: layer.values.get(plr_id) for layer in layer_results}
    normalized_values = {}
    for layer in layer_results:
        normalized, _ = normalize(
            {
                cell: value
                for cell, value in layer.values.items()
                if value is not None and cell in district_ids
            },
            next(
                item.get("normalization", "auto")
                for item in layer_config["layers"]
                if item["id"] == layer.layer_id
            ),
            layer_config.get("skew_threshold", 1.0),
        )
        normalized_values[layer.layer_id] = normalized.get(plr_id)

    def competitor_records(dataset_id, accepted_categories):
        listing = next(item for item in service.list_datasets() if item["dataset_id"] == dataset_id)
        manifest = service.catalog.get(dataset_id, listing["latest_snapshot_id"])
        results = []
        for record in service._load_records(manifest):
            category = getattr(record, "canonical_category", None)
            if category not in accepted_categories or not getattr(record, "geometry", None):
                continue
            point = shape(record.geometry)
            transformer = Transformer.from_crs(4326, 25833, always_xy=True)
            mx, my = transformer.transform(point.x, point.y)
            distance = hypot(mx - x, my - y)
            if distance <= radius_m:
                results.append(
                    {
                        "name": getattr(record, "name", None) or record.record_id,
                        "category": category,
                        "distance_m": round(distance, 1),
                        "record_id": record.record_id,
                        "lon": point.x,
                        "lat": point.y,
                    }
                )
        results.sort(key=lambda item: (item["distance_m"], item["record_id"]))
        return results, manifest

    category_config = layer_config.get("categories", {})
    osm, osm_manifest = competitor_records("osm_pois", category_config.get("osm", []))
    ihk, ihk_manifest = competitor_records("ihk_businesses", category_config.get("ihk", []))
    evidence = [
        Evidence(
            id="s2-locate-wfs",
            method="wfs",
            title="Berlin address geocoding WFS",
            url=request_url,
            quote=f"{address} → PLR {plr_id} {props.get('plr_name', '')}",
            tier="official",
            confidence="high",
        )
    ]
    for identifier, dataset_id, manifest, count, label in (
        ("osm", "osm_pois", osm_manifest, len(osm), "OSM competitors"),
        ("ihk", "ihk_businesses", ihk_manifest, len(ihk), "IHK competitors"),
    ):
        evidence.append(
            Evidence(
                id=f"s2-locate-{identifier}-{manifest.snapshot_id}",
                method="snapshot",
                title=label,
                url=service.configs[dataset_id].resource_url,
                quote=f"{count} {label.lower()} within {radius_m:g} m of {address}",
                tier="community" if identifier == "osm" else "official",
                confidence="high",
                source_snapshot_id=manifest.snapshot_id,
            )
        )
    evidence.extend(layer.evidence for layer in layer_results)
    return LocateResult(
        address=props,
        point={"type": "Point", "coordinates": [lon, lat]},
        plr_id=plr_id,
        plr_name=props.get("plr_name", name_map.get(plr_id, {}).get("name", plr_id)),
        values=normalized_values,
        raw_values=raw_values,
        osm=osm,
        ihk=ihk,
        evidence=[item.model_dump(mode="json") for item in evidence],
    )
