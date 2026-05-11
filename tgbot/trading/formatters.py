"""Pure formatters for trading messages.

Kept separate from the orchestrator so ``monitor.py`` doesn't need to
know about Telegram markdown. Each function returns a markdown string
ready to pass to ``bot.send_message(parse_mode=MARKDOWN)``.
"""
from __future__ import annotations

from typing import Optional


# Explorer URL helpers ---------------------------------------------------
def _solana_tx_url(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"


def _evm_tx_url(chain: str, txhash: str) -> str:
    bases = {
        "eth": "https://etherscan.io/tx/",
        "base": "https://basescan.org/tx/",
        "bsc": "https://bscscan.com/tx/",
    }
    return bases.get(chain, "https://etherscan.io/tx/") + txhash


def tx_explorer_url(chain: str, sig_or_hash: str) -> str:
    if chain == "sol":
        return _solana_tx_url(sig_or_hash)
    return _evm_tx_url(chain, sig_or_hash)


def _wallet_explorer_url(chain: str, address: str) -> str:
    if chain == "sol":
        return f"https://solscan.io/account/{address}"
    bases = {
        "eth": "https://etherscan.io/address/",
        "base": "https://basescan.org/address/",
        "bsc": "https://bscscan.com/address/",
    }
    return bases.get(chain, "https://etherscan.io/address/") + address


def _short_addr(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:4]}…{addr[-4:]}"


def _fmt_mc(mc: float) -> str:
    if mc >= 1e9:
        return f"{mc / 1e9:.2f}B"
    if mc >= 1e6:
        return f"{mc / 1e6:.2f}M"
    if mc >= 1e3:
        return f"{mc / 1e3:.2f}K"
    return f"{mc:.2f}"


def _escape_md(text: str) -> str:
    """Escape characters that are special in Telegram MarkdownV1.

    We use the legacy Markdown parser (``ParseMode.MARKDOWN``) elsewhere
    in the bot. It only treats ``* _ ` [`` specially.
    """
    for c in ("*", "_", "`", "["):
        text = text.replace(c, "\\" + c)
    return text


# ── activity (default fallback when we can't classify) ─────────────────
def activity_message(
    chain: str,
    wallet: str,
    wallet_label: Optional[str],
    sig_or_hash: str,
    note: str = "Activity detected",
) -> str:
    label = f" _{_escape_md(wallet_label)}_" if wallet_label else ""
    return (
        f"🛰 *{chain.upper()}*{label}\n"
        f"Wallet: [{_short_addr(wallet)}]({_wallet_explorer_url(chain, wallet)})\n"
        f"{_escape_md(note)}\n"
        f"[tx →]({tx_explorer_url(chain, sig_or_hash)})"
    )


# ── swap ───────────────────────────────────────────────────────────────
def swap_message(
    chain: str,
    wallet: str,
    wallet_label: Optional[str],
    sig_or_hash: str,
    side: str,                       # "buy" | "sell"
    token_symbol: str,
    token_address: str,
    amount: Optional[float],
    usd_value: Optional[float] = None,
    mc_usd: Optional[float] = None,
) -> str:
    label = f" _{_escape_md(wallet_label)}_" if wallet_label else ""
    icon = "🟢" if side == "buy" else "🔴"
    verb = "Bought" if side == "buy" else "Sold"
    amt = f" `{amount:,.4f}`" if amount is not None else ""
    usd = f" (~${usd_value:,.2f})" if usd_value is not None else ""
    mc = f"\nMC: *${_fmt_mc(mc_usd)}*" if mc_usd is not None else ""
    return (
        f"{icon} *{chain.upper()}*{label} — {verb}\n"
        f"`{token_symbol}`{amt}{usd}\n"
        f"Wallet: [{_short_addr(wallet)}]({_wallet_explorer_url(chain, wallet)})\n"
        f"Token: `{_short_addr(token_address)}`"
        f"{mc}\n"
        f"[tx →]({tx_explorer_url(chain, sig_or_hash)})"
    )


# ── transfer ───────────────────────────────────────────────────────────
def transfer_message(
    chain: str,
    wallet: str,
    wallet_label: Optional[str],
    sig_or_hash: str,
    direction: str,                  # "in" | "out"
    counterparty: Optional[str],
    token_symbol: str,
    amount: Optional[float],
) -> str:
    label = f" _{_escape_md(wallet_label)}_" if wallet_label else ""
    icon = "📥" if direction == "in" else "📤"
    amt = f"`{amount:,.4f}` " if amount is not None else ""
    cp = (
        f"\nFrom/To: [{_short_addr(counterparty)}]({_wallet_explorer_url(chain, counterparty)})"
        if counterparty else ""
    )
    return (
        f"{icon} *{chain.upper()}*{label} — Transfer {direction.upper()}\n"
        f"{amt}`{token_symbol}`\n"
        f"Wallet: [{_short_addr(wallet)}]({_wallet_explorer_url(chain, wallet)})"
        f"{cp}\n"
        f"[tx →]({tx_explorer_url(chain, sig_or_hash)})"
    )


# ── MC alert ───────────────────────────────────────────────────────────
def mc_alert_message(
    alert_id: int,
    chain: str,
    token_symbol: str,
    token_address: str,
    direction: str,
    mc_target: float,
    mc_current: float,
    pair_url: Optional[str],
    label: Optional[str],
    persistent: bool,
) -> str:
    arrow = "🚀" if direction == "above" else "📉"
    cross = "crossed *above*" if direction == "above" else "fell *below*"
    tag = f" — _{_escape_md(label)}_" if label else ""
    kind = "(persistent — staying armed)" if persistent else "(one-shot — alert disarmed)"
    link = f"\n[chart →]({pair_url})" if pair_url else ""
    return (
        f"{arrow} *MC alert #{alert_id}*{tag}\n"
        f"`{token_symbol}` {cross} `${_fmt_mc(mc_target)}`\n"
        f"Now: *${_fmt_mc(mc_current)}* — *{chain.upper()}*\n"
        f"Token: `{_short_addr(token_address)}` {kind}"
        f"{link}"
    )
