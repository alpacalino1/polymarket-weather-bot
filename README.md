# 🌤️ Polymarket Weather Trading Bot

An AI-powered trading bot that finds weather-related prediction markets on [Polymarket](https://polymarket.com) and trades them using data from **free weather and radar APIs**.

## How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Polymarket     │     │  Weather APIs    │     │  Trading Bot    │
│  Gamma API      │────▶│  (Open-Meteo,    │────▶│  Strategies      │
│  (Market Scan)  │     │   NWS, Radar)    │     │  (Temp, Precip)  │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                                                          ▼
                                                 ┌─────────────────┐
                                                 │  Polymarket     │
                                                 │  CLOB API       │
                                                 │  (Place Orders) │
                                                 └─────────────────┘
```

1. **Scan**: Queries Polymarket's Gamma API for weather-related markets (temperature, rain, snow, storms, etc.)
2. **Parse**: Extracts location, date, threshold, and metric type from each market question
3. **Forecast**: Fetches weather data from Open-Meteo (global, free), NWS (US, free), and RainViewer (radar, free)
4. **Analyze**: Calculates the probability of the event occurring based on forecast data
5. **Trade**: When the model probability differs significantly from the market price, places a trade

## Features

- 🔍 **Market Discovery** — Automatically finds weather markets across 12+ search terms and tags
- 🌡️ **Temperature Strategy** — Models P(temp ≥ X) using forecast mean + ensemble uncertainty
- 🌧️ **Precipitation Strategy** — Uses Open-Meteo's precipitation probability + amount forecasts
- ❄️ **Snowfall Strategy** — Snow accumulation probability modeling
- 📡 **Radar Integration** — RainViewer global radar for short-term precipitation awareness
- 📊 **Rich Console Output** — Color-coded tables with signals, edges, and explanations
- 🛡️ **Risk Management** — Position sizing, max exposure, daily loss limits, cooldowns
- 🎯 **3 Modes** — scan-only, paper trading, live trading

## Free APIs Used

| API | Coverage | Data | Key Required? |
|-----|----------|------|---------------|
| [Open-Meteo](https://open-meteo.com) | Global | Forecast, ensemble, geocoding | **No** |
| [NWS](https://weather.gov) | US only | Forecast, alerts, hourly | **No** |
| [RainViewer](https://rainviewer.com) | Global | Radar composite, nowcast | **No** |

## Quick Start

### 1. Install dependencies

```bash
cd polymarket-weather-bot
pip install -r requirements.txt
```

### 2. Configure (optional)

```bash
cp .env.example .env
# Edit .env with your Polymarket private key (for live trading only)
```

### 3. Run in scan mode

```bash
# Just find opportunities (no trades)
python main.py

# Single scan and exit
python main.py --once
```

### 4. Paper trade (simulate)

```bash
python main.py --mode paper --once
```

### 5. Live trading (real USDC)

```bash
# Set POLYMARKET_PRIVATE_KEY in .env first!
python main.py --mode live
```

## CLI Options

```
Usage: python main.py [OPTIONS]

Options:
  -m, --mode [scan|paper|live]   Bot mode (default: scan)
  -1, --once                     Run a single scan and exit
  -i, --interval INTEGER         Scan interval in minutes (default: 15)
  -p, --max-position FLOAT       Max USDC per position (default: 100)
  -e, --min-edge FLOAT           Minimum edge to trade, e.g. 0.05 = 5%
  -c, --cities TEXT              Focus on specific cities (repeatable)
  --log-level [DEBUG|INFO|WARNING|ERROR]
  -h, --help                     Show this message
```

### Examples

```bash
# Scan for opportunities, 5-minute intervals
python main.py -i 5

# Paper trade, max $50 per position, focus on NYC
python main.py -m paper -p 50 -c "New York"

# Live trade with high-conviction signals only (10% edge)
python main.py -m live -e 0.10

# One-off scan for multiple cities
python main.py --once -c "Chicago" -c "Miami" -c "Seattle"
```

## Environment Variables

See `.env.example` for all options. Key ones:

| Variable | Required For | Description |
|----------|-------------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Live trading | Your Polymarket wallet private key |
| `MAX_POSITION_USDC` | Trading | Max USDC per position (default: 100) |
| `MIN_EDGE` | Trading | Minimum probability edge to trade (default: 0.05) |
| `BOT_MODE` | All | scan / paper / live |
| `FOCUS_LOCATIONS` | All | Pipe-separated cities e.g. `New York,NY\|Chicago,IL` |

### Getting Your Polymarket Private Key

1. Go to [Polymarket Settings → API Keys](https://polymarket.com/settings/api-keys)
2. Create a new API key and copy the private key
3. Set `POLYMARKET_PRIVATE_KEY` in your `.env` file

> ⚠️ **Security**: Never commit your private key. The `.env` file is gitignored.

## Market Types Supported

| Metric | Example Question | Strategy |
|--------|-----------------|----------|
| Temperature | "Will the high in NYC be ≥ 90°F on June 15?" | Temperature strategy (normal distribution model) |
| Rain | "Will it rain in Chicago on July 4?" | Precipitation probability (Open-Meteo PoP) |
| Snow | "Will Boston receive ≥ 2 inches of snow?" | Snowfall probability |
| Wind | "Will Miami wind exceed 50mph?" | Wind speed probability |
| Storms | "Will a hurricane make landfall..." | Radar + NWS alert integration |

## Strategy Logic

### Temperature Threshold Strategy

Given a forecast with:
- `μ` = forecast high temperature (°C)
- `σ` = ensemble uncertainty (°C)
- `T` = threshold temperature (°C)

P(temp ≥ T) = 1 − Φ((T − μ) / σ)

Where Φ is the standard normal CDF.

The edge = |P(temp ≥ T) − market_price|

- **Buy YES** when model probability > market price + min_edge
- **Buy NO** when model probability < market price − min_edge

### Precipitation Strategy

- **"Will it rain?"** → Directly uses Open-Meteo's `precipitation_probability_max`
- **"≥ X inches?"** → Models precipitation amount as normal distribution around forecast sum
- Position sizing scales with edge magnitude and parse confidence

## Risk Management

- Max position per market (default: $100 USDC)
- Max total exposure (default: $500 USDC)
- Max concurrent positions (default: 5)
- Max daily loss (default: $200 USDC) — bot stops trading if exceeded
- Market cooldown (default: 30 min) — don't re-check same market too often
- Min market volume filter (default: $1000 24h volume)
- Position sizes scaled by edge magnitude (bigger edge → bigger bet)

## Project Structure

```
polymarket-weather-bot/
├── main.py                  # Entry point, CLI, orchestration
├── config.py                # Configuration from env vars
├── requirements.txt         # Python dependencies
├── .env.example             # Example environment config
├── README.md                # This file
├── weather/
│   ├── __init__.py
│   ├── open_meteo.py        # Open-Meteo API (global, free)
│   ├── nws.py               # NWS API (US, free)
│   └── radar.py             # RainViewer + NWS radar (free)
├── polymarket/
│   ├── __init__.py
│   ├── client.py            # Gamma API + CLOB trading client
│   └── markets.py           # Market question parser (NLP)
├── strategies/
│   ├── __init__.py
│   ├── temperature.py       # Temperature threshold strategy
│   └── precipitation.py     # Rain/snow/wind strategies
└── utils/
    ├── __init__.py
    └── logger.py            # Logging setup
```

## License

MIT — Use at your own risk. Trading involves financial risk.
