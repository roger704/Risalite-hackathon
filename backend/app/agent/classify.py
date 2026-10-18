import re

from app.agent.interfaces import ModelClient
from app.contracts.api import Classification

DISTRICTS = {
    "mitte": "Mitte",
    "friedrichshain-kreuzberg": "Friedrichshain-Kreuzberg",
    "friedrichshain kreuzberg": "Friedrichshain-Kreuzberg",
    "kreuzberg": "Friedrichshain-Kreuzberg",
    "friedrichshain": "Friedrichshain-Kreuzberg",
    "pankow": "Pankow",
    "prenzlauer berg": "Pankow",
    "charlottenburg-wilmersdorf": "Charlottenburg-Wilmersdorf",
    "charlottenburg": "Charlottenburg-Wilmersdorf",
    "wilmersdorf": "Charlottenburg-Wilmersdorf",
    "spandau": "Spandau",
    "steglitz-zehlendorf": "Steglitz-Zehlendorf",
    "steglitz": "Steglitz-Zehlendorf",
    "zehlendorf": "Steglitz-Zehlendorf",
    "tempelhof-schöneberg": "Tempelhof-Schöneberg",
    "tempelhof": "Tempelhof-Schöneberg",
    "schöneberg": "Tempelhof-Schöneberg",
    "neukölln": "Neukölln",
    "treptow-köpenick": "Treptow-Köpenick",
    "treptow": "Treptow-Köpenick",
    "köpenick": "Treptow-Köpenick",
    "marzahn-hellersdorf": "Marzahn-Hellersdorf",
    "marzahn": "Marzahn-Hellersdorf",
    "hellersdorf": "Marzahn-Hellersdorf",
    "lichtenberg": "Lichtenberg",
    "reinickendorf": "Reinickendorf",
    "wedding": "Mitte",
    "moabit": "Mitte",
}
_ADDRESS = re.compile(
    r"\b[\wäöüß.-]+(?:straße|str\.|platz|allee|damm|weg|ufer)\s+\d+\b",
    re.IGNORECASE,
)
_POINT_WORDS = ("lot", "parcel", "grundstück", "storefront", "this property")
_GAP_WORDS = ("missing", "fehlen", "gap", "lack", "where are")


def _fallback(question: str) -> Classification:
    lowered = question.casefold()
    address_match = _ADDRESS.search(question)
    point = bool(address_match) or any(word in lowered for word in _POINT_WORDS)
    gap = not point and any(word in lowered for word in _GAP_WORDS)
    shape = "point" if point else "gap" if gap else "location"
    template = (
        "coffee"
        if any(word in lowered for word in ("café", "cafe", "coffee", "kaffee"))
        else "kita"
        if any(word in lowered for word in ("kita", "daycare"))
        else "property"
        if point
        else None
    )
    normalized = lowered.replace("ß", "ss")
    area = None
    for alias, canonical in sorted(DISTRICTS.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.casefold().replace("ß", "ss") in normalized:
            area = canonical
            break
    clarification = None
    if point and not address_match:
        clarification = "Which address should I analyse?"
    elif not point and area is None:
        clarification = "Which district or neighbourhood?"
    return Classification(
        shape=shape,
        template=template,
        area=area,
        address=address_match.group(0) if address_match else None,
        why="Keyword classification: "
        + ("specific property" if point else "service gap" if gap else "area opportunity"),
        needs_clarification=clarification is not None,
        clarification_question=clarification,
    )


async def classify(
    question: str, profiles: dict | None = None, model: ModelClient | None = None
) -> Classification:
    if model is None:
        return _fallback(question)
    try:
        result, _ = await model.structured(
            "classifier",
            "plan",
            {"question": question, "profiles": profiles or {}},
            Classification,
        )
        return Classification.model_validate(result.model_dump())
    except Exception:
        return _fallback(question)
