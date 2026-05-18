# Portfolio Snapshots + Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily snapshot of aggregated USD portfolio value across watched wallets, exposed in the Trading menu as a "📊 Portfolio" button that renders a 30-day chart (with 7j/30j/90j/all toggles).

**Architecture:**
- New module `tgbot/trading/portfolio.py` holds business logic (snapshot, history load, chart render).
- New SQLite table `portfolio_snapshots` in the existing `trading.db` (via `TradingDB` extension).
- New APScheduler job registered at startup in `register_trading()`, fires daily at 08:00 Europe/Paris and calls `take_snapshot()`.
- New inline button + callback handlers extend `tgbot/trading/handlers.py`.

**Tech Stack:** Python 3.12+, python-telegram-bot v21, APScheduler v3, matplotlib (new dep), sqlite3 (stdlib), pytest + pytest-asyncio.

**Spec reference:** `docs/superpowers/specs/2026-05-18-portfolio-snapshots-design.md`

---

## File Structure

- **Create** `tgbot/trading/portfolio.py` — pure logic: `Snapshot` dataclass, `take_snapshot()`, `load_history()`, `render_chart()`.
- **Modify** `tgbot/trading/db.py` — add `portfolio_snapshots` table + `add_snapshot()` / `list_snapshots(since=None)`.
- **Modify** `tgbot/trading/__init__.py` — register the daily APScheduler job in `register_trading()`.
- **Modify** `tgbot/trading/handlers.py` — new button + callbacks `trd:portfolio[:period|:force]`.
- **Modify** `requirements.txt` — add `matplotlib>=3.7`.
- **Create** `tests/test_portfolio_db.py` — DB roundtrips.
- **Create** `tests/test_portfolio_snapshot.py` — `take_snapshot()` with mocked fetchers.
- **Create** `tests/test_portfolio_chart.py` — `render_chart()` returns non-empty PNG bytes.

---

## Task 1: Add matplotlib dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add matplotlib to requirements.txt**

Add this line in the dev/optional section (after the Trading module block, before "Dev (tests)"):

```
# Portfolio chart rendering:
matplotlib>=3.7
```

- [ ] **Step 2: Install in venv**

Run: `python -m pip install -r requirements.txt`
Expected: matplotlib installs without error.

- [ ] **Step 3: Verify import**

