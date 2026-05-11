"""EVM wallet monitoring via Alchemy WebSocket.

We use ``eth_subscribe`` with the Alchemy-extended subscription type
``alchemy_minedTransactions`` filtered by per-wallet ``from``/``to`` so
the Alchemy node streams only the relevant mined transactions.

Detailed swap decoding (Uniswap V2/V3 + Universal Router selectors,
ERC20 Transfer event log parsing) is intentionally minimal in this
step — we surface a generic activity event with the tx hash and the
chain explorer URL. Richer parsing is a later iteration.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from .db import TradingDB
from .prices import PriceClient
from .solana import EventSink, Holding, WalletEvent

logger = logging.getLogger(__name__)


# Internal shortcode → Alchemy subdomain piece.
_ALCHEMY_NETS = {
    "eth": "eth-mainnet",
    "base": "base-mainnet",
    "bsc": "bnb-mainnet",
}


def alchemy_wss_url(chain: str, api_key: str) -> str:
    net = _ALCHEMY_NETS.get(chain)
    if net is None:
        raise ValueError(f"Unsupported EVM chain {chain!r}")
    return f"wss://{net}.g.alchemy.com/v2/{api_key}"


def alchemy_http_url(chain: str, api_key: str) -> str:
    net = _ALCHEMY_NETS.get(chain)
    if net is None:
        raise ValueError(f"Unsupported EVM chain {chain!r}")
    return f"https://{net}.g.alchemy.com/v2/{api_key}"


_NATIVE_SYMBOLS = {"eth": "ETH", "base": "ETH", "bsc": "BNB"}


async def fetch_evm_holdings(
    chain: str, api_key: str, owner: str,
    price_client: Optional[PriceClient] = None,
    max_tokens: int = 50,
) -> tuple[list[Holding], Optional[float]]:
    """Return (token_holdings, native_value_usd) for an EVM wallet.

    Uses Alchemy's ``alchemy_getTokenBalances`` (defaults to ERC20 tokens
    with a non-zero balance) and ``alchemy_getTokenMetadata`` per token.
    Native balance comes from ``eth_getBalance``. Prices are best-effort
    via Dexscreener — many long-tail tokens will show no price.
    """
    url = alchemy_http_url(chain, api_key)
    timeout = aiohttp.ClientTimeout(total=20)
    native_symbol = _NATIVE_SYMBOLS.get(chain, "ETH")
    holdings: list[Holding] = []
    native_value_usd: Optional[float] = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Native balance
        balances_req = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
            "params": [owner, "latest"],
        }
        async with session.post(url, json=balances_req) as resp:
            resp.raise_for_status()
            native_payload = await resp.json()
        try:
            native_wei = int(native_payload.get("result", "0x0"), 16)
            native_amount = native_wei / 1e18
        except (TypeError, ValueError):
            native_amount = 0.0

        # 2. ERC20 balances
        tok_req = {
            "jsonrpc": "2.0", "id": 2, "method": "alchemy_getTokenBalances",
            "params": [owner, "erc20"],
        }
        async with session.post(url, json=tok_req) as resp:
            resp.raise_for_status()
            tok_payload = await resp.json()
        token_balances = ((tok_payload.get("result") or {}).get("tokenBalances") or [])
        non_zero = []
        for tb in token_balances:
            raw = tb.get("tokenBalance")
            if not raw or raw == "0x" or int(raw, 16) == 0:
                continue
            non_zero.append((tb["contractAddress"], int(raw, 16)))
        non_zero = non_zero[:max_tokens]

        # 3. Metadata + price per token (sequential to stay simple; caller
        # uses /holdings interactively so latency is acceptable).
        for addr, raw_balance in non_zero:
            meta_req = {
                "jsonrpc": "2.0", "id": 3, "method": "alchemy_getTokenMetadata",
                "params": [addr],
            }
            try:
                async with session.post(url, json=meta_req) as resp:
                    resp.raise_for_status()
                    meta_payload = await resp.json()
            except aiohttp.ClientError:
                continue
            meta = meta_payload.get("result") or {}
            decimals = meta.get("decimals")
            if decimals is None:
                continue
            try:
                amount = raw_balance / (10 ** int(decimals))
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            symbol = meta.get("symbol") or "?"
            name = meta.get("name") or "?"
            price = None
            value = None
            if price_client is not None:
                info = await price_client.get_token(addr, chain)
                if info is not None:
                    price = info.price_usd
                    if price is not None:
                        value = price * amount
            holdings.append(Holding(
                symbol=symbol, name=name, mint_or_address=addr,
                amount=amount, price_usd=price, value_usd=value,
            ))

        # 4. Native value via wrapped-token Dexscreener price.
        if native_amount > 0 and price_client is not None:
            wrapped = {
                "eth":  "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH (Ethereum)
                "base": "0x4200000000000000000000000000000000000006",  # WETH (Base)
                "bsc":  "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
            }.get(chain)
            if wrapped:
                info = await price_client.get_token(wrapped, chain)
                if info is not None and info.price_usd is not None:
                    native_value_usd = info.price_usd * native_amount

    holdings.sort(key=lambda h: (h.value_usd or 0), reverse=True)
    # Prepend native as a synthetic "holding" entry too, for clarity.
    holdings.insert(0, Holding(
        symbol=native_symbol, name=f"Native {native_symbol}",
        mint_or_address="native", amount=native_amount,
        price_usd=(native_value_usd / native_amount) if native_value_usd and native_amount else None,
        value_usd=native_value_usd,
    ))
    return holdings, native_value_usd


class EvmMonitor:
    """One Alchemy WSS subscription for one EVM chain."""

    def __init__(self, chain: str, api_key: str, db: TradingDB, on_event: EventSink):
        self._chain = chain
        self._api_key = api_key
        self._db = db
        self._on_event = on_event
        self._wallets_changed = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ── public ────────────────────────────────────────────────────────
    def notify_wallets_changed(self) -> None:
        self._wallets_changed.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run_forever(), name=f"evm-monitor-{self._chain}"
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wallets_changed.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            finally:
                self._task = None

    # ── loop ──────────────────────────────────────────────────────────
    async def _run_forever(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._one_session()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Alchemy WSS %s error: %s; retry in %ss", self._chain, e, backoff
                )
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
        wallets = self._db.list_wallets(self._chain)
        addresses = [w["address"] for w in wallets]
        labels = {w["address"].lower(): w.get("label") for w in wallets}

        if not addresses:
            logger.debug("EVM monitor %s idle: no wallets watched.", self._chain)
            self._wallets_changed.clear()
            await self._wait_for_wallets(timeout=30)
            return

        url = alchemy_wss_url(self._chain, self._api_key)
        logger.info(
            "Alchemy WSS %s connecting (watching %d wallet(s))",
            self._chain, len(addresses),
        )
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024,
        ) as ws:
            await self._subscribe(ws, addresses)
            self._wallets_changed.clear()
            await self._consume(ws, labels)

    async def _subscribe(self, ws, addresses: list[str]) -> None:
        # alchemy_minedTransactions accepts an array of filter objects;
        # we expand each wallet to two filters (from + to) so the node
        # streams either direction.
        filters = []
        for a in addresses:
            filters.append({"from": a})
            filters.append({"to": a})
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "alchemy_minedTransactions",
                {
                    "addresses": filters,
                    "includeRemoved": False,
                    "hashesOnly": False,
                },
            ],
        }
        await ws.send(json.dumps(req))
        try:
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            ack = json.loads(ack_raw)
            logger.debug("Alchemy %s subscribe ack: %s", self._chain, ack)
        except asyncio.TimeoutError:
            logger.warning(
                "Alchemy %s subscribe ack timed out; continuing anyway", self._chain
            )

    async def _consume(self, ws, labels: dict[str, Optional[str]]) -> None:
        while not self._stop.is_set() and not self._wallets_changed.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except ConnectionClosed:
                logger.info("Alchemy %s WSS closed by remote; will reconnect", self._chain)
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("method") != "eth_subscription":
                continue
            params = msg.get("params") or {}
            result = params.get("result") or {}
            event = self._normalize(result, labels)
            if event is not None:
                try:
                    await self._on_event(event)
                except Exception:
                    logger.exception("on_event dispatch crashed")

    def _normalize(
        self, result: dict, labels: dict[str, Optional[str]]
    ) -> Optional[WalletEvent]:
        # Alchemy delivers either the full transaction (default) or just
        # the hash (when hashesOnly=true). We requested full transactions.
        tx = result.get("transaction") or result
        tx_hash = tx.get("hash")
        if not tx_hash:
            return None
        frm = (tx.get("from") or "").lower()
        to = (tx.get("to") or "").lower()
        wallet = frm if frm in labels else (to if to in labels else None)
        if wallet is None:
            # Subscription filter matched but neither from/to is our wallet —
            # could be a contract event subscription side-effect; skip.
            return None
        direction = "out" if wallet == frm else "in"
        counterparty = to if direction == "out" else frm

        # Decode native value if present (hex string).
        amount = None
        val = tx.get("value")
        if isinstance(val, str) and val.startswith("0x"):
            try:
                amount = int(val, 16) / 1e18
            except ValueError:
                amount = None

        # Heuristic: tx with non-zero value AND empty calldata → native
        # transfer; everything else (ERC20 transfer, swap, contract call)
        # surfaces as generic activity until richer decoding is added.
        is_native = (
            amount is not None
            and amount > 0
            and tx.get("input", "0x") == "0x"
        )
        if is_native:
            symbol = {"eth": "ETH", "base": "ETH", "bsc": "BNB"}.get(self._chain, "ETH")
            return WalletEvent(
                chain=self._chain,
                wallet=wallet,
                wallet_label=labels.get(wallet),
                sig_or_hash=tx_hash,
                kind="transfer",
                direction=direction,
                counterparty=counterparty,
                token_symbol=symbol,
                amount=amount,
                raw=result,
            )

        return WalletEvent(
            chain=self._chain,
            wallet=wallet,
            wallet_label=labels.get(wallet),
            sig_or_hash=tx_hash,
            kind="activity",
            raw=result,
        )
