"""SQLite project store."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name           TEXT PRIMARY KEY,
    path           TEXT NOT NULL,
    start_command  TEXT,
    entry_file     TEXT,
    env_vars       TEXT DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class DB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_project(self, name: str, path: str) -> bool:
        try:
            with self._conn() as c:
                c.execute("INSERT INTO projects (name, path) VALUES (?, ?)", (name, path))
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_project(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM projects WHERE name = ?", (name,))
            return cur.rowcount > 0

    def get_project(self, name: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM projects ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def update_project(self, name: str, **fields) -> bool:
        if not fields:
            return False
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        with self._conn() as c:
            cur = c.execute(f"UPDATE projects SET {cols} WHERE name = ?", values)
            return cur.rowcount > 0
