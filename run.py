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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from polymarket_client import fetch_active_weather_markets
from strategy import (
    TradeLog, evaluate_market,
    MIN_EDGE, MIN_VOLUME, MAX_DAYS_AHEAD,
)

# Cap per-cycle market evaluations so a single hourly run always finishes.
MAX_MARKETS_PER_CYCLE = 60
FORECAST_WORKERS      = 8

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

    # Cap per cycle: prioritize markets with the most volume so we always
    # finish inside the Actions timeout. Unconsidered markets roll to next cycle.
    if len(markets) > MAX_MARKETS_PER_CYCLE:
        markets.sort(key=lambda s: s.volume or 0, reverse=True)
        markets = markets[:MAX_MARKETS_PER_CYCLE]
        log.info("capped to top %d markets by volume", MAX_MARKETS_PER_CYCLE)

    open_exposure = ledger.total_open_exposure()

    # Filter out markets we've already traded.
    to_eval = [
        s for s in markets
        if not ledger.already_open(s.market_id, "YES")
        and not ledger.already_open(s.market_id, "NO")
    ]
    log.info("evaluating %d markets with %d workers",
             len(to_eval), FORECAST_WORKERS)

    # Evaluate in parallel. Each worker does its own HTTP calls so they stack.
    # We DO NOT update exposure inside the pool; we do the sizing-pass once
    # all results are back, sequentially, so the exposure cap is respected.
    trade_results: list = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=FORECAST_WORKERS) as ex:
        futures = {
            ex.submit(evaluate_market, snap, bankroll_usd, open_exposure): snap
            for snap in to_eval
        }
        for fut in as_completed(futures):
            snap = futures[fut]
            try:
                tr = fut.result()
            except Exception as e:
                log.debug("evaluate_market error on %s: %s", snap.slug, e)
                continue
            if tr is not None:
                trade_results.append(tr)
    log.info("parallel evaluation finished in %.1fs, %d candidate trades",
             time.time() - t0, len(trade_results))

    # Sort candidates by edge desc so best opportunities get funded first.
    trade_results.sort(key=lambda t: t.edge, reverse=True)

    trades_opened = 0
    for tr in trade_results:
        if open_exposure + tr.size_usd > bankroll_usd * 0.60:
            log.info("exposure cap reached, stopping further entries")
            break
        ledger.record(tr)
        open_exposure += tr.size_usd
        trades_opened += 1

    log.info("considered %d · opened %d · open exposure $%.2f",
             len(to_eval), trades_opened, open_exposure)

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
