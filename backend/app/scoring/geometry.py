import geopandas as gpd
from shapely.geometry import mapping, shape


def load_plr_geometries(records) -> gpd.GeoDataFrame:
    rows = [
        {
            "id": record.area_code,
            "name": record.name,
            "geometry": shape(record.geometry),
        }
        for record in records
        if getattr(record, "record_type", None) == "area"
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def cells_geojson(gdf_subset: gpd.GeoDataFrame, names: dict[str, dict[str, str]]) -> dict:
    projected = gdf_subset.to_crs(25833)
    features = []
    for _, row in projected.sort_values("id").iterrows():
        geometry = row.geometry.simplify(10, preserve_topology=True)
        geometry = gpd.GeoSeries([geometry], crs=25833).to_crs(4326).iloc[0]
        feature = mapping(geometry)

        def rounded(value):
            if isinstance(value, (tuple, list)):
                return [rounded(item) for item in value]
            if isinstance(value, float):
                return round(value, 6)
            return value

        attrs = names.get(row.id, {})
        features.append(
            {
                "type": "Feature",
                "id": row.id,
                "geometry": {
                    "type": feature["type"],
                    "coordinates": rounded(feature["coordinates"]),
                },
                "properties": {
                    "id": row.id,
                    "name": attrs.get("name", row.get("name", "")),
                    "bzr_name": attrs.get("bzr_name", ""),
                    "pgr_name": attrs.get("pgr_name", ""),
                    "bez": attrs.get("bez", ""),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
