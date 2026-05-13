"""SQLite store for scheduled tasks and bot settings."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    target       TEXT NOT NULL,
    operation    TEXT,
    trigger_kind TEXT NOT NULL,
    trigger_spec TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class SchedulerDB:
    """Thin wrapper over the shared `projects.db` for scheduler-specific tables.

    Uses idempotent CREATE TABLE IF NOT EXISTS so it can coexist with the
    main `DB` class without ordering constraints.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            row = c.execute(
                "SELECT value FROM bot_settings WHERE key = 'notifications_enabled'"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO bot_settings (key, value) VALUES ('notifications_enabled', '1')"
                )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ─── bot_settings ────────────────────────────────────────────────────
    def get_notifications_enabled(self) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM bot_settings WHERE key = 'notifications_enabled'"
            ).fetchone()
            return row is not None and row["value"] == "1"

    def set_notifications_enabled(self, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('notifications_enabled', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if enabled else "0",),
            )

    # ─── scheduled_tasks ─────────────────────────────────────────────────
    def add_task(self, *, name: str, task_type: str, target: str,
                 operation: Optional[str], trigger_kind: str,
                 trigger_spec: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO scheduled_tasks "
                "(name, task_type, target, operation, trigger_kind, trigger_spec, "
                " enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (name, task_type, target, operation,
                 trigger_kind, json.dumps(trigger_spec), now),
            )
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def list_tasks(self, *, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM scheduled_tasks"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._conn() as c:
            rows = c.execute(sql).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def set_enabled(self, task_id: int, enabled: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE scheduled_tasks SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, task_id),
            )
            return cur.rowcount > 0

    def set_last_run(self, task_id: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, last_status = ? WHERE id = ?",
                (now, status, task_id),
            )

    def update_trigger(self, task_id: int, trigger_kind: str, trigger_spec: dict) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE scheduled_tasks SET trigger_kind = ?, trigger_spec = ? WHERE id = ?",
                (trigger_kind, json.dumps(trigger_spec), task_id),
            )
            return cur.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["trigger_spec"] = json.loads(d["trigger_spec"])
        return d
