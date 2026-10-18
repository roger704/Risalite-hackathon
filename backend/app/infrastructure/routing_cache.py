"""Persistent cache for graph- and profile-versioned routing results."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def default_routing_cache_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "routing" / "cache.sqlite"


class RoutingCache:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS routing_cache (
                    cache_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def make_key(
        kind: str,
        graph_version: str,
        profile_hash: str,
        coordinates: list[list[float]],
        minutes: list[int | float] | None = None,
    ) -> str:
        payload = {
            "kind": kind,
            "graph_version": graph_version,
            "profile_hash": profile_hash,
            "coordinates": [[round(value, 6) for value in pair] for pair in coordinates],
            "minutes": minutes,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def get(
        self,
        kind: str,
        graph_version: str,
        profile_hash: str,
        coordinates: list[list[float]],
        minutes: list[int | float] | None = None,
    ) -> dict[str, Any] | None:
        key = self.make_key(kind, graph_version, profile_hash, coordinates, minutes)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM routing_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        result["cache_hit"] = True
        return result

    def put(
        self,
        kind: str,
        graph_version: str,
        profile_hash: str,
        coordinates: list[list[float]],
        result: dict[str, Any],
        created_at: str,
        minutes: list[int | float] | None = None,
    ) -> None:
        key = self.make_key(kind, graph_version, profile_hash, coordinates, minutes)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO routing_cache
                    (cache_key, kind, graph_version, profile_hash, created_at, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    kind,
                    graph_version,
                    profile_hash,
                    created_at,
                    json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False),
                ),
            )
