"""Place-name normalization for deterministic finding localization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.application.address_search import normalize_street

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_DIRECTION = re.compile(
    r"\s+(?:Nordost|Nordwest|Südost|Südwest|Nord|Süd|Ost|West)\s*$", re.IGNORECASE
)
_HOUSE_NUMBER = re.compile(r"(?:^|\s)(?P<number>\d+(?:\s*[A-Za-z])?)\s*$")


def _tokens(text: str) -> tuple[str, ...]:
    raw_tokens = _TOKEN.findall(text)
    normalized = []
    index = 0
    while index < len(raw_tokens):
        value = normalize_street(raw_tokens[index]).replace("straße", "strasse")
        if value:
            if (
                value.isdigit()
                and index + 1 < len(raw_tokens)
                and len(raw_tokens[index + 1]) == 1
                and raw_tokens[index + 1].isalpha()
            ):
                suffix = normalize_street(raw_tokens[index + 1]).replace("straße", "strasse")
                normalized.append(f"{value}{suffix}")
                index += 2
                continue
            normalized.append(value)
        index += 1
    return tuple(normalized)


def _term_key(term: str) -> tuple[str, ...]:
    return _tokens(term)


def _valid_term(term: str, minimum_length: int) -> bool:
    return bool(_term_key(term)) and len(normalize_street(term).replace("straße", "strasse")) >= (
        minimum_length
    )


def _dedupe(terms: Iterable[str], minimum_length: int = 4) -> list[str]:
    result = []
    seen = set()
    for term in terms:
        value = term.strip()
        key = _term_key(value)
        if not _valid_term(value, minimum_length) or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def cell_terms(name: str | None, streets: Iterable[str] = ()) -> list[str]:
    candidates: list[str] = []
    if name:
        candidates.append(name)
        without_parentheses = re.sub(r"\s*\([^)]*\)", " ", name).strip()
        candidates.append(without_parentheses)
        for candidate in (name, without_parentheses):
            without_direction = _DIRECTION.sub("", candidate).strip()
            candidates.append(without_direction)
    candidates.extend(street for street in streets if street)
    return _dedupe(candidates)


def _address_parts(address: str | None) -> tuple[str, str | None]:
    if not address:
        return "", None
    street = address.split(",", 1)[0].strip()
    street = re.sub(r"(?:^|\s+)Berlin\s*$", "", street, flags=re.IGNORECASE).strip()
    street = re.sub(r"\s+\d{5}\s*$", "", street).strip()
    match = _HOUSE_NUMBER.search(street)
    if match is None:
        return street, None
    number = re.sub(r"\s+", "", match.group("number"))
    return street[: match.start()].strip(" ,"), number


def address_street(address: str | None) -> str | None:
    street, _ = _address_parts(address)
    return street or None


def address_terms(address: str | None) -> list[str]:
    street, number = _address_parts(address)
    if not street:
        return []
    term = f"{street} {number}" if number else street
    return _dedupe([term], minimum_length=0)


def mentioned(text: str, terms: Sequence[str]) -> str | None:
    text_tokens = _tokens(text)
    for term in terms:
        term_tokens = _term_key(term)
        if not term_tokens or len(term_tokens) > len(text_tokens):
            continue
        width = len(term_tokens)
        if any(
            text_tokens[i : i + width] == term_tokens for i in range(len(text_tokens) - width + 1)
        ):
            return term
    return None
