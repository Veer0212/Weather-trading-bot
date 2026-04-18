"""
polymarket_client.py
--------------------
Thin read-only wrapper around Polymarket's Gamma + CLOB APIs for weather-market
discovery and orderbook inspection. No wallet required for this module.

Endpoints:
  Gamma  : https://gamma-api.polymarket.com   (market metadata)
  CLOB   : https://clob.polymarket.com        (orderbooks, tick prices)

For order PLACEMENT, see `polymarket_trader.py` (stub included; requires py-clob-client
+ a funded Polygon wallet with USDC). Paper-trading mode doesn't touch it.
"""
from __future__ import annotations

import re
import time
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

log = logging.getLogger("polymarket")

# -- City coord registry used by the question parser ------------------------------
# Open-Meteo honours lat/lon; these are the canonical airport/city points used by
# most Polymarket weather markets (resolution source = local NWS station).
CITY_COORDS: dict[str, tuple[float, float]] = {
    "nyc":             (40.7794, -73.9690),   # Central Park (NWS KNYC)
    "new york":        (40.7794, -73.9690),
    "new york city":   (40.7794, -73.9690),
    "los angeles":     (33.9381, -118.3889),  # LAX area (KLAX)
    "la":              (33.9381, -118.3889),
    "chicago":         (41.9742, -87.9073),   # O'Hare (KORD)
    "miami":           (25.7933, -80.2906),   # MIA
    "boston":          (42.3656, -71.0096),   # BOS
    "dallas":          (32.8998, -97.0403),   # DFW
    "denver":          (39.8617, -104.6732),  # DEN
    "atlanta":         (33.6407, -84.4277),   # ATL
    "seattle":         (47.4502, -122.3088),  # SEA
    "philadelphia":    (39.8744, -75.2424),   # PHL
    "phoenix":         (33.4342, -112.0116),  # PHX
    "houston":         (29.9844, -95.3414),   # IAH
    "san francisco":   (37.6213, -122.3790),  # SFO
    "washington":      (38.9445, -77.4558),   # IAD
    "dc":              (38.9445, -77.4558),
    "las vegas":       (36.0840, -115.1537),  # LAS
}


# -- Data classes ------------------------------------------------------------------
@dataclass
class MarketSnapshot:
    """Parsed + enriched view of a single Polymarket weather market."""
    market_id: str
    question: str
    slug: str
    end_date_iso: Optional[str]
    yes_price: Optional[float]
    no_price: Optional[float]
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    volume: float
    liquidity: float
    # Parsed semantic bits:
    city: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    metric: Optional[str]               # 'temp_high' | 'snow' | 'precip' | 'temp_low'
    operator: Optional[str]             # 'gte' | 'lte' | 'eq_range'
    threshold_f: Optional[float]        # Fahrenheit threshold (temp)
    threshold_in: Optional[float]       # inches threshold (snow/precip)
    target_date: Optional[str]          # YYYY-MM-DD in local city timezone
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# -- HTTP helpers ------------------------------------------------------------------
def _get(url: str, params: Optional[dict] = None, tries: int = 3,
         backoff: float = 0.6) -> Any:
    last_err: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": "polymarket-weather-bot/1.0"})
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:          # network hiccup
            last_err = e
        time.sleep(backoff * (i + 1))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last_err}")


# -- Market discovery --------------------------------------------------------------
WEATHER_KEYWORDS = (
    "temperature", "temp ", "weather", "snow", "rain", "hurricane", "storm",
    "precipitation", "precip", "heat", "cold", "freeze", "blizzard", "tornado",
    "fahrenheit", "celsius", "degrees",
)

def fetch_active_weather_markets(min_volume: float = 0.0,
                                  max_pages: int = 20,
                                  page_size: int = 100) -> list[MarketSnapshot]:
    """Page through Gamma /markets, keep only live weather markets."""
    out: list[MarketSnapshot] = []
    seen: set[str] = set()
    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": page_size,
            "offset": page * page_size,
            "order": "volume",
            "ascending": "false",
        }
        try:
            batch = _get(f"{GAMMA}/markets", params=params)
        except Exception as e:
            log.warning("Gamma markets fetch failed p%d: %s", page, e)
            break
        if not batch:
            break
        added_this_page = 0
        for m in batch:
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            q = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            blob = q + " " + slug
            if not any(kw in blob for kw in WEATHER_KEYWORDS):
                continue
            try:
                snap = _parse_market(m)
            except Exception as e:
                log.debug("Skip unparseable market %s: %s", mid, e)
                continue
            if snap.volume < min_volume:
                continue
            out.append(snap)
            added_this_page += 1
        if len(batch) < page_size:
            break
    return out


