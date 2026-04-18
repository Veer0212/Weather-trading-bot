"""
polymarket_trader.py
--------------------
TODO / scaffolding: live-trading adapter. Only needed when you flip from paper
to real money. See SETUP_GUIDE.md §2 for the full switchover procedure.

Requires:
    pip install py-clob-client web3

Design: replace `strategy.TradeLog.record(trade)` with a call into
`place_live_order(trade)` here. The paper log continues to track P&L in
parallel (don't delete it — that is your live-vs-intended audit trail).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("trader")


def _get_client():
    """Lazily import and construct the CLOB client."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as e:
        raise RuntimeError(
            "py-clob-client not installed. Run: pip install py-clob-client web3"
        ) from e

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY env var not set")

    client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)

    # Derive API creds from the wallet signature. Polymarket caches these
    # deterministically so it is safe to call on every bot start.
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    return client


def place_live_order(market_id: str, token_id: str, side: str,
                     price: float, size_usd: float) -> Optional[dict]:
    """
    Place a GTC limit order on Polymarket's CLOB.

    Args:
        market_id: condition ID of the market (used for logging only)
        token_id:  the CLOB token ID for the outcome you are buying
        side:      "YES" or "NO" (we translate to BUY on that outcome's token)
        price:     price in dollars, 0..1 (e.g. 0.55 == 55¢)
        size_usd:  notional USD size of the order

    Returns the CLOB response dict, or None on failure.
    """
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
    except ImportError as e:
        raise RuntimeError("py-clob-client not installed") from e

    client = _get_client()

    # Polymarket sizes orders in OUTCOME TOKENS, not USD. Shares = usd / price.
    shares = round(size_usd / price, 2)
    order_args = OrderArgs(
        price=price,
        size=shares,
        side="BUY",           # always a BUY on the chosen outcome's token
        token_id=token_id,
    )

    signed = client.create_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    log.info("LIVE ORDER placed: market=%s side=%s price=%.4f shares=%.2f -> %s",
             market_id, side, price, shares, resp)
    return resp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("This module is a stub — see SETUP_GUIDE.md §2 before wiring it in.")
