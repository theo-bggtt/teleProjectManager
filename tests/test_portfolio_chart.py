"""Tests for tgbot.trading.portfolio.render_chart and load_snapshots_for_period."""
from datetime import datetime, timedelta, timezone

import pytest

from tgbot.trading.db import TradingDB
from tgbot.trading.portfolio import load_snapshots_for_period, render_chart


@pytest.fixture
def tdb(tmp_path):
    return TradingDB(tmp_path / "trading.db")


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(tdb: TradingDB, days_ago: int, total: float, ko: int = 0):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    tdb.add_snapshot(taken_at=_iso(when), total_usd=total,
                     wallets_ok=1, wallets_ko=ko, raw_json="{}")


def test_load_snapshots_period_30d_filters(tdb):
    _seed(tdb, 60, 100.0)  # outside window
    _seed(tdb, 20, 200.0)  # inside
    _seed(tdb, 5,  300.0)  # inside
    snaps = load_snapshots_for_period(tdb, "30d")
    assert [s["total_usd"] for s in snaps] == [200.0, 300.0]


def test_load_snapshots_period_all(tdb):
    _seed(tdb, 60, 100.0)
    _seed(tdb, 5,  300.0)
    snaps = load_snapshots_for_period(tdb, "all")
    assert len(snaps) == 2


def test_render_chart_empty_returns_png_bytes(tdb):
    png = render_chart([], period_days=30)
    assert isinstance(png, bytes) and len(png) > 1_000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_chart_single_point(tdb):
    _seed(tdb, 1, 100.0)
    snaps = tdb.list_snapshots()
    png = render_chart(snaps, period_days=30)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_chart_multiple_points(tdb):
    for d, v in [(7, 100.0), (5, 120.0), (3, 110.0), (0, 150.0)]:
        _seed(tdb, d, v)
    snaps = tdb.list_snapshots()
    png = render_chart(snaps, period_days=7)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5_000  # real chart, not the placeholder


def test_render_chart_partial_snapshot_flag(tdb):
    """Snapshots with wallets_ko > 0 on the last point should still render."""
    _seed(tdb, 1, 100.0, ko=2)
    snaps = tdb.list_snapshots()
    png = render_chart(snaps, period_days=30)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
