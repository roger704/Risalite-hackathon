"""SQLite manifests and jobs; publication switches the active pointer transactionally."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.domain import NotFoundError, manifest_from_dict, manifest_to_dict


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, manifest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS snapshot_cache ON snapshots(dataset_id, fingerprint);
                CREATE TABLE IF NOT EXISTS active (
                    dataset_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT, error_code TEXT, message TEXT, snapshot_id TEXT
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin_job(self, job_id: str, dataset_id: str):
        with self.connect() as connection:
            # Caller holds this dataset's process lock, so prior running jobs are interrupted.
            connection.execute(
                "UPDATE jobs SET status='aborted', finished_at=CURRENT_TIMESTAMP "
                "WHERE dataset_id=? AND status IN ('queued', 'running')",
                (dataset_id,),
            )
            connection.execute(
                "INSERT INTO jobs(job_id,dataset_id,status) VALUES(?,?,'running')",
                (job_id, dataset_id),
            )

    def finish_job(self, job_id: str, status: str, *, snapshot_id=None, error=None):
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=CURRENT_TIMESTAMP, snapshot_id=?, "
                "error_code=?, message=? WHERE job_id=?",
                (
                    status,
                    snapshot_id,
                    getattr(error, "code", "INTERNAL_ERROR") if error else None,
                    str(error)[:1000] if error else None,
                    job_id,
                ),
            )

    def publish(self, manifest, job_id: str):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?)",
                (
                    manifest.snapshot_id,
                    manifest.dataset_id,
                    manifest.request_fingerprint,
                    json.dumps(manifest_to_dict(manifest), sort_keys=True),
                ),
            )
            connection.execute(
                "INSERT INTO active VALUES(?,?) ON CONFLICT(dataset_id) "
                "DO UPDATE SET snapshot_id=excluded.snapshot_id",
                (manifest.dataset_id, manifest.snapshot_id),
            )
            self._complete_job(connection, job_id, manifest.snapshot_id)

    @staticmethod
    def _complete_job(connection, job_id: str, snapshot_id: str):
        connection.execute(
            "UPDATE jobs SET status='succeeded', finished_at=CURRENT_TIMESTAMP, "
            "snapshot_id=?, error_code=NULL, message=NULL WHERE job_id=?",
            (snapshot_id, job_id),
        )

    def job_succeeded(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row is not None and row["status"] == "succeeded"

    def get(self, dataset_id: str, snapshot_id: str | None = None):
        with self.connect() as connection:
            if snapshot_id is None:
                row = connection.execute(
                    "SELECT s.manifest FROM snapshots s JOIN active a USING(snapshot_id) "
                    "WHERE a.dataset_id=?",
                    (dataset_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT manifest FROM snapshots WHERE dataset_id=? AND snapshot_id=?",
                    (dataset_id, snapshot_id),
                ).fetchone()
        if row is None:
            raise NotFoundError(f"No published snapshot for {dataset_id}")
        return manifest_from_dict(json.loads(row["manifest"]))

    def cached(self, dataset_id: str, fingerprint: str):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT manifest FROM snapshots WHERE dataset_id=? AND fingerprint=? "
                "ORDER BY rowid DESC LIMIT 1",
                (dataset_id, fingerprint),
            ).fetchone()
        return manifest_from_dict(json.loads(row["manifest"])) if row else None

    def activate(self, manifest, job_id: str):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO active VALUES(?,?) ON CONFLICT(dataset_id) "
                "DO UPDATE SET snapshot_id=excluded.snapshot_id",
                (manifest.dataset_id, manifest.snapshot_id),
            )
            self._complete_job(connection, job_id, manifest.snapshot_id)
