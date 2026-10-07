# Favorite Hunter

A Polymarket scanner that asks one question:

> **Which high-probability Polymarket outcome is priced *lower* than its realistic probability by enough to justify the risk?**

It finds outcomes whose **executable** price is 0.80–0.98, estimates their true probability from **external** data (never from the Polymarket price), and keeps only those where

```
estimated probability − (executable price + taker fee) ≥ minimum edge
```

**Paper trading only.** No code path places, signs or cancels a real order. Real-money execution (Phase 8) is intentionally not built.

---

## Status

| Phase | What | Built | Unit-tested | Verified on live data |
|---|---|---|---|---|
| 1 | Polymarket client + 80–98¢ scanner | ✅ | ✅ | ⏳ run `favorite-hunter verify --phase 1` |
| 2 | Price / order-book history (SQLite) | ✅ | ✅ | ⏳ |
| 3 | Paper trading (depth-aware fills, settlement) | ✅ | ✅ | ⏳ |
| 4 | Analytics + historical backtest | ✅ | ✅ | ⏳ |
| 5 | External probability sources | ✅ | ✅ | ⏳ |
| 6 | Favorite Edge Score + dark dashboard | ✅ | ✅ | ⏳ |
| 7 | Telegram / Discord alerts | ✅ | ✅ | ⏳ |
| 8 | Real trading | ❌ by design | – | – |

**Why ⏳:** the environment this was built in blocks outbound access to Polymarket and every data provider (HTTP 403 from the egress proxy), so nothing could be tested against live data there. API formats were taken from Polymarket's current official SDK ([`Polymarket/py-sdk`](https://github.com/Polymarket/py-sdk)); everything else is covered by 179 tests with hand-computed expected values. `favorite-hunter verify` runs all seven phases against the real APIs in one command — run it first.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

favorite-hunter verify            # live check of all 7 phases (temporary DB)
favorite-hunter scan              # one scan: favorites, estimates, edge, status
favorite-hunter dashboard --run   # dark dashboard on http://127.0.0.1:8050 + scan loop
```

Other commands: `run` (scan loop without UI), `trades`, `settle`, `report`, `backtest`, `history <market_id>`, `alerts-test`, `db-stats`. Add `--help` to any of them.

### Network access needed

| Host | Used for | Required |
|---|---|---|
| `gamma-api.polymarket.com`, `clob.polymarket.com`, `data-api.polymarket.com` | markets, order books, fees, price history, resolutions | yes |
| `data-api.binance.vision`, `api.exchange.coinbase.com`, `api.kraken.com`, `www.deribit.com` | crypto spot, candles, implied vol | crypto engine |
| `site.api.espn.com` | live scores, clock, red cards, pregame line | sports engine |
| `api.the-odds-api.com` | sportsbook odds (needs `ODDS_API_KEY`) | optional |
| `api.elections.kalshi.com` | cross-market prices for mapped markets | optional |
| `api.telegram.org`, `discord.com` | alerts | optional |

When a host is unreachable the bot shows **DATA UNAVAILABLE** with the reason. It never substitutes a value.

---

## How it works

```
Gamma markets ─► prefilter on cached quotes ─► CLOB order books ─► executable VWAP in [0.80, 0.98]?
      │                                                                    │
      └──────────── both outcomes of every market are checked ◄───────────┘
                                         │
             probability engines (external data only) ─► edge after fees ─► max executable size
                                         │
                confidence (0-100) ─► Favorite Edge Score (0-100) ─► filters ─► status
                                         │
         paper trade (TRADE) · baseline observation · history · alerts · dashboard · analytics
