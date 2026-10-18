import re
from dataclasses import dataclass


class AreaNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedArea:
    name: str
    level: str
    matched_on: str | None
    plr_ids: list[str]


def _normal(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().replace("ß", "ss")).strip()


def resolve_area(name: str | None, names: dict[str, dict[str, str]]) -> ResolvedArea:
    all_ids = sorted(names)
    if _normal(name) in {"", "berlin", "all"}:
        return ResolvedArea("Berlin", "city", None, all_ids)

    target = _normal(name)
    fields = (
        ("bezirk", "bez"),
        ("prognoseraum", "pgr_name"),
        ("bezirksregion", "bzr_name"),
        ("plr", "name"),
    )
    for level, field in fields:
        matches = []
        for plr_id, attributes in names.items():
            value = attributes.get(field, "")
            if field == "bez":
                value = value.split(" - ", 1)[-1]
            if _normal(value) == target:
                matches.append(plr_id)
        if matches:
            return ResolvedArea(name.strip(), level, field, sorted(matches))

    prefix = sorted(
        plr_id
        for plr_id, attributes in names.items()
        if _normal(attributes.get("pgr_name", "")).startswith(target + " ")
    )
    if prefix:
        return ResolvedArea(
            name.strip(), "ortsteil_approx", "prefix of Prognoseräume (approximation)", prefix
        )
    if re.fullmatch(r"\d{8}", name.strip()) and name.strip() in names:
        return ResolvedArea(
            names[name.strip()].get("name", name.strip()), "plr", "plr_id", [name.strip()]
        )

    suggestions = sorted(
        {
            attributes.get(field, "")
            for attributes in names.values()
            for field in ("bez", "pgr_name", "bzr_name", "name")
            if attributes.get(field)
        }
    )
    close = [item for item in suggestions if target and target in _normal(item)][:5]
    if not close:
        close = suggestions[:5]
    raise AreaNotFoundError(f"Area not found: {name!r}. Suggestions: {', '.join(close) or 'none'}")
