"""Solana wallet monitoring via Helius Atlas WebSocket.

We use ``transactionSubscribe`` on the Atlas Geyser-backed endpoint,
filtered by ``accountInclude`` (= the addresses we want notifications
for). The endpoint returns parsed transactions in real-time.

REST helpers (``getAssetsByOwner`` for holdings, ``getSignaturesForAddress``
for backfill) come in later steps.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .db import TradingDB

logger = logging.getLogger(__name__)

HELIUS_ATLAS_WSS = "wss://atlas-mainnet.helius-rpc.com/?api-key={key}"
HELIUS_RPC_HTTP = "https://mainnet.helius-rpc.com/?api-key={key}"


@dataclass
class WalletEvent:
    """Normalized event delivered by chain-specific monitors."""

    chain: str                                  # "sol" | "eth" | "base" | "bsc"
    wallet: str
    wallet_label: Optional[str]
    sig_or_hash: str
    kind: str                                   # "activity" | "swap" | "transfer"
    side: Optional[str] = None                  # "buy" | "sell" (swap)
    direction: Optional[str] = None             # "in" | "out" (transfer)
    token_symbol: Optional[str] = None
    token_address: Optional[str] = None
    amount: Optional[float] = None
    counterparty: Optional[str] = None
    raw: dict = field(default_factory=dict)


# Callback signature: async def on_event(event: WalletEvent) -> None
EventSink = Callable[[WalletEvent], Awaitable[None]]


class SolanaMonitor:
    """Helius WSS subscriber for one or more Solana wallets.

    On startup it queries the DB for the current wallet list, subscribes
    with ``accountInclude``, and dispatches every received transaction
    via the supplied ``on_event`` callback. Reconnects with exponential
    backoff. ``notify_wallets_changed()`` triggers a clean re-subscribe.
    """

    def __init__(self, api_key: str, db: TradingDB, on_event: EventSink):
        self._api_key = api_key
        self._db = db
        self._on_event = on_event
        self._wallets_changed = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ── public API ────────────────────────────────────────────────────
    def notify_wallets_changed(self) -> None:
        self._wallets_changed.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_forever(), name="solana-monitor")

    async def stop(self) -> None:
        self._stop.set()
        self._wallets_changed.set()  # unblock any wait
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            finally:
                self._task = None

    # ── internals ─────────────────────────────────────────────────────
    async def _run_forever(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._one_session()
                backoff = 1  # clean reconnect
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Solana WSS session error: %s; retry in %ss", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)

    async def _wait_for_wallets(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wallets_changed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _one_session(self) -> None:
        wallets = self._db.list_wallets("sol")
        addresses = [w["address"] for w in wallets]
        labels = {w["address"]: w.get("label") for w in wallets}

        if not addresses:
            logger.debug("Solana monitor idle: no wallets watched.")
            self._wallets_changed.clear()
            await self._wait_for_wallets(timeout=30)
            return

        url = HELIUS_ATLAS_WSS.format(key=self._api_key)
        logger.info("Solana WSS connecting (watching %d wallet(s))", len(addresses))
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024,
        ) as ws:
            await self._subscribe(ws, addresses)
            self._wallets_changed.clear()
            await self._consume(ws, labels)

    async def _subscribe(self, ws, addresses: list[str]) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {
                    "accountInclude": addresses,
                    "vote": False,
                    "failed": False,
                },
                {
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        await ws.send(json.dumps(req))
        # Helius sends the subscription id back; consume it but don't validate.
        try:
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            ack = json.loads(ack_raw)
            logger.debug("Solana subscribe ack: %s", ack)
        except asyncio.TimeoutError:
            logger.warning("Solana subscribe ack timed out; continuing anyway")

    async def _consume(self, ws, labels: dict[str, Optional[str]]) -> None:
        while not self._stop.is_set() and not self._wallets_changed.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except ConnectionClosed:
                logger.info("Solana WSS closed by remote; will reconnect")
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Solana WSS: non-JSON frame ignored")
                continue
            if msg.get("method") != "transactionNotification":
                continue
            result = (msg.get("params") or {}).get("result") or {}
            event = self._normalize(result, labels)
            if event is not None:
                try:
                    await self._on_event(event)
                except Exception:
                    logger.exception("on_event dispatch crashed")

    def _normalize(
        self, result: dict, labels: dict[str, Optional[str]]
    ) -> Optional[WalletEvent]:
        sig = result.get("signature")
        if not sig:
            return None
        tx = result.get("transaction") or {}
        # Identify which watched wallet was involved by scanning accountKeys.
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = []
        for k in msg.get("accountKeys") or []:
            if isinstance(k, dict):
                keys.append(k.get("pubkey"))
            elif isinstance(k, str):
                keys.append(k)
        wallet = next((k for k in keys if k in labels), None)
        if wallet is None:
            # Subscription matched on a watched program/account but no wallet
            # is directly in the keys — still surface as activity on the first
            # watched key we can find anywhere in the payload.
            wallet = next(iter(labels.keys()))

        # Future iterations will inspect ``meta.preTokenBalances`` /
        # ``meta.postTokenBalances`` to derive swap side + amounts. For now
        # we surface a generic activity event — enough to verify the WSS
        # pipeline end-to-end on real trades.
        return WalletEvent(
            chain="sol",
            wallet=wallet,
            wallet_label=labels.get(wallet),
            sig_or_hash=sig,
            kind="activity",
            raw=result,
        )
