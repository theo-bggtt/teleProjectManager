"""Tests for portfolio_snapshots table in TradingDB."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tgbot.trading.db import TradingDB


@pytest.fixture
def tdb(tmp_path):
    return TradingDB(tmp_path / "trading.db")


def test_portfolio_snapshots_table_exists(tdb):
    conn = sqlite3.connect(tdb.db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    assert "portfolio_snapshots" in names


def test_add_snapshot_returns_id(tdb):
    sid = tdb.add_snapshot(
        taken_at="2026-05-18T08:00:00Z",
        total_usd=12345.67,
        wallets_ok=3,
        wallets_ko=0,
        raw_json=json.dumps({"foo": "bar"}),
    )
    assert isinstance(sid, int) and sid > 0


def test_list_snapshots_empty(tdb):
    assert tdb.list_snapshots() == []


def test_list_snapshots_orders_by_taken_at(tdb):
    tdb.add_snapshot(taken_at="2026-05-18T08:00:00Z", total_usd=100.0,
                     wallets_ok=1, wallets_ko=0, raw_json="{}")
    tdb.add_snapshot(taken_at="2026-05-17T08:00:00Z", total_usd=90.0,
                     wallets_ok=1, wallets_ko=0, raw_json="{}")
    rows = tdb.list_snapshots()
    assert [r["taken_at"] for r in rows] == [
        "2026-05-17T08:00:00Z", "2026-05-18T08:00:00Z",
    ]


def test_list_snapshots_filters_by_since(tdb):
    tdb.add_snapshot(taken_at="2026-04-01T08:00:00Z", total_usd=50.0,
                     wallets_ok=1, wallets_ko=0, raw_json="{}")
    tdb.add_snapshot(taken_at="2026-05-15T08:00:00Z", total_usd=80.0,
                     wallets_ok=1, wallets_ko=0, raw_json="{}")
    tdb.add_snapshot(taken_at="2026-05-18T08:00:00Z", total_usd=100.0,
                     wallets_ok=1, wallets_ko=0, raw_json="{}")
    rows = tdb.list_snapshots(since="2026-05-01T00:00:00Z")
    assert [r["total_usd"] for r in rows] == [80.0, 100.0]


def test_list_snapshots_raw_json_passthrough(tdb):
    tdb.add_snapshot(taken_at="2026-05-18T08:00:00Z", total_usd=100.0,
                     wallets_ok=1, wallets_ko=0,
                     raw_json=json.dumps({"wallet": "abc", "usd": 100.0}))
    rows = tdb.list_snapshots()
    assert json.loads(rows[0]["raw_json"]) == {"wallet": "abc", "usd": 100.0}