Run: `python -c "import matplotlib; print(matplotlib.__version__)"`
Expected: prints a version ≥ 3.7.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat(portfolio): add matplotlib dependency for chart rendering"
```

---

## Task 2: Extend `TradingDB` with `portfolio_snapshots` table

**Files:**
- Modify: `tgbot/trading/db.py` (SCHEMA constant + new methods)
- Test: `tests/test_portfolio_db.py` (new file)

- [ ] **Step 1: Write the failing test for table creation**

Create `tests/test_portfolio_db.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_portfolio_db.py -v`
Expected: 5 FAILs — `AttributeError: 'TradingDB' object has no attribute 'add_snapshot'` and similar.

- [ ] **Step 3: Extend SCHEMA in `tgbot/trading/db.py`**

Append to the `SCHEMA` constant (after the `seen_tx` block, before the closing `"""`):

```sql

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT    NOT NULL,
    total_usd   REAL    NOT NULL,
    wallets_ok  INTEGER NOT NULL,
    wallets_ko  INTEGER NOT NULL,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON portfolio_snapshots(taken_at);
```

- [ ] **Step 4: Add methods to `TradingDB`**

Append inside the `TradingDB` class in `tgbot/trading/db.py` (after `prune_seen` method, with the corresponding section header comment):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_portfolio_db.py -v`
Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add tgbot/trading/db.py tests/test_portfolio_db.py
git commit -m "feat(portfolio): add portfolio_snapshots table + DB methods"
```

---

## Task 3: Implement `take_snapshot()` with mocked fetchers

**Files:**
- Create: `tgbot/trading/portfolio.py`
- Test: `tests/test_portfolio_snapshot.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_portfolio_snapshot.py`:

```python
"""Tests for tgbot.trading.portfolio.take_snapshot."""
import json
from unittest.mock import AsyncMock

import pytest

from tgbot.trading.db import TradingDB
from tgbot.trading.portfolio import take_snapshot


@pytest.fixture
def tdb(tmp_path):
    return TradingDB(tmp_path / "trading.db")


class _Holding:
    """Minimal stand-in for trading.solana/evm Holding objects."""
    def __init__(self, value_usd):
        self.value_usd = value_usd


@pytest.mark.asyncio
async def test_take_snapshot_no_wallets_writes_zero(tdb):
    fetch_sol = AsyncMock()
    fetch_evm = AsyncMock()
    result = await take_snapshot(
        tdb, helius_key="x", alchemy_key="y", price_client=None,
        fetch_sol=fetch_sol, fetch_evm=fetch_evm,
    )
    assert result is not None
    assert result["total_usd"] == 0.0
    assert result["wallets_ok"] == 0
    rows = tdb.list_snapshots()
    assert len(rows) == 1
    assert rows[0]["total_usd"] == 0.0


@pytest.mark.asyncio
async def test_take_snapshot_aggregates_usd(tdb):
    tdb.add_wallet("sol_addr", "sol", "main")
    tdb.add_wallet("0xeth", "eth", "side")
    fetch_sol = AsyncMock(return_value=([_Holding(100.0), _Holding(50.0)], None))
    fetch_evm = AsyncMock(return_value=([_Holding(200.0)], None))
    result = await take_snapshot(
        tdb, helius_key="x", alchemy_key="y", price_client=None,
        fetch_sol=fetch_sol, fetch_evm=fetch_evm,
    )
    assert result["total_usd"] == 350.0
    assert result["wallets_ok"] == 2
    assert result["wallets_ko"] == 0
    raw = json.loads(tdb.list_snapshots()[0]["raw_json"])
    assert "sol_addr" in raw and "0xeth" in raw


@pytest.mark.asyncio
async def test_take_snapshot_partial_failure(tdb):
    tdb.add_wallet("sol_addr", "sol", None)
    tdb.add_wallet("0xeth", "eth", None)
    fetch_sol = AsyncMock(return_value=([_Holding(100.0)], None))
    fetch_evm = AsyncMock(side_effect=RuntimeError("alchemy down"))
    result = await take_snapshot(
        tdb, helius_key="x", alchemy_key="y", price_client=None,
        fetch_sol=fetch_sol, fetch_evm=fetch_evm,
    )
    assert result["total_usd"] == 100.0
    assert result["wallets_ok"] == 1
    assert result["wallets_ko"] == 1


@pytest.mark.asyncio
async def test_take_snapshot_all_failures_writes_nothing(tdb):
    tdb.add_wallet("sol_addr", "sol", None)
    tdb.add_wallet("0xeth", "eth", None)
    fetch_sol = AsyncMock(side_effect=RuntimeError("helius down"))
    fetch_evm = AsyncMock(side_effect=RuntimeError("alchemy down"))
    result = await take_snapshot(
        tdb, helius_key="x", alchemy_key="y", price_client=None,
        fetch_sol=fetch_sol, fetch_evm=fetch_evm,
    )
    assert result is None
    assert tdb.list_snapshots() == []


@pytest.mark.asyncio
async def test_take_snapshot_holdings_with_none_value(tdb):
    """A holding with value_usd=None must not crash the aggregation."""
    tdb.add_wallet("sol_addr", "sol", None)
    fetch_sol = AsyncMock(return_value=([_Holding(None), _Holding(50.0)], None))
    fetch_evm = AsyncMock()
    result = await take_snapshot(
        tdb, helius_key="x", alchemy_key="y", price_client=None,
        fetch_sol=fetch_sol, fetch_evm=fetch_evm,
    )
    assert result["total_usd"] == 50.0
    assert result["wallets_ok"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_portfolio_snapshot.py -v`
Expected: 5 FAILs — `ModuleNotFoundError: No module named 'tgbot.trading.portfolio'`.

- [ ] **Step 3: Create `tgbot/trading/portfolio.py`**

```python
"""Portfolio snapshots: aggregate USD across watched wallets, store history,
render a chart of evolution.

The module is dependency-injectable: fetchers are passed in (defaults wire
to the real on-chain helpers) so tests can swap in mocks without monkey-
patching.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .db import TradingDB

logger = logging.getLogger(__name__)


# Concurrency cap when fetching multiple wallets — protects Helius/Alchemy.
_FETCH_CONCURRENCY = 4


@dataclass
class Snapshot:
    """In-memory view of a portfolio_snapshots row."""
    taken_at: str
    total_usd: float
    wallets_ok: int
    wallets_ko: int