def _parse_market(m: dict) -> MarketSnapshot:
    """Extract yes/no prices + parse the question text into structured params."""
    # outcomePrices / outcomes / clobTokenIds arrive as JSON-encoded strings.
    def _dec(x: Any) -> list:
        if isinstance(x, list):
            return x
        if isinstance(x, str):
            try:
                return json.loads(x)
            except Exception:
                return []
        return []

    outcomes = [str(o).lower() for o in _dec(m.get("outcomes"))]
    prices   = [float(p) for p in _dec(m.get("outcomePrices")) or []]
    tokens   = [str(t) for t in _dec(m.get("clobTokenIds")) or []]

    yes_p = no_p = None
    yes_tok = no_tok = None
    if outcomes and prices:
        for i, name in enumerate(outcomes):
            if name in ("yes", "true") and i < len(prices):
                yes_p = prices[i]
                yes_tok = tokens[i] if i < len(tokens) else None
            elif name in ("no", "false") and i < len(prices):
                no_p = prices[i]
                no_tok = tokens[i] if i < len(tokens) else None

    q = m.get("question") or ""
    city, lat, lon, metric, op, thr_f, thr_in, date = _parse_question(q)

    return MarketSnapshot(
        market_id=str(m.get("id") or m.get("conditionId") or ""),
        question=q,
        slug=m.get("slug") or "",
        end_date_iso=m.get("endDate") or m.get("endDateIso"),
        yes_price=yes_p, no_price=no_p,
        yes_token_id=yes_tok, no_token_id=no_tok,
        volume=float(m.get("volume") or 0.0),
        liquidity=float(m.get("liquidity") or 0.0),
        city=city, lat=lat, lon=lon,
        metric=metric, operator=op,
        threshold_f=thr_f, threshold_in=thr_in,
        target_date=date,
        raw=m,
    )


# -- Question parser ---------------------------------------------------------------
_TEMP_RE = re.compile(
    r"(?:highest\s+temperature|high\s+temperature|temp(?:erature)?|temp high|"
    r"low\s+temperature|temp low|overnight\s+low|\bhigh\b|\blow\b)"
    r".{0,80}?"
    r"(?:reach|exceed|above|below|at\s+least|greater\s+than|less\s+than|over|under|hit)?"
    r"\s*"
    r"(\d{2,3})\s*(?:°|degrees?)?\s*(f|fahrenheit|c|celsius)?",
    re.IGNORECASE,
)
# Fallback: just "NN F" or "NN°F" or "NN degrees" anywhere.
# Accepts "95F", "95°F", "95 degrees", "95°", etc.
_TEMP_FALLBACK_RE = re.compile(
    r"(\d{2,3})\s*(?:°\s*(f|c)?|degrees?\s*(f|c|fahrenheit|celsius)?|(f|c)\b)",
    re.IGNORECASE,
)
_SNOW_RE = re.compile(
    r"(?:snow.{0,80}?(\d+(?:\.\d+)?)\s*(?:inches?|in\b|\"|inch|\+)"
    r"|(\d+(?:\.\d+)?)\+?\s*(?:inches?|in\b|\"|inch).{0,40}?snow)",
    re.IGNORECASE,
)
_PRECIP_RE = re.compile(
    r"(?:(?:rain|precipitation|precip).{0,80}?(\d+(?:\.\d+)?)\s*(?:inches?|in\b|\"|inch|mm)"
    r"|(\d+(?:\.\d+)?)\s*(?:inches?|in\b|\"|inch|mm).{0,40}?(?:rain|precipitation|precip))",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["january","february","march","april","may","june","july","august",
     "september","october","november","december"], start=1)}


