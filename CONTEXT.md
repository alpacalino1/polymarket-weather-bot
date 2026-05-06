# Polymarket Weather Bot → BTC Barrier Monitor — Context Dump

## Project location
/home/anto/polymarket-weather-bot/

## What was built
Started as a Polymarket weather trading bot. Evolved into a general-purpose
crypto/Polymarket autopilot. Final deliverable: standalone BTC Barrier probability
monitor.

## Key files

### Active / main tools
| File | Purpose | Run with |
|------|---------|----------|
| `btc_barrier.py` | **Main tool.** Live BTC vol + barrier probabilities + trade signals | `python btc_barrier.py` or `--live` |
| `live.py` | Live BTC-Polymarket dashboard (TUI, uses Gamma + CLOB APIs) | `python live.py` |
| `snipe.py` | BTC sniper — watches 1 Polymarket market, paper trades edges | `python snipe.py --live` |
| `run_btc.py` | Full BTC arb scanner (spread arb + threshold model) | `python run_btc.py --trade` |
| `demo.py` | Weather market demo with seed markets + exit management | `python demo.py` |
| `main.py` | Original CLI entry point (scan/paper/live/autopilot modes) | `python main.py` |

### Supporting modules
| Path | Purpose |
|------|---------|
| `feed/coingecko.py` | BTC spot + OHLCV from CoinGecko + Binance (free, no keys) |
| `strategies/btc_threshold.py` | GBM barrier probability strategy |
| `strategies/temperature.py` | Weather temperature threshold strategy |
| `strategies/precipitation.py` | Weather rain/snow strategy |
| `strategies/spread_arb.py` | CLOB orderbook spread arb scanner |
| `polymarket/client.py` | Gamma API (market discovery) + CLOB client (trading) |
| `polymarket/markets.py` | NLP parser for market questions |
| `autopilot/db.py` | SQLite paper trading DB (trades, portfolio, stats) |
| `autopilot/runner.py` | Full autopilot loop (entries + exits) |
| `weather/open_meteo.py` | Open-Meteo weather API (global, free) |
| `weather/nws.py` | NWS weather API (US, free) |
| `weather/radar.py` | RainViewer radar (global, free) |
| `config.py` | Config from env vars |

## APIs used (all free, zero keys)
- CoinGecko public API — BTC spot, OHLCV
- Binance public API — BTC ticker, klines
- Polymarket Gamma API — market discovery, prices
- Polymarket CLOB API — orderbook depth
- Open-Meteo — weather forecasts
- NWS — US weather
- RainViewer — global radar

## The only auth needed
`POLYMARKET_PRIVATE_KEY` in `.env` — only if placing real orders via CLOB.
Everything else runs without any keys.

## Virtual env
```
cd ~/polymarket-weather-bot && source venv/bin/activate
```

## What works
- ✅ btc_barrier.py: snapshot + live TUI with color-coded barrier table + trade signals
- ✅ CoinGecko feed: live BTC price + historical volatility
- ✅ GBM first-passage barrier probability math (corrected — works for both up/down)
- ✅ Polymarket Gamma parsing: correctly handles JSON string outcomes/prices/tokens
- ✅ AutopilotDB: SQLite trade journal, portfolio tracking, P&L stats
- ✅ CLOB spread scanner: works but rarely finds arbs (markets efficient)
- ✅ Weather strategies: work against real Open-Meteo data

## What doesn't work / known issues
- Polymarket has very few BTC markets (1-28 depending on search, most low volume)
- Pure CLOB spread arb (YES+NO < $1) is extremely rare — market makers are fast
- Weather markets basically don't exist on Polymarket
- CoinGecko free tier rate limits at ~10-30 calls/min → keep intervals ≥15s
- The live TUI mode (`--live`) uses Rich Live with screen=True — needs real terminal
- The `d` command alias doesn't exist — use `cd`

## Real findings from live runs
- Polymarket top volume: sports (NBA, soccer $3-5K/daily), geopolitics ($1-2K), crypto ($70-130K)
- BTC barrier model at σ=83% shows ±5% in 30d at 82-85% probability → highly actionable
- The "BTC $1M before GTA VI" market at 49% YES is a joke — model says <0.5%
- BTC dips to $65K in May: model 27% vs market 6.5% — real divergence

## Current state
The BTC barrier tool is the cleanest deliverable. Polymarket integration is
complete but dormant (no markets worth trading right now). Weather stuff is
a dead end (no weather markets exist).

## Next steps / ideas
1. Run `python btc_barrier.py --live` in a real terminal — that's the main tool
2. If you get a Polymarket private key, wire it up for live CLOB trading
3. Extend barrier model with drift estimation from historical data
4. Add options-implied vol comparison (Deribit API, free)
5. Build Discord/Telegram alerts when signals cross thresholds
6. Add multi-asset support (ETH, SOL)

## Repo
https://github.com/alpacalino1/polymarket-weather-bot
