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
