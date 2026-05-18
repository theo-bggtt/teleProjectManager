"""SQLite store for the trading module.

Lives in its own file (``data/trading.db``) so the trading domain does
not share schema or migrations with the existing project DB. Pattern
follows ``tgbot/db.py``: ``DB`` class + ``@contextmanager _conn``.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address    TEXT NOT NULL,
    chain      TEXT NOT NULL,
    label      TEXT,
    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (address, chain)
);

CREATE TABLE IF NOT EXISTS alerts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address      TEXT NOT NULL,
    chain              TEXT NOT NULL,
    mc_target          REAL NOT NULL,
    direction          TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    persistent         INTEGER NOT NULL DEFAULT 0,
    cooldown_min       INTEGER NOT NULL DEFAULT 60,
    last_triggered_at  TIMESTAMP,
    label              TEXT,
    armed              INTEGER NOT NULL DEFAULT 1,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_armed ON alerts(armed);

CREATE TABLE IF NOT EXISTS seen_tx (
    chain        TEXT NOT NULL,
    sig_or_hash  TEXT NOT NULL,
    seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chain, sig_or_hash)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT    NOT NULL,
    total_usd   REAL    NOT NULL,
    wallets_ok  INTEGER NOT NULL,
    wallets_ko  INTEGER NOT NULL,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON portfolio_snapshots(taken_at);
"""


class TradingDB:
    """SQLite-backed store for wallets, MC alerts, and tx deduplication."""

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

    # ── wallets ────────────────────────────────────────────────────────
    def add_wallet(self, address: str, chain: str, label: Optional[str] = None) -> bool:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO wallets (address, chain, label) VALUES (?, ?, ?)",
                    (address, chain, label),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_wallet(self, address: str, chain: Optional[str] = None) -> int:
        with self._conn() as c:
            if chain is None:
                cur = c.execute("DELETE FROM wallets WHERE address = ?", (address,))
            else:
                cur = c.execute(
                    "DELETE FROM wallets WHERE address = ? AND chain = ?",
                    (address, chain),
                )
            return cur.rowcount

    def list_wallets(self, chain: Optional[str] = None) -> list[dict]:
        with self._conn() as c:
            if chain is None:
                rows = c.execute(
                    "SELECT * FROM wallets ORDER BY chain, added_at"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM wallets WHERE chain = ? ORDER BY added_at",
                    (chain,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ── alerts ─────────────────────────────────────────────────────────
    def add_alert(
        self,
        token_address: str,
        chain: str,
        mc_target: float,
        direction: str = "above",
        persistent: bool = False,
        cooldown_min: int = 60,
        label: Optional[str] = None,
    ) -> int:
        if direction not in ("above", "below"):
            raise ValueError(f"direction must be 'above' or 'below', got {direction!r}")
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO alerts
                  (token_address, chain, mc_target, direction, persistent, cooldown_min, label)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_address, chain, mc_target, direction,
                 1 if persistent else 0, cooldown_min, label),
            )
            return cur.lastrowid

    def remove_alert(self, alert_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
            return cur.rowcount > 0

    def list_alerts(self, armed_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM alerts"
        if armed_only:
            sql += " WHERE armed = 1"
        sql += " ORDER BY created_at"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def mark_alert_triggered(self, alert_id: int, disarm: bool) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE alerts
                   SET last_triggered_at = CURRENT_TIMESTAMP,
                       armed = ?
                 WHERE id = ?
                """,
                (0 if disarm else 1, alert_id),
            )

    # ── seen_tx (dedup) ────────────────────────────────────────────────
    def mark_seen(self, chain: str, sig_or_hash: str) -> bool:
        """Return True if newly inserted, False if it was already known."""
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO seen_tx (chain, sig_or_hash) VALUES (?, ?)",
                    (chain, sig_or_hash),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def prune_seen(self, keep_last_days: int = 7) -> int:
        with self._conn() as c:
            cur = c.execute(
                f"DELETE FROM seen_tx WHERE seen_at < datetime('now', '-{int(keep_last_days)} days')"
            )
            return cur.rowcount

    # ── portfolio snapshots ────────────────────────────────────────────
    def add_snapshot(
        self,
        *,
        taken_at: str,
        total_usd: float,
        wallets_ok: int,
        wallets_ko: int,
        raw_json: Optional[str] = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO portfolio_snapshots
                  (taken_at, total_usd, wallets_ok, wallets_ko, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (taken_at, total_usd, wallets_ok, wallets_ko, raw_json),
            )
            return cur.lastrowid

    def list_snapshots(self, since: Optional[str] = None) -> list[dict]:
        """Return snapshots ordered by taken_at ASC. If `since` is given
        (ISO 8601 UTC string), only snapshots with taken_at >= since."""
        with self._conn() as c:
            if since is None:
                rows = c.execute(
                    "SELECT * FROM portfolio_snapshots ORDER BY taken_at ASC"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM portfolio_snapshots "
                    "WHERE taken_at >= ? ORDER BY taken_at ASC",
                    (since,),
                ).fetchall()
            return [dict(r) for r in rows]
