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
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
