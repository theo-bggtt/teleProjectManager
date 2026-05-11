"""Dexscreener price + market-cap client.

Dexscreener is keyless and aggregates pools across many DEXes. We query
``/latest/dex/tokens/{address}`` and pick the pair with the highest USD
liquidity as the canonical reference (best signal-to-noise for price).

A small in-memory TTL cache prevents hammering the API when multiple
alerts target the same token.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens"
DEFAULT_TTL_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 8


@dataclass
class TokenInfo:
    address: str
    chain: str             # dexscreener chain id: "solana", "ethereum", "base", "bsc", ...
    symbol: str
    name: str
    price_usd: Optional[float]
    mc_usd: Optional[float]            # market cap (or FDV fallback)
    liquidity_usd: Optional[float]
    pair_url: Optional[str]


# Map of internal chain shortcodes → Dexscreener chain ids (best-effort).
_CHAIN_DEX_IDS = {
    "sol": "solana",
    "eth": "ethereum",
    "base": "base",
    "bsc": "bsc",
}


def dexscreener_chain_id(chain: str) -> str:
    return _CHAIN_DEX_IDS.get(chain, chain)


class PriceClient:
    """Async Dexscreener client with a short TTL cache.

    Designed to be created once and shared across the trading module.
    Caller must ``await client.close()`` on shutdown.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._ttl = ttl_seconds
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None
        # cache_key = (chain, address.lower()) -> (expires_epoch, TokenInfo | None)
        self._cache: dict[tuple[str, str], tuple[float, Optional[TokenInfo]]] = {}
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_token(self, address: str, chain: str) -> Optional[TokenInfo]:
        """Return the best pair info for ``address`` on ``chain``.

        ``chain`` may be either an internal shortcode ("sol", "eth", "base", "bsc")
        or a Dexscreener chain id directly ("solana", "ethereum", ...).
        Returns ``None`` if the token is unknown or the API errors out.
        """
        wanted_chain = dexscreener_chain_id(chain)
        key = (wanted_chain, address.lower())

        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        # Single-flight per key to avoid thundering-herd on cache miss.
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            info = await self._fetch(address, wanted_chain)
            self._cache[key] = (now + self._ttl, info)
            return info

    async def _fetch(self, address: str, wanted_chain: str) -> Optional[TokenInfo]:
        session = await self._ensure_session()
        url = f"{DEXSCREENER_BASE}/{address}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Dexscreener HTTP %s for %s", resp.status, address)
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Dexscreener fetch failed for %s: %s", address, e)
            return None

        pairs = payload.get("pairs") or []
        # Keep only pairs on the requested chain, then pick highest USD liquidity.
        candidates = [
            p for p in pairs
            if (p.get("chainId") or "").lower() == wanted_chain.lower()
        ]
        if not candidates:
            return None

        def _liq(p: dict) -> float:
            liq = p.get("liquidity") or {}
            try:
                return float(liq.get("usd") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        best = max(candidates, key=_liq)
        base = best.get("baseToken") or {}
        try:
            price = float(best["priceUsd"]) if best.get("priceUsd") else None
        except (TypeError, ValueError):
            price = None
        mc = best.get("marketCap") or best.get("fdv")
        try:
            mc = float(mc) if mc is not None else None
        except (TypeError, ValueError):
            mc = None

        return TokenInfo(
            address=base.get("address") or address,
            chain=wanted_chain,
            symbol=base.get("symbol") or "?",
            name=base.get("name") or "?",
            price_usd=price,
            mc_usd=mc,
            liquidity_usd=_liq(best) or None,
            pair_url=best.get("url"),
        )
