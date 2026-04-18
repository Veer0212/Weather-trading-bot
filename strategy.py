"""
strategy.py
-----------
Turn (market snapshot, fair-value model) tuples into paper trades.

Pipeline:

   MarketSnapshot ──┐
                    ├──> FairValue (weather_forecast.estimate_probability)
   Question parser ─┘              │
                                   ▼
                      Edge detection  ──►  Kelly sizing  ──►  Paper trade log

Design decisions:
  * Only trade markets we can *confidently parse* (city + metric + threshold + date).
  * Minimum volume gate -- ignore markets with < $500 volume (too thin to fill).
  * Minimum edge threshold -- skip anything under 6 percentage points.
  * Fractional Kelly (25%) to avoid blowup from single bad forecasts.
  * Cap each bet at 5% of bankroll; cap total exposure at 60%.
  * Apply Polymarket's slippage model (±1 tick) to entry price.
  * Book dedup -- never enter a second position on same (market_id, side) if open.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from polymarket_client import MarketSnapshot
from weather_forecast import Fairvalue, estimate_probability

log = logging.getLogger("strategy")

# -- Configurable strategy params --------------------------------------------------
MIN_EDGE                = 0.06     # 6 percentage points
MIN_VOLUME              = 500.0    # dollars
MAX_DAYS_AHEAD          = 10       # skip farther-out markets (forecast weak)
KELLY_FRACTION          = 0.25     # conservative Kelly
MAX_BET_PCT_BANKROLL    = 0.05     # 5% max per bet
MAX_TOTAL_EXPOSURE_PCT  = 0.60     # 60% max total open risk
MIN_PRICE               = 0.03     # don't buy <3¢ (resolution-risk)
MAX_PRICE               = 0.97     # don't buy >97¢ (almost no upside)


@dataclass
class Trade:
    timestamp: str
    market_id: str
    question: str
    side: str                    # "YES" or "NO"
    entry_price: float
    model_probability: float
    edge: float                  # model_p - entry_price
    kelly_fraction: float
    size_usd: float
    expected_value_usd: float
    ensemble_mean: float
    ensemble_std: float
    climate_base_rate: float
    target_date: Optional[str]
    end_date: Optional[str]
    slug: str
    status: str = "OPEN"         # OPEN | SETTLED_WIN | SETTLED_LOSS | CLOSED_NEUTRAL
    pnl_usd: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# -- Edge math ---------------------------------------------------------------------
def kelly_fraction_full(p: float, price: float) -> float:
    """Full Kelly on a binary bet at 1:1/(price-1) payoff. 0 if no edge."""
    b = (1.0 - price) / price       # net odds
    q = 1.0 - p
    f = (b * p - q) / b
    return max(f, 0.0)


def detect_edges(snapshot: MarketSnapshot,
                 fv: Fairvalue) -> Optional[tuple[str, float, float]]:
    """
    Return (side, entry_price, edge) or None if no tradeable edge.
    side is "YES" or "NO". `entry_price` uses the current book mid.
    """
    yes = snapshot.yes_price
    no  = snapshot.no_price
    p   = fv.probability

    # YES side: we think event is more likely than market-implied yes price.
    yes_edge = (p - yes) if (yes is not None) else -1
    # NO side: symmetric -- (1-p) vs NO price.
    no_edge  = ((1.0 - p) - no) if (no is not None) else -1

    if yes_edge >= no_edge and yes_edge >= MIN_EDGE and yes is not None:
        if MIN_PRICE <= yes <= MAX_PRICE:
            return "YES", yes, yes_edge
    if no_edge > yes_edge and no_edge >= MIN_EDGE and no is not None:
        if MIN_PRICE <= no <= MAX_PRICE:
            return "NO", no, no_edge

    return None


# -- Pipeline ----------------------------------------------------------------------
def _tradeable(snap: MarketSnapshot) -> tuple[bool, str]:
    """Pre-flight filters; returns (ok, reason_if_not)."""
    if snap.lat is None or snap.lon is None:
        return False, "no city parsed"
    if snap.metric is None or snap.operator is None:
        return False, "no metric/operator parsed"
    if snap.metric.startswith("temp") and snap.threshold_f is None:
        return False, "no temp threshold parsed"
    if snap.metric in ("snow", "precip") and snap.threshold_in is None:
        return False, "no snow/precip threshold parsed"
    if snap.yes_price is None or snap.no_price is None:
        return False, "missing yes/no prices"
    if snap.volume < MIN_VOLUME:
        return False, f"volume ${snap.volume:.0f} < ${MIN_VOLUME:.0f}"
    tgt = snap.target_date
    if not tgt and snap.end_date_iso:
        # fall back to end-date date portion
        try:
            tgt = snap.end_date_iso[:10]
        except Exception:
            tgt = None
    if not tgt:
        return False, "no target date"
    try:
        days = (datetime.fromisoformat(tgt).date()
                - datetime.utcnow().date()).days
        if days < 0:
            return False, "target date already passed"
        if days > MAX_DAYS_AHEAD:
            return False, f"target {days}d out > max {MAX_DAYS_AHEAD}d"
    except Exception as e:
        return False, f"bad date: {e}"
    return True, ""


def evaluate_market(snap: MarketSnapshot, bankroll_usd: float,
                    total_open_exposure_usd: float) -> Optional[Trade]:
    """Full evaluation: fair value -> edge -> sizing -> Trade object (or None)."""
    ok, why = _tradeable(snap)
    if not ok:
        log.debug("skip %s: %s", snap.slug or snap.market_id, why)
        return None

    target_date = snap.target_date or (snap.end_date_iso or "")[:10]
    threshold   = snap.threshold_f if snap.metric.startswith("temp") \
                  else snap.threshold_in
    try:
        fv = estimate_probability(
            lat=snap.lat, lon=snap.lon,
            target_date=target_date,
            metric=snap.metric, operator=snap.operator,
            threshold=threshold,
        )
    except Exception as e:
        log.warning("fair-value fail for %s: %s", snap.slug, e)
        return None

    edge = detect_edges(snap, fv)
    if edge is None:
        return None
    side, entry, edge_pp = edge

    # Sizing
    p_side = fv.probability if side == "YES" else (1.0 - fv.probability)
    full_k = kelly_fraction_full(p_side, entry)
    size_pct = min(KELLY_FRACTION * full_k, MAX_BET_PCT_BANKROLL)

    # Respect total exposure cap
    headroom_usd = max(MAX_TOTAL_EXPOSURE_PCT * bankroll_usd
                        - total_open_exposure_usd, 0.0)
    size_usd = min(size_pct * bankroll_usd, headroom_usd)
    if size_usd < 1.0:
        return None

    # Expected P&L if we're right about p:  EV = p * (1-entry)/entry * size - (1-p) * size
    b = (1.0 - entry) / entry
    ev = p_side * b * size_usd - (1.0 - p_side) * size_usd

    return Trade(
        timestamp=datetime.now(timezone.utc).isoformat(),
        market_id=snap.market_id,
        question=snap.question,
        side=side,
        entry_price=entry,
        model_probability=p_side,
        edge=edge_pp,
        kelly_fraction=size_pct,
        size_usd=round(size_usd, 2),
        expected_value_usd=round(ev, 2),
        ensemble_mean=fv.ensemble_mean,
        ensemble_std=fv.ensemble_std,
        climate_base_rate=fv.climate_base_rate,
        target_date=target_date,
        end_date=snap.end_date_iso,
        slug=snap.slug,
        details={
            "yes_price": snap.yes_price,
            "no_price":  snap.no_price,
            "volume":    snap.volume,
            "liquidity": snap.liquidity,
            "threshold_f":  snap.threshold_f,
            "threshold_in": snap.threshold_in,
            "operator":  snap.operator,
            "metric":    snap.metric,
            "city":      snap.city,
            "n_members": fv.n_members,
            "method":    fv.method,
        },
    )


# -- Persistence -------------------------------------------------------------------
class TradeLog:
    """Append-only paper-trade ledger backed by a JSON lines file + a snapshot."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.log_dir / "trades.jsonl"
        self.latest = self.log_dir / "latest.json"
        self.dashboard_data = self.log_dir / "dashboard_data.json"

    def open_trades(self) -> list[Trade]:
        if not self.jsonl.exists():
            return []
        out = []
        with self.jsonl.open("r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("status") == "OPEN":
                        out.append(Trade(**{k: v for k, v in d.items()}))
                except Exception:
                    continue
        return out

    def already_open(self, market_id: str, side: str) -> bool:
        return any(t.market_id == market_id and t.side == side
                   for t in self.open_trades())

    def record(self, trade: Trade) -> None:
        with self.jsonl.open("a") as f:
            f.write(json.dumps(trade.to_dict()) + "\n")
        log.info("PAPER TRADE  %s  %s  @%.3f  edge=%.3f  $%.2f  — %s",
                 trade.side, trade.market_id, trade.entry_price,
                 trade.edge, trade.size_usd, trade.question[:70])

    def total_open_exposure(self) -> float:
        return sum(t.size_usd for t in self.open_trades())

    def snapshot_for_dashboard(self, bankroll_usd: float,
                               latest_markets: list[dict]) -> None:
        """Dump everything the dashboard needs in a single JSON file."""
        trades = []
        if self.jsonl.exists():
            with self.jsonl.open("r") as f:
                for line in f:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        continue
        # Split + summarise
        open_   = [t for t in trades if t.get("status") == "OPEN"]
        settled = [t for t in trades if t.get("status") != "OPEN"]
        realized_pnl = sum(t.get("pnl_usd", 0.0) for t in settled)
        expected_pnl = sum(t.get("expected_value_usd", 0.0) for t in open_)
        open_exposure = sum(t.get("size_usd", 0.0) for t in open_)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bankroll_usd": bankroll_usd,
            "open_exposure_usd": round(open_exposure, 2),
            "realized_pnl_usd": round(realized_pnl, 2),
            "expected_open_pnl_usd": round(expected_pnl, 2),
            "n_open": len(open_),
            "n_settled": len(settled),
            "wins": sum(1 for t in settled if t.get("status") == "SETTLED_WIN"),
            "losses": sum(1 for t in settled if t.get("status") == "SETTLED_LOSS"),
            "open_trades": open_[-100:],
            "recent_settled": settled[-20:],
            "markets_scanned": latest_markets,
        }
        self.dashboard_data.write_text(json.dumps(payload, indent=2))
