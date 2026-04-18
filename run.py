"""
run.py
------
Main loop. Scans Polymarket weather markets, computes fair values from ensemble
forecasts, logs paper trades to disk, and updates the dashboard.

Usage:
    python run.py                           # default: paper, $1000 bankroll
    python run.py --bankroll 5000           # bigger virtual bankroll
    python run.py --interval 120            # scan every 120s
    python run.py --once                    # single scan, useful for debugging
    python run.py --mode live --pk $PK      # live trading (requires wallet + py-clob-client)

Serve the dashboard from the project directory while the bot runs:

    python -m http.server 8787
    # then open http://localhost:8787/dashboard.html
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from polymarket_client import fetch_active_weather_markets
from strategy import (
    TradeLog, evaluate_market,
    MIN_EDGE, MIN_VOLUME, MAX_DAYS_AHEAD,
)

log = logging.getLogger("run")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-7s %(name)-12s %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "bot.log"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers,
                        force=True)


def one_cycle(ledger: TradeLog, bankroll_usd: float) -> None:
    log.info("=== cycle start ===")
    try:
        markets = fetch_active_weather_markets(min_volume=MIN_VOLUME)
    except Exception as e:
        log.error("failed to fetch markets: %s", e)
        return

    log.info("fetched %d candidate weather markets", len(markets))
    open_exposure = ledger.total_open_exposure()

    considered = 0
    trades_opened = 0
    for snap in markets:
        if ledger.already_open(snap.market_id, "YES") or \
           ledger.already_open(snap.market_id, "NO"):
            continue
        considered += 1
        try:
            tr = evaluate_market(snap, bankroll_usd, open_exposure)
        except Exception as e:
            log.debug("evaluate_market error on %s: %s", snap.slug, e)
            continue
        if tr is None:
            continue
        ledger.record(tr)
        open_exposure += tr.size_usd
        trades_opened += 1
        if open_exposure >= bankroll_usd * 0.60:
            log.info("hit 60%% exposure cap, stopping further entries this cycle")
            break

    log.info("considered %d · opened %d · open exposure $%.2f",
             considered, trades_opened, open_exposure)

    ledger.snapshot_for_dashboard(
        bankroll_usd=bankroll_usd,
        latest_markets=[s.to_dict() for s in markets[:100]],
    )
    log.info("=== cycle done ===\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=1000.0,
                    help="paper bankroll in USD (default 1000)")
    ap.add_argument("--interval", type=int, default=180,
                    help="seconds between scans (default 180)")
    ap.add_argument("--once", action="store_true", help="single scan then exit")
    ap.add_argument("--log-dir", default="logs")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    setup_logging(log_dir)

    log.info("Polymarket Weather Bot starting")
    log.info("  mode        = PAPER")
    log.info("  bankroll    = $%.2f", args.bankroll)
    log.info("  interval    = %ds", args.interval)
    log.info("  min edge    = %.1f pp", MIN_EDGE * 100)
    log.info("  min volume  = $%.0f", MIN_VOLUME)
    log.info("  max horizon = %d days", MAX_DAYS_AHEAD)

    ledger = TradeLog(log_dir=str(log_dir))

    # Graceful shutdown so dashboard snapshot is flushed on Ctrl-C.
    stop = {"flag": False}
    def _sig(_s, _f):
        log.info("caught signal, shutting down after this cycle")
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while True:
        try:
            one_cycle(ledger, args.bankroll)
        except Exception as e:
            log.exception("cycle crashed: %s", e)
        if args.once or stop["flag"]:
            break
        log.info("sleeping %ds…", args.interval)
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("bot shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
