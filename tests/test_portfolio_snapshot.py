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
