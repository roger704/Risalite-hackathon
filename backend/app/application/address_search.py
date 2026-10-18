"""Deterministic local search over a published Berlin address snapshot."""

from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import pyarrow.parquet as pq

from app.domain import Address, record_from_dict


@dataclass(frozen=True)
class ParsedQuery:
    street_key: str
    house_number: int | None = None
    house_number_suffix: str | None = None
    postcode: str | None = None


def normalize_street(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    tokens = [
        token[:-3] + "strasse" if token == "str" or token.endswith("str") else token
        for token in tokens
    ]
    return "".join(tokens)


def parse_query(q: str) -> ParsedQuery:
    value = q.strip()
    postcode = None
    postcode_match = re.search(r",?\s*(\d{5})(?:\s+berlin)?$", value, re.IGNORECASE)
    if postcode_match:
        postcode = postcode_match.group(1)
        value = value[: postcode_match.start()].rstrip(" ,")
    house_number = None
    suffix = None
    house_match = re.search(r"(?:^|\s)(\d{1,4})\s*([A-Za-z]{1,2})?\s*$", value)
    if house_match:
        house_number = int(house_match.group(1))
        suffix = house_match.group(2).upper() if house_match.group(2) else None
        value = value[: house_match.start()].rstrip(" ,")
    street_key = normalize_street(value)
    if not street_key:
        raise ValueError("Street query must not be empty")
    return ParsedQuery(
        street_key=street_key,
        house_number=house_number,
        house_number_suffix=suffix,
        postcode=postcode,
    )


def _district_key(value: str) -> str:
    return normalize_street(value)


class AddressIndex:
    def __init__(
        self,
        rows: Iterable[tuple[int, Mapping]],
        *,
        record_json=None,
    ):
        self._record_json = record_json
        self._row_records = {}
        self._streets: dict[str, list[tuple]] = {}
        self._district_names: dict[str, str] = {}
        for row_index, row in rows:
            if record_json is None:
                self._row_records[row_index] = row
            street_name = row["street_name"]
            street_key = normalize_street(street_name)
            number = row.get("house_number")
            suffix = row.get("house_number_suffix")
            postcode = row.get("postcode")
            district_code = row.get("district_code")
            district_name = row.get("district_name")
            if district_name:
                self._district_names.setdefault(_district_key(district_name), district_name)
            sort_key = (
                street_name,
                postcode or "",
                number is not None,
                number if number is not None else 0,
                bool(suffix),
                (suffix or "").upper(),
                row["record_id"],
            )
            self._streets.setdefault(street_key, []).append(
                (
                    number,
                    (suffix or "").upper(),
                    postcode,
                    district_code,
                    _district_key(district_name) if district_name else None,
                    sort_key,
                    row_index,
                )
            )
        for entries in self._streets.values():
            entries.sort(key=lambda item: item[5])
        self._street_keys = sorted(self._streets)

    @classmethod
    def from_parquet(cls, path: str | Path) -> AddressIndex:
        table = pq.read_table(path, columns=["record_json"])
        record_json = table.column("record_json")

        def rows():
            for row_index, value in enumerate(record_json):
                yield row_index, json.loads(value.as_py())

        return cls(rows(), record_json=record_json)

    def _record(self, row_index: int) -> Address:
        if self._record_json is not None:
            value = self._record_json[row_index].as_py()
            record = record_from_dict(json.loads(value))
        else:
            record = record_from_dict(self._row_records[row_index])
        if not isinstance(record, Address):
            raise ValueError("Address index snapshot contains a non-address record")
        return record

    def search(
        self,
        parsed: ParsedQuery,
        postcode: str | None = None,
        district: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[int, list[tuple[Address, str]], bool, list[dict[str, str | None]]]:
        if not 0 <= offset or limit < 1:
            raise ValueError("Address search paging values are invalid")
        exact_key = parsed.street_key
        if exact_key in self._streets:
            street_keys = [exact_key]
            used_prefix_fallback = False
        elif len(exact_key) >= 4:
            used_prefix_fallback = True
            position = bisect_left(self._street_keys, exact_key)
            street_keys = []
            while position < len(self._street_keys) and self._street_keys[position].startswith(
                exact_key
            ):
                street_keys.append(self._street_keys[position])
                position += 1
        else:
            street_keys = []
            used_prefix_fallback = False

        entries = [entry for key in street_keys for entry in self._streets[key]]
        effective_postcode = postcode if postcode is not None else parsed.postcode
        if effective_postcode is not None:
            entries = [entry for entry in entries if entry[2] == effective_postcode]
        if district is not None:
            district_key = _district_key(district)
            entries = [
                entry for entry in entries if entry[3] == district or entry[4] == district_key
            ]

        if parsed.house_number is not None:
            numbered = [entry for entry in entries if entry[0] == parsed.house_number]
            if parsed.house_number_suffix is not None:
                entries = [
                    entry for entry in numbered if entry[1] == parsed.house_number_suffix.upper()
                ]
                match_type = "exact"
            else:
                exact = [entry for entry in numbered if not entry[1]]
                if exact:
                    entries = exact
                    match_type = "exact"
                else:
                    entries = [entry for entry in numbered if entry[1]]
                    match_type = "house_number_suffix_variant"
        else:
            match_type = "street_prefix" if used_prefix_fallback else "street"
        entries.sort(key=lambda item: item[5])
        total = len(entries)
        candidate_pairs = sorted(
            {
                (entry[2], self._district_names.get(entry[4]) if entry[4] else None)
                for entry in entries
            },
            key=lambda pair: (pair[0] or "", pair[1] or ""),
        )
        candidates = [
            {"postcode": candidate_postcode, "district_name": district_name}
            for candidate_postcode, district_name in candidate_pairs[:20]
        ]
        ambiguous = total > 1 and match_type in {
            "exact",
            "house_number_suffix_variant",
        }
        page = entries[offset : offset + limit]
        return (
            total,
            [(self._record(entry[6]), match_type) for entry in page],
            ambiguous,
            candidates,
        )
