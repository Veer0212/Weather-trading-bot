# Polymarket Weather Bot — Setup Guide

This guide walks through everything needed to run the bot, starting from zero. The bot ships in **paper-trading mode** by default — that works with zero setup beyond Python. The wallet / API section below is only required if you later flip to live trading.

## 1. Run the paper-trade bot (5 minutes, zero wallet needed)

```bash
cd polymarket_weather_bot
python3 -m pip install -r requirements.txt

# In terminal A: run the bot
python3 run.py --bankroll 1000 --interval 180

# In terminal B: serve the dashboard
python3 -m http.server 8787
```

Then open **http://localhost:8787/dashboard.html** — the dashboard auto-refreshes every 60 seconds, reads the bot's `logs/dashboard_data.json`, and shows open paper positions, markets scanned, and rolling P&L.

That is it. No wallet, no API key, no USDC. The bot will discover live Polymarket weather markets, pull ensemble forecasts from Open-Meteo, find mispricings, and log virtual trades.

## 2. Going live (optional — only after you trust the strategy)

**Polymarket is geoblocked from the United States.** If you are accessing from a restricted jurisdiction, do not proceed. The rest of this section assumes you are eligible.

### 2.1 Create a Polymarket account
1. Go to https://polymarket.com and connect a wallet. The easiest path is to use **Magic** (email-based wallet) during signup. You can also connect MetaMask.
2. Complete on-ramp: deposit USDC onto Polygon. Minimum useful bankroll is ~$50 but the bot's Kelly sizing assumes $200+.

### 2.2 Export your wallet private key
For Magic wallets, go to Settings → Export Private Key.
For MetaMask, Account Details → Show Private Key.

**Treat this like a password. Never commit it to git.** Set it as an env var:

```bash
export POLYMARKET_PRIVATE_KEY="0x...."
```

### 2.3 Install the live-trading dependencies

```bash
pip install py-clob-client web3
```

### 2.4 Derive API credentials
Polymarket's CLOB uses deterministic API keys derived from your wallet signature:

```python
from py_clob_client.client import ClobClient
client = ClobClient("https://clob.polymarket.com",
                    key=os.environ["POLYMARKET_PRIVATE_KEY"],
                    chain_id=137)
creds = client.create_or_derive_api_creds()
print(creds)   # apiKey / secret / passphrase — store these
```

### 2.5 Flip the bot to live mode
You will need to extend the bot with an order-placement function. A skeleton `polymarket_trader.py` is included as `TODO` scaffolding — it hooks into `strategy.evaluate_market` in place of the paper-log. Before enabling live trading:

1. **Set a hard daily loss limit in `run.py`** (e.g. kill switch at -10% of bankroll).
2. **Dry-run for 3 days minimum in paper mode** with real market data. Inspect every trade the bot would have made.
3. **Start small.** Flip live mode with a $20–50 bankroll first.
4. **Verify resolution source matches.** For each market you trade, read the market's resolution criteria in Polymarket's UI and confirm your data source (Open-Meteo → NOAA NWS) matches. Most NYC temperature markets resolve on NWS Central Park (KNYC) observations; Open-Meteo is calibrated to this station.

## 3. How the strategy works (the one-pager)

**The edge:** Polymarket weather markets are often priced by retail sentiment ("it felt cold yesterday, so no snow") while the ground truth is public NOAA data that anyone can access in real time. The bot exploits this gap.

**Fair value pipeline:**
1. Pull 139+ ensemble forecast members from Open-Meteo (GFS + ECMWF + ICON + GEM).
2. Count the fraction of members where the event threshold triggers → empirical probability.
3. If the empirical is saturated (0 or 1), blend with parametric normal CDF using ensemble mean/std.
4. Pull 15-year climate base rate from NOAA ERA5 reanalysis for the same calendar day.
5. Blend ensemble + climate (horizon-weighted — more climate weight for far-out forecasts).
6. Apply 2% shrinkage toward 0.5 as calibration smoothing.

**Edge detection:**
- Long YES if `model_p - market_yes_price ≥ 6pp`
- Long NO if `(1 - model_p) - market_no_price ≥ 6pp`

**Sizing (fractional Kelly):**
```
size% = 0.25 × [(b · p − q) / b], capped at 5% of bankroll
```
where `b = (1 − price) / price`.

**Risk controls:**
- Minimum market volume $500 (liquidity)
- Max 10-day forecast horizon (accuracy degrades)
- Never bet above 97¢ or below 3¢ (dead money or resolution risk)
- Max 60% total open exposure
- Deduplication — never double up on (market, side)

## 4. What to monitor

Watch the dashboard for:

- **Realized P&L** once markets start settling — should trend up if the edge is real.
- **Average edge on winners vs losers** — winners should have ≥8pp edge; if losers average ≥10pp edge you may have a calibration problem.
- **Markets scanned vs parsed** — if parse rate drops below ~40% of weather markets, the question parser in `polymarket_client._parse_question` needs new cases added.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard shows "waiting for first cycle" forever | Make sure `python3 run.py` is running in another terminal. The bot writes `logs/dashboard_data.json` at the end of each cycle. |
| `fetch_active_weather_markets` returns 0 markets | Weather markets come and go. Drop to `min_volume=0` in `run.py:one_cycle` to see everything. |
| Open-Meteo returns HTTP 429 | Back off. Non-commercial tier is ~10k req/day; bot should do <500/day. |
| Parser skips markets | Add test cases to the unit tests and extend the regex in `polymarket_client._parse_question`. |
