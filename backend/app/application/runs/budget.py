"""Persistent admission budget for a public review deployment."""

import sqlite3
from pathlib import Path


class PublicRunBudget:
    def __init__(self, path: Path, maximum: int):
        self.path = path
        self.maximum = maximum
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS admissions (run_id TEXT PRIMARY KEY)")

    def admit(self, run_id: str) -> bool:
        with sqlite3.connect(self.path, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admissions WHERE run_id = ?", (run_id,)).fetchone():
                db.commit()
                return True
            count = db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
            if count >= self.maximum:
                db.rollback()
                return False
            db.execute("INSERT INTO admissions VALUES (?)", (run_id,))
            db.commit()
            return True