```

### Price and fees
- **Purchase price** = VWAP of buying the reference stake ($100) by walking the live ask book. Best ask is shown alongside; depth is never assumed unlimited.
- **Taker fee** per share = `rate × (p × (1 − p))^exponent`, from the market's `feeSchedule` or the CLOB `/clob-markets/{condition_id}` fee data (formula from Polymarket's SDK). If unknown, the worst published rate (7%) is assumed and flagged.
- Per candidate: purchase price, payout ($1), profit if correct, loss if wrong, break-even probability (= all-in cost), estimated probability, edge, EV per share, ROI, Kelly, risk/reward, and *one loss erases N wins*.

Example: price 0.90, fee 0.0045 (rate 5%), estimate 0.96 → cost 0.9045, edge +5.55pt, EV $0.0555/share, ROI 6.1%. At 0.94 vs 0.95 the edge is ~1pt: skipped.

### Probability engines
Each estimate carries its evidence, sources, a step-by-step calculation, an uncertainty, risks and loss scenarios.

| Engine | Data | Model |
|---|---|---|
| **Crypto** — above/below/between at a time, up/down windows, "reach"/"dip to" | Binance, Coinbase, Kraken spot; Binance candles; Deribit DVOL | Driftless log-price; lowest probability across realised/implied vol scenarios, Student-t(4) **and** normal returns, ±basis buffer; reflection principle for touch markets; already-touched barriers → `DATA_LAG` |
| **Sports** — moneyline, win/draw, totals, spreads | ESPN score, clock, period, red cards, pregame line; The Odds API | Basketball/football margin model; hockey/soccer Poisson goals fitted to the pregame moneyline (soccer red cards adjust rates); baseball innings model; de-vigged sportsbook consensus (Pinnacle preferred) |
| **Politics** | `manual_evidence.yaml`: polls, model forecasts, reporting, vote counts; Kalshi mappings | Sourced probabilities; vote-count model; Kalshi mid-price |
| **Manual** (any category) | `manual_evidence.yaml` | Sourced probability with expiry |

Independent estimates are combined by inverse-variance weighting; if they disagree by more than `conflict_threshold` (5pt) the result is **CONFLICTING SOURCES** and nothing trades. Everything without a source is **DATA UNAVAILABLE**. See [`manual_evidence.example.yaml`](manual_evidence.example.yaml).

### Confidence, Favorite Edge Score, filters
- **Confidence (0–100)** — edge relative to uncertainty, source quality, number of independent sources, time left, event certainty, liquidity, spread, depth, rule clarity. It is **not** a probability.
- **Favorite Edge Score (0–100)** — 30% probability edge, 20% source reliability, 15% time remaining, 15% liquidity, 10% spread, 10% event certainty (configurable).
- **Filters** (configurable): price band, min edge 4pt after fees, min ROI, edge still positive at estimate − 1σ, ask depth, spread, stale books, stale evidence, ambiguous rules, disputed resolution, confidence, executable size.
- **Statuses:** `TRADE`, `HOLDING`, `FILTERED` (edge but a risk filter failed, with reasons), `NO EDGE`, `CONFLICT`, `DATA UNAVAILABLE`.
- **Opportunity types:** `LIVE_EVENT_EDGE`, `EXTERNAL_ODDS_EDGE`, `NEAR_RESOLUTION_EDGE`, `DATA_LAG`, `THRESHOLD_EDGE`, `POLLING_EDGE`, `PRICE_DISLOCATION`, `ORDER_BOOK_EDGE`.
- **Time buckets:** `<1h`, `1-6h`, `6-24h`, `1-3d`, `3d+` (plus `past_end`). Sports use game start + typical game length.

### Paper trading
- **Model trades** open only on `TRADE`. Size: fixed stake or fractional Kelly, capped by max stake, the size that keeps the minimum edge, paper cash, per-event and total exposure. Fills walk the book and stop where marginal edge would fall below the minimum.
- **Baseline observations** record every in-band favorite once per market side and time bucket at a $100 simulated fill — what blindly buying favorites would do. They never touch the paper bankroll; they are the benchmark the model must beat.
- Settlement only on final payouts (`[1,0]`, `[0,1]`, or a `[0.5,0.5]` split); proposed/disputed resolutions wait.

### Analytics and backtest
`favorite-hunter report` (and the dashboard) break results down by entry range (0.80–0.85 … 0.97–0.98), category, time remaining, estimated edge, confidence, liquidity, volume and strategy: bets, win rate with 95% interval, average entry, required (break-even) vs actual win rate, expected win rate, calibration, ROI, PnL, max drawdown, largest loss, longest losing streak.

**Extreme favorites** (90¢+, 95¢+, 97¢+) get an explicit verdict — e.g. *average entry 0.95, required 95%, actual 94% → LOSING STRATEGY* — with a significance note. Under 30 settled bets the verdict is *INSUFFICIENT DATA*.

`favorite-hunter backtest` samples resolved markets' price history once per time bucket. It uses historical traded/marked prices plus an explicit slippage assumption, not executable asks, so treat it as optimistic.

### Dashboard
Dark, read-only (no order endpoints). Opportunities table sorted by EV with every requested column; clicking a market opens **why the bot thinks it is mispriced**: evidence, sources, calculation, live order book and simulated fill, confidence and score breakdowns, risks, what could cause a loss, and the full resolution rules. Plus paper trades, analytics with charts, and source health.

### Alerts
Telegram and/or Discord, in this format, for `TRADE`/`HOLDING` opportunities above the score and confidence thresholds. One alert per opportunity; re-alerts only after the cooldown and a material edge improvement.

```
POLYMARKET FAVORITE EDGE
Market: Will Team A win?   Side: YES   Ask: $0.870
Estimated Probability: 94.0%   Edge: +7.0% (after fees)   Expected ROI: 8.0%
Confidence: 91/100   Time Remaining: 42 minutes   Available Size: $180
Main Reason: Team A leads 2-0 in the 78th minute.
Risk: Possible comeback / match abandonment / resolution-rule issue
PAPER TRADE ONLY.
```

---

## Configuration
- `config.yaml` (copy [`config.example.yaml`](config.example.yaml)): price band, filters, fees, sizing, weights, alerts, data-source URLs.
- Environment only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, `ODDS_API_KEY`.
- `manual_evidence.yaml` (copy [`manual_evidence.example.yaml`](manual_evidence.example.yaml)): sourced polls, forecasts, vote counts, Kalshi mappings. Entries need `source` and `as_of`; expired entries are ignored.

## Limitations
- **Not yet run against live data** (see Status). Run `favorite-hunter verify` and fix anything it flags before trusting results.
- ESPN's scoreboard API is unofficial and can change; sports models use published league averages and ignore possession, base runners and empty nets.
- Politics needs manually entered, sourced data; there is no free reliable polling API.
- The backtest measures blind favorites at historical prices, not the probability model, and is optimistic about execution.
- A model is only as good as its inputs: start with small paper stakes and let the baseline comparison tell you whether the edge is real.

## Project layout
| Module | Role |
|---|---|
| `polymarket_client.py` | Gamma / CLOB / Data API (read-only) |
| `market_scanner.py` | favorites in the band, both outcomes |
| `orderbook_engine.py` | depth-walking fills, max executable size |
| `probability/` | `engine`, `crypto_engine`, `sports_engine`, `politics_engine`, evidence file, combination |
| `sources/` | Binance/Coinbase/Kraken/Deribit, ESPN, The Odds API, Kalshi |
| `edge_calculator.py` | EV, ROI, break-even, Kelly |
| `confidence.py`, `scoring.py`, `risk_engine.py` | confidence, Favorite Edge Score, filters/statuses |
| `paper_trader.py` | model trades, baselines, settlement |
| `database.py` | SQLite schema and history |
| `analytics.py`, `backtest.py`, `reporting.py` | performance analytics |
| `alerts.py` | Telegram / Discord |
| `dashboard/` | FastAPI API + static dark UI |
| `runner.py`, `cli.py`, `verify.py` | scan loop, commands, live verification |

```bash
pytest          # 179 tests
ruff check .    # lint (optional)
```
