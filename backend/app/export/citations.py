"""Resolve evidence IDs to stable ledger citation numbers."""

import re

_ID_SHAPED = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\d[A-Za-z0-9_.:-]*")


def citation_number(evidence: list[dict], evidence_id: str | None) -> int | None:
    if not evidence_id:
        return None
    for entry in evidence:
        if entry.get("evidence_id") == evidence_id:
            return entry.get("n")
    return None


def citation(evidence: list[dict], evidence_id: str | None) -> str:
    number = citation_number(evidence, evidence_id)
    return f"[{number}]" if number is not None else ""


def replace_citations(text: str, evidence: list[dict]) -> str:
    numbers = {entry.get("evidence_id"): entry.get("n") for entry in evidence}

    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token in numbers:
            number = numbers[token]
            return f"[{number}]" if number is not None else ""
        return "" if _ID_SHAPED.fullmatch(token) else match.group(0)

    return re.sub(r"\[([^\[\]]+)\](?!\()", replace, text)