def _parse_question(q: str) -> tuple[
    Optional[str], Optional[float], Optional[float],
    Optional[str], Optional[str],
    Optional[float], Optional[float],
    Optional[str],
]:
    ql = q.lower()

    # City match (longest first to prefer "new york city" over "new york")
    city = None
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if name in ql:
            city = name
            break
    lat = lon = None
    if city:
        lat, lon = CITY_COORDS[city]

    # Metric
    metric: Optional[str] = None
    op: Optional[str] = None
    thr_f: Optional[float] = None
    thr_in: Optional[float] = None

    if "snow" in ql:
        metric = "snow"
        m = _SNOW_RE.search(q)
        if m:
            thr_in = float(m.group(1) or m.group(2))
        op = _infer_operator(ql)
    elif "rain" in ql or "precip" in ql:
        metric = "precip"
        m = _PRECIP_RE.search(q)
        if m:
            thr_in = float(m.group(1) or m.group(2))
        op = _infer_operator(ql)
    elif ("temp" in ql or "degree" in ql or "fahrenheit" in ql
          or "celsius" in ql
          or re.search(r"\b(overnight|high|low|lows|highs)\b", ql)
          or re.search(r"\d+\s*°", q)
          or re.search(r"\d+\s*(?:f|c)\b", ql)):
        # "low temperature" / "overnight low" → temp_low, else default to high.
        # IMPORTANT: the word "below" contains "low" so we must look for token
        # boundaries, not substring membership.
        is_low = bool(re.search(r"\b(low|lows|overnight|minimum|min\s*temp)\b", ql))
        metric = "temp_low" if is_low else "temp_high"
        m = _TEMP_RE.search(q) or _TEMP_FALLBACK_RE.search(q)
        if m:
            val = float(m.group(1))
            # Collapse all unit capture groups to one.
            unit = ""
            for i in range(2, m.lastindex + 1 if m.lastindex else 2):
                try:
                    if m.group(i):
                        unit = m.group(i).lower()
                        break
                except (IndexError, Exception):
                    pass
            if unit.startswith("c"):
                val = val * 9.0 / 5.0 + 32.0
            thr_f = val
        op = _infer_operator(ql)

    # Target date -- prefer explicit month/day in the question, else the market's
    # endDate (caller can override using the market metadata).
    date_iso: Optional[str] = None
    dm = _DATE_RE.search(q)
    if dm:
        mon = _MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        yr = int(dm.group(3)) if dm.group(3) else datetime.now(timezone.utc).year
        date_iso = f"{yr:04d}-{mon:02d}-{day:02d}"

    return city, lat, lon, metric, op, thr_f, thr_in, date_iso


def _infer_operator(ql: str) -> str:
    if any(w in ql for w in (" at least ", " above ", " over ", " exceed",
                              " greater than ", " >= ", " hit ", " or more",
                              " higher than ")):
        return "gte"
    if any(w in ql for w in (" below ", " under ", " less than ", " <= ",
                              " or less", " lower than ", " no more than ")):
        return "lte"
    # Default for "will it snow X inches" style questions
    return "gte"


# -- Orderbook / live price --------------------------------------------------------
def get_orderbook(token_id: str) -> dict:
    """Return the CLOB orderbook for a single outcome token."""
    return _get(f"{CLOB}/book", params={"token_id": token_id})


def get_last_trade_price(token_id: str) -> Optional[float]:
    try:
        data = _get(f"{CLOB}/prices-history",
                    params={"market": token_id, "interval": "1h", "fidelity": 60})
        pts = data.get("history") or []
        if pts:
            return float(pts[-1]["p"])
    except Exception:
        return None
    return None


# -- CLI smoke test ----------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    markets = fetch_active_weather_markets(min_volume=0.0)
    print(f"Found {len(markets)} live weather markets")
    for s in markets[:15]:
        print("-" * 60)
        print(f"{s.question}")
        print(f"  city={s.city} metric={s.metric} op={s.operator} "
              f"thr_f={s.threshold_f} thr_in={s.threshold_in} date={s.target_date}")
        print(f"  yes={s.yes_price} no={s.no_price} vol=${s.volume:,.0f}")
