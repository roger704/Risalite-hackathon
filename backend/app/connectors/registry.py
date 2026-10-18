"""Adapter name -> connector class. Modules load lazily so planned adapters can be
registered before they exist; ingesting an unimplemented adapter fails cleanly."""

import importlib

from app.domain import ConfigError

ADAPTERS: dict[str, str] = {
    "lor": "app.connectors.lor:LORConnector",
    "population": "app.connectors.population:PopulationConnector",
    "osm": "app.connectors.osm:OSMConnector",
    "ihk": "app.connectors.ihk:IHKConnector",
    "gtfs": "app.connectors.gtfs:GTFSConnector",
    "berlin_wfs": "app.connectors.berlin_wfs:BerlinWFSConnector",
    "berlin_green_spaces": "app.connectors.berlin_green_spaces:BerlinGreenSpacesConnector",
}


def load_connector(adapter: str):
    target = ADAPTERS.get(adapter)
    if target is None:
        raise ConfigError(f"Adapter is not implemented: {adapter}")
    module_name, class_name = target.split(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ConfigError(f"Adapter is not implemented: {adapter}") from exc
    return getattr(module, class_name)()
