"""SQLite store for named shell macros.

A macro is a saved shell command (or sequence) with an optional working
directory. The user creates, runs, edits and deletes macros via Telegram
wizards from the admin menu.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS shell_macros (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    command      TEXT NOT NULL,
    cwd          TEXT,
    created_at   TEXT NOT NULL,
    last_run_at  TEXT
);
"""


@dataclass
class Macro:
    id: int
    name: str
    command: str
    cwd: Optional[str]
    created_at: str
    last_run_at: Optional[str]


# Sentinel used by `update()` to distinguish "do not change" from "set to None".
_UNSET: Any = object()


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_CMD_MAX = 4000


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def is_valid_command(cmd: str) -> bool:
    return 0 < len(cmd) <= _CMD_MAX


class MacrosDB:
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

    def add(self, *, name: str, command: str, cwd: Optional[str]) -> Optional[int]:
        """Insert a new macro. Returns the new id, or None if `name` is taken."""
        now = _utcnow()
        try:
            with self._conn() as c:
                cur = c.execute(
                    "INSERT INTO shell_macros (name, command, cwd, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, command, cwd, now),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get(self, macro_id: int) -> Optional[Macro]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM shell_macros WHERE id = ?", (macro_id,),
            ).fetchone()
            return _row_to_macro(row) if row else None

    def get_by_name(self, name: str) -> Optional[Macro]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM shell_macros WHERE name = ?", (name,),
            ).fetchone()
            return _row_to_macro(row) if row else None

    def list(self) -> list[Macro]:
        """Most recently executed first; never-executed fall back to created_at desc."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM shell_macros "
                "ORDER BY last_run_at IS NULL, last_run_at DESC, created_at DESC"
            ).fetchall()
            return [_row_to_macro(r) for r in rows]

    def update(self, macro_id: int, *,
               name: Any = _UNSET,
               command: Any = _UNSET,
               cwd: Any = _UNSET) -> bool:
        """Update a subset of fields. Returns False if no row updated (unknown id)
        or on UNIQUE constraint violation (name collision)."""
        sets: list[str] = []
        values: list[Any] = []
        if name is not _UNSET:
            sets.append("name = ?")
            values.append(name)
        if command is not _UNSET:
            sets.append("command = ?")
            values.append(command)
        if cwd is not _UNSET:
            sets.append("cwd = ?")
            values.append(cwd)

        if not sets:
            return False

        values.append(macro_id)
        sql = f"UPDATE shell_macros SET {', '.join(sets)} WHERE id = ?"
        try:
            with self._conn() as c:
                cur = c.execute(sql, values)
                return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def touch(self, macro_id: int) -> None:
        """Update last_run_at to now. No-op if id is unknown."""
        with self._conn() as c:
            c.execute(
                "UPDATE shell_macros SET last_run_at = ? WHERE id = ?",
                (_utcnow(), macro_id),
            )

    def delete(self, macro_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM shell_macros WHERE id = ?", (macro_id,))
            return cur.rowcount > 0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_macro(row: sqlite3.Row) -> Macro:
    return Macro(
        id=row["id"],
        name=row["name"],
        command=row["command"],
        cwd=row["cwd"],
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
    )