FetchSolFn = Callable[..., Awaitable[tuple[list[Any], Any]]]
FetchEvmFn = Callable[..., Awaitable[tuple[list[Any], Any]]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def take_snapshot(
    db: TradingDB,
    *,
    helius_key: Optional[str],
    alchemy_key: Optional[str],
    price_client: Any,
    fetch_sol: Optional[FetchSolFn] = None,
    fetch_evm: Optional[FetchEvmFn] = None,
) -> Optional[dict]:
    """Fetch holdings of every watched wallet, aggregate USD, persist a row.

    Returns the inserted row as a dict on success (including zero-wallets case),
    or None if every wallet failed (avoids writing a misleading $0 total).
    """
    # Late imports keep optional aiohttp deps out of test-only paths.
    if fetch_sol is None:
        from .solana import fetch_solana_holdings as fetch_sol  # type: ignore
    if fetch_evm is None:
        from .evm import fetch_evm_holdings as fetch_evm  # type: ignore

    wallets = db.list_wallets()
    if not wallets:
        snapshot_row = {
            "taken_at": _utc_now_iso(),
            "total_usd": 0.0,
            "wallets_ok": 0,
            "wallets_ko": 0,
        }
        db.add_snapshot(
            taken_at=snapshot_row["taken_at"],
            total_usd=0.0, wallets_ok=0, wallets_ko=0,
            raw_json=json.dumps({}),
        )
        logger.info("portfolio snapshot: no wallets, wrote zero row")
        return snapshot_row

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
    detail: dict[str, dict] = {}
    ok = 0
    ko = 0
    total_usd = 0.0

    async def _one(w: dict) -> None:
        nonlocal ok, ko, total_usd
        chain = w["chain"]
        addr = w["address"]
        async with sem:
            try:
                if chain == "sol":
                    holdings, _ = await fetch_sol(helius_key, addr)
                else:
                    holdings, _ = await fetch_evm(
                        chain, alchemy_key, addr, price_client=price_client,
                    )
            except Exception as e:  # noqa: BLE001 — log + count, do not raise
                logger.warning(
                    "portfolio snapshot wallet failed: %s/%s — %s: %s",
                    chain, addr, type(e).__name__, e,
                )
                ko += 1
                return
        w_total = sum(float(h.value_usd or 0.0) for h in holdings)
        total_usd += w_total
        detail[addr] = {"chain": chain, "total_usd": w_total}
        ok += 1

    await asyncio.gather(*(_one(w) for w in wallets))

    if ok == 0:
        logger.error("portfolio snapshot fully failed (%d wallets KO), skipping write", ko)
        return None

    taken_at = _utc_now_iso()
    db.add_snapshot(
        taken_at=taken_at,
        total_usd=total_usd,
        wallets_ok=ok,
        wallets_ko=ko,
        raw_json=json.dumps(detail),
    )
    logger.info(
        "portfolio snapshot ok: total=$%.2f wallets_ok=%d wallets_ko=%d",
        total_usd, ok, ko,
    )
    return {
        "taken_at": taken_at,
        "total_usd": total_usd,
        "wallets_ok": ok,
        "wallets_ko": ko,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_portfolio_snapshot.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add tgbot/trading/portfolio.py tests/test_portfolio_snapshot.py
git commit -m "feat(portfolio): take_snapshot() aggregates wallet USD with partial-failure tolerance"
```

---

## Task 4: Implement `render_chart()` + history filtering

**Files:**
- Modify: `tgbot/trading/portfolio.py` (add `load_snapshots_for_period` + `render_chart`)
- Test: `tests/test_portfolio_chart.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_portfolio_chart.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_portfolio_chart.py -v`
Expected: 6 FAILs — `ImportError: cannot import name 'load_snapshots_for_period'`.

- [ ] **Step 3: Append `load_snapshots_for_period` and `render_chart` to `tgbot/trading/portfolio.py`**

Add at the bottom of `portfolio.py`:

```python
from datetime import timedelta
from io import BytesIO


# Period code → (label, days). "all" means no lower bound.
_PERIODS: dict[str, tuple[str, Optional[int]]] = {
    "7d":  ("7 derniers jours",   7),
    "30d": ("30 derniers jours",  30),
    "90d": ("90 derniers jours",  90),
    "all": ("Depuis le début",    None),
}


def load_snapshots_for_period(db: TradingDB, period: str) -> list[dict]:
    """Return snapshots for the requested period code ('7d'/'30d'/'90d'/'all')."""
    if period not in _PERIODS:
        raise ValueError(f"Unknown period: {period!r}")
    _, days = _PERIODS[period]
    if days is None:
        return db.list_snapshots()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return db.list_snapshots(since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _fmt_usd(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def render_chart(snapshots: list[dict], period_days: int) -> bytes:
    """Render a PNG chart of total_usd over time. Returns raw PNG bytes.

    Uses matplotlib's Agg backend (no GUI). Safe to call from async via
    asyncio.to_thread.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 5), dpi=100)

    if not snapshots:
        ax.text(0.5, 0.5, "Pas encore de données",
                ha="center", va="center", fontsize=16,
                transform=ax.transAxes, color="#bbbbbb")
        ax.set_xticks([])
        ax.set_yticks([])
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    xs = [datetime.strptime(s["taken_at"], "%Y-%m-%dT%H:%M:%SZ") for s in snapshots]
    ys = [float(s["total_usd"]) for s in snapshots]

    ax.plot(xs, ys, marker="o", linewidth=2, color="#4dd0e1")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.set_title(f"Portfolio — {period_days}j" if period_days else "Portfolio")

    # Y axis: $K / $M formatting
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_usd(v)))
    # X axis: short date
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate(rotation=0)

    # Annotations top-right
    last = ys[-1]
    ref = ys[0]
    pct = ((last / ref) - 1.0) * 100.0 if ref > 0 else 0.0
    ath = max(ys)
    ath_when = xs[ys.index(ath)]
    pct_color = "#4caf50" if pct >= 0 else "#ef5350"
    sign = "+" if pct >= 0 else ""
    ax.text(
        0.99, 0.97,
        f"Actuel : {_fmt_usd(last)}\n"
        f"{period_days}j : {sign}{pct:.1f}%\n"
        f"ATH : {_fmt_usd(ath)} ({ath_when.strftime('%d %b')})",
        transform=ax.transAxes, ha="right", va="top",
        fontsize=10, color="#eeeeee",
        bbox=dict(facecolor="#222222", edgecolor=pct_color, alpha=0.85),
    )

    # Partial-snapshot warning footer if the last point had failures
    if snapshots[-1].get("wallets_ko", 0) > 0:
        ax.text(
            0.01, -0.18,
            f"⚠ Dernier snapshot partiel ({snapshots[-1]['wallets_ok']} OK / "
            f"{snapshots[-1]['wallets_ko']} KO)",
            transform=ax.transAxes, ha="left", va="top",
            fontsize=8, color="#ffb74d",
        )

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_portfolio_chart.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add tgbot/trading/portfolio.py tests/test_portfolio_chart.py
git commit -m "feat(portfolio): render_chart() with dark theme + period filtering"
```

---

## Task 5: Register the daily APScheduler job at startup

**Files:**
- Modify: `tgbot/trading/__init__.py`

- [ ] **Step 1: Add the job registration after `install_monitor_lifecycle`**

In `tgbot/trading/__init__.py`, replace the body of `register_trading` (everything below the `if cfg.trading is None or not cfg.trading.enabled:` early return) with:

```python
    # Local imports: optional deps (aiohttp/websockets) only loaded when enabled.
    from apscheduler.triggers.cron import CronTrigger

    from .db import TradingDB
    from .handlers import register_handlers
    from .monitor import TradingMonitor, install_monitor_lifecycle
    from .portfolio import take_snapshot
    from .prices import PriceClient

    db = TradingDB(cfg.data_dir / "trading.db")
    price_client = PriceClient()
    monitor = TradingMonitor(app, cfg, db, price_client)

    register_handlers(
        app, cfg, db, monitor,
        wizard_step=wizard_step,
        wizard_finish=wizard_finish,
        wizard_escape=wizard_escape,
    )
    install_monitor_lifecycle(app, monitor)

    # Daily portfolio snapshot at 08:00 Europe/Paris.
    async def _portfolio_job() -> None:
        try:
            await take_snapshot(
                db,
                helius_key=cfg.trading.helius_api_key,
                alchemy_key=cfg.trading.alchemy_api_key,
                price_client=price_client,
            )
        except Exception:  # noqa: BLE001 — must not kill the scheduler
            logger.exception("daily portfolio snapshot raised")

    app.job_queue.scheduler.add_job(
        _portfolio_job,
        trigger=CronTrigger(hour=8, minute=0, timezone="Europe/Paris"),
        id="trading:portfolio_snapshot_daily",
        misfire_grace_time=3600,  # if bot was down at 08:00, still fire within the hour
        max_instances=1,
        replace_existing=True,
    )

    logger.info("Trading module registered (chains: sol + %s).",
                ", ".join(cfg.trading.evm_chains))
```

- [ ] **Step 2: Sanity check — bot starts without error**

Run: `python -m tgbot --help` (or whatever lightest invocation prints config without polling).
Alternatively, just check syntax: `python -c "import tgbot.trading"`
Expected: no exception.

- [ ] **Step 3: Manual smoke test (optional but recommended)**

Start the bot normally with `python -m tgbot`. Logs should include:
```
Trading module registered (chains: sol + ...)
```
No APScheduler errors. The job appears in `app.job_queue.scheduler.get_jobs()` with id `trading:portfolio_snapshot_daily`.

- [ ] **Step 4: Commit**

```bash
git add tgbot/trading/__init__.py
git commit -m "feat(portfolio): register daily snapshot job at 08:00 Europe/Paris"
```

---

## Task 6: Add Portfolio button + period switching callbacks

**Files:**
- Modify: `tgbot/trading/handlers.py`

This task wires the UI. No new unit tests (handlers are integration-heavy; the underlying `render_chart` / `take_snapshot` are already tested). A manual smoke check at the end.

- [ ] **Step 1: Add `📊 Portfolio` button to `_home_markup`**

In `tgbot/trading/handlers.py`, locate `_home_markup` (around line 347). Replace the function body with:

```python
    def _home_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 Wallets surveillés", callback_data="trd:wallets")],
            [InlineKeyboardButton("🔔 Alertes MC", callback_data="trd:alerts")],
            [InlineKeyboardButton("💰 Holdings", callback_data="trd:hold")],
            [InlineKeyboardButton("📊 Portfolio", callback_data="trd:portfolio")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu:home")],
        ])
```

- [ ] **Step 2: Add a renderer for the portfolio view**

Above `async def on_trading_callback` (just before its definition), add:

```python
    def _period_markup(active: str, *, has_data: bool) -> InlineKeyboardMarkup:
        """Build the period switcher row + back button.
        If no data exists yet, surface a 'Snapshot maintenant' instead."""
        def _label(code: str, text: str) -> str:
            return f"{text} ✓" if code == active else text
        rows: list[list[InlineKeyboardButton]] = []
        if has_data:
            rows.append([
                InlineKeyboardButton(_label("7d",  "7j"),
                                     callback_data="trd:portfolio:7d"),
                InlineKeyboardButton(_label("30d", "30j"),
                                     callback_data="trd:portfolio:30d"),
                InlineKeyboardButton(_label("90d", "90j"),
                                     callback_data="trd:portfolio:90d"),
                InlineKeyboardButton(_label("all", "All"),
                                     callback_data="trd:portfolio:all"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("🔄 Snapshot maintenant",
                                     callback_data="trd:portfolio:force"),
            ])
        rows.append([
            InlineKeyboardButton("⬅️ Retour Trading", callback_data="trd:home"),
        ])
        return InlineKeyboardMarkup(rows)

    async def _render_portfolio(query, ctx, period: str = "30d") -> None:
        """Generate and send/replace the portfolio chart message."""
        from .portfolio import (
            load_snapshots_for_period, render_chart, take_snapshot,
        )
        import asyncio
        from io import BytesIO
        from telegram import InputMediaPhoto

        snaps_all = db.list_snapshots()
        has_data = bool(snaps_all)

        if not has_data:
            caption = (
                "*📊 Portfolio*\n"
                "Pas encore de données. Le premier snapshot sera pris à 08:00 "
                "(Europe/Paris) demain — ou clique le bouton pour forcer."
            )
            try:
                await query.edit_message_text(
                    caption, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_period_markup("30d", has_data=False),
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
            return

        snaps = load_snapshots_for_period(db, period)
        days_map = {"7d": 7, "30d": 30, "90d": 90, "all": 0}
        png = await asyncio.to_thread(render_chart, snaps, days_map[period])

        # Caption summary
        last = snaps[-1] if snaps else snaps_all[-1]
        if len(snaps) >= 2:
            ref = snaps[0]["total_usd"]
            pct = ((last["total_usd"] / ref) - 1.0) * 100.0 if ref > 0 else 0.0
            sign = "+" if pct >= 0 else ""
            emoji = "🟢" if pct >= 0 else "🔴"
            perf_line = f"{period}: {sign}{pct:.1f}% {emoji}"
        else:
            perf_line = f"{period}: — (1 seul snapshot)"
        partial = sum(1 for s in snaps if s.get("wallets_ko", 0) > 0)
        partial_tag = f" ({partial} partiels)" if partial else ""
        caption = (
            f"*📊 Portfolio ({period})*\n"
            f"Actuel: ${last['total_usd']:,.0f}\n"
            f"{perf_line}\n"
            f"Snapshots: {len(snaps)}{partial_tag}"
        )

        # Replace media if previous message was a photo, else send new
        if query.message and query.message.photo:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=BytesIO(png), caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                ),
                reply_markup=_period_markup(period, has_data=True),
            )
        else:
            # Delete the text message and send a fresh photo (Telegram API
            # forbids editing text → photo in place).
            try:
                await query.message.delete()
            except Exception:
                pass
            await ctx.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=BytesIO(png),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_period_markup(period, has_data=True),
            )

    async def _force_snapshot(query, ctx) -> None:
        from .portfolio import take_snapshot
        await query.edit_message_text(
            "⏳ Snapshot en cours…", parse_mode=ParseMode.MARKDOWN,
        )
        try:
            result = await take_snapshot(
                db,
                helius_key=monitor.helius_api_key,
                alchemy_key=monitor.alchemy_api_key,
                price_client=monitor.price_client,
            )
        except Exception as e:
            logger.exception("force snapshot failed")
            await query.edit_message_text(
                f"❌ Snapshot échoué : `{type(e).__name__}: {e}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_period_markup("30d", has_data=False),
            )
            return
        if result is None:
            await query.edit_message_text(
                "❌ Tous les wallets en échec. Réessaie plus tard.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_period_markup("30d", has_data=False),
            )
            return
        await _render_portfolio(query, ctx, period="30d")
```

- [ ] **Step 3: Route the new callbacks in `on_trading_callback`**

Inside `on_trading_callback` (around the existing `if action == "anoop":` block), add **before** that block:

```python
        if action == "portfolio":
            period = parts[2] if len(parts) >= 3 else "30d"
            if period == "force":
                await _force_snapshot(query, ctx)
                return
            if period in ("7d", "30d", "90d", "all"):
                await _render_portfolio(query, ctx, period=period)
                return
            # Default click on the menu button (no period segment)
            await _render_portfolio(query, ctx, period="30d")
            return
```

- [ ] **Step 4: Sanity check — module imports OK**

Run: `python -c "import tgbot.trading.handlers"`
Expected: no exception.

- [ ] **Step 5: Manual smoke test**

Start bot with `python -m tgbot`. In Telegram:
1. Open Trading menu → confirm "📊 Portfolio" button is visible.
2. Click it. Since DB is empty on fresh install, see the "Pas encore de données" placeholder + "🔄 Snapshot maintenant" button.
3. Click "🔄 Snapshot maintenant" → wait → see the chart with 1 data point, period switcher (7j / 30j ✓ / 90j / All), back button.
4. Click "7j" → image refreshes in place, period marker switches.
5. Click "⬅️ Retour Trading" → returns to Trading home menu.

Note: with only 1 snapshot, perf line shows "— (1 seul snapshot)". This is expected; will populate over days.

- [ ] **Step 6: Commit**

```bash
git add tgbot/trading/handlers.py
git commit -m "feat(portfolio): Telegram UI — button, period switcher, force snapshot"
```

---

## Task 7: Run the full test suite

**Files:** none modified.

- [ ] **Step 1: Run the entire test suite to ensure no regression**

Run: `python -m pytest -v`
Expected: all pre-existing tests still pass + the 16 new tests (5 DB + 5 snapshot + 6 chart) pass.

- [ ] **Step 2: If any failure, fix it and re-run before declaring done**

Failure analysis: any test that breaks is likely from a missed dep (matplotlib not installed in CI venv) or from an unrelated regression — investigate before committing further.

- [ ] **Step 3: No commit needed if all green**

---

## Verification checklist

Before considering the feature done, confirm:

- [ ] `python -m pytest -v` is fully green.
- [ ] Bot starts cleanly with `python -m tgbot`.
- [ ] APScheduler shows the `trading:portfolio_snapshot_daily` job in `get_jobs()`.
- [ ] `📊 Portfolio` button appears in the Trading menu.
- [ ] Empty-state placeholder + force button appear when no snapshot exists.
- [ ] Force-snapshot writes a row and renders a single-point chart.
- [ ] Period switcher (7j/30j/90j/All) edits the photo in place without resending a new message.
- [ ] After 24h, the daily cron has run and a second snapshot exists (verifiable via `sqlite3 data/trading.db "SELECT count(*) FROM portfolio_snapshots"`).
