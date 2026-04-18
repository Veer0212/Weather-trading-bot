# Polymarket Weather Bot

A paper-trading (or live-ready) bot that hunts mispriced weather markets on Polymarket by comparing crowd-implied probabilities to a 139-member multi-model ensemble forecast blended with 15-year NOAA climate base rates.

## The 30-second pitch

Polymarket weather markets — "will NYC hit 75°F on April 25?", "will it snow 2+ inches in Chicago on Jan 15?" — are often priced by vibes. The ground truth is free public NOAA data that anyone can pull in real time. This bot pulls both, computes fair value, and logs every bet where the edge exceeds 6 percentage points using fractional Kelly sizing.

## Files

```
polymarket_weather_bot/
├── README.md               # this file
├── SETUP_GUIDE.md          # zero-to-running in 5 minutes + live-trading setup
├── requirements.txt        # numpy + requests (that's it)
│
├── polymarket_client.py    # Gamma/CLOB API wrapper + question parser
├── weather_forecast.py     # Open-Meteo ensemble + NOAA ERA5 climate prior
├── strategy.py             # edge detection + Kelly sizing + trade log
├── run.py                  # main loop
│
├── dashboard.html          # auto-refreshing live view of trades + P&L
└── logs/                   # created at runtime
    ├── bot.log             # rolling log
    ├── trades.jsonl        # append-only trade ledger
    └── dashboard_data.json # dashboard payload
```

## Quick start

```bash
pip install -r requirements.txt

# Terminal A — run the bot
python3 run.py --bankroll 1000 --interval 180

# Terminal B — serve the dashboard
python3 -m http.server 8787
```

Open **http://localhost:8787/dashboard.html**.

## Strategy in one paragraph

For each active weather market on Polymarket, parse the question into `(city, metric, operator, threshold, target_date)`. Fetch 139 ensemble members (GFS + ECMWF + ICON + GEM) from Open-Meteo. Count the fraction of members triggering the event — that's the empirical probability. Blend with a parametric normal CDF when the empirical saturates. Layer on a 15-year ERA5 climate base rate with horizon-dependent weight (far-out forecasts lean more on climatology). Apply a small calibration shrinkage. Compare to the market's current YES/NO prices. If the edge exceeds 6pp and the market has at least $500 volume, log a fractional-Kelly paper trade.

## Risk controls

| Control | Value |
|---|---|
| Min edge to trade | 6pp |
| Min market volume | $500 |
| Max forecast horizon | 10 days |
| Kelly fraction | 0.25 (quarter Kelly) |
| Max bet size | 5% of bankroll |
| Max total open exposure | 60% of bankroll |
| Price floor / ceiling | 3¢ / 97¢ |

## Going live

See **SETUP_GUIDE.md** section 2. TL;DR: you need a Polymarket account with USDC on Polygon, and to replace the paper-log call in `strategy.py` with a `py-clob-client` order placement. Do not do this until you have at least 3 days of paper-trading data you trust.

## What could go wrong

- **Resolution source mismatch.** Polymarket NYC temperature markets resolve on NWS Central Park (KNYC). Open-Meteo uses the nearest grid point, which is extremely close but not identical. For edge cases (threshold within 1°F), this can flip outcomes.
- **Ensemble overconfidence.** When all 139 members agree on the direction, the bot flags the market as near-certain. In reality, real-world black swans (unexpected storm fronts) happen. The parametric fallback + calibration shrinkage partially protect against this; Kelly fractional sizing finishes the job.
- **Thin liquidity.** At $500 volume, your bet itself can move the market. The bot does not currently check depth-of-book before sizing. For live trading, add a check in `polymarket_client.get_orderbook` to ensure fill price within 1 tick of quoted price.
- **Polymarket market structure changes.** The parser is pattern-based and will miss new question formats. Monitor the "markets scanned" count on the dashboard.

## Credits

- **Open-Meteo** — free multi-model ensemble forecast API (no key).
- **Polymarket Gamma/CLOB** — public market data endpoints.
- Strategy design informed by published research on prediction-market arbitrage (Quantpedia, arxiv 2508.03474).
