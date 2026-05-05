#!/usr/bin/env python3
"""
Demo harness - seeds realistic weather markets and runs the full autopilot
against REAL weather data to show the bot making actual trades.

Usage:
    python demo.py
    python demo.py --cities "New York" "Chicago" "Miami" "Denver"
"""

import asyncio
import sys
import os
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from config import config
from autopilot import AutopilotDB, AutopilotRunner
from weather import OpenMeteoClient, RainViewerClient, fahrenheit_to_celsius
from strategies import TemperatureStrategy, PrecipitationStrategy
from polymarket.markets import parse_weather_market
from polymarket.client import PolymarketMarket

console = Console()

# ── Seed: realistic weather markets ──────────────────────────────────────

# These simulate what Polymarket weather markets actually look like.
# We set the market prices at "mispriced" levels so the bot finds edges.
SEED_MARKETS = [
    PolymarketMarket(
        id="seed-1",
        condition_id="seed-temp-nyc-1",
        question="Will the high temperature in New York City be ≥ 85°F on May 10, 2026?",
        slug="nyc-temp-may10",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.30, "No": 0.70},  # Market is skeptical, we'll see
        volume=25000,
        volume_24h=5000,
        tags=["weather", "temperature"],
        token_ids={"Yes": "tok-nyc-yes-1", "No": "tok-nyc-no-1"},
    ),
    PolymarketMarket(
        id="seed-2",
        condition_id="seed-temp-chicago-1",
        question="Will the high temperature in Chicago be ≥ 75°F on May 10, 2026?",
        slug="chicago-temp-may10",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.65, "No": 0.35},  # Overconfident?
        volume=18000,
        volume_24h=4000,
        tags=["weather", "temperature"],
        token_ids={"Yes": "tok-chi-yes-1", "No": "tok-chi-no-1"},
    ),
    PolymarketMarket(
        id="seed-3",
        condition_id="seed-rain-miami-1",
        question="Will it rain in Miami on May 10, 2026?",
        slug="miami-rain-may10",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.15, "No": 0.85},  # Very skeptical about rain
        volume=12000,
        volume_24h=3000,
        tags=["weather", "rain", "precipitation"],
        token_ids={"Yes": "tok-mia-yes-1", "No": "tok-mia-no-1"},
    ),
    PolymarketMarket(
        id="seed-4",
        condition_id="seed-temp-denver-1",
        question="Will the high temperature in Denver be ≥ 80°F on May 12, 2026?",
        slug="denver-temp-may12",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.40, "No": 0.60},
        volume=15000,
        volume_24h=3500,
        tags=["weather", "temperature"],
        token_ids={"Yes": "tok-den-yes-1", "No": "tok-den-no-1"},
    ),
    PolymarketMarket(
        id="seed-5",
        condition_id="seed-snow-denver-1",
        question="Will Denver receive ≥ 1 inch of snow on May 12, 2026?",
        slug="denver-snow-may12",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.08, "No": 0.92},  # Very low because it's May
        volume=8000,
        volume_24h=2000,
        tags=["weather", "snow"],
        token_ids={"Yes": "tok-den-snow-yes", "No": "tok-den-snow-no"},
    ),
    PolymarketMarket(
        id="seed-6",
        condition_id="seed-temp-la-1",
        question="Will the high temperature in Los Angeles be ≥ 80°F on May 11, 2026?",
        slug="la-temp-may11",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.55, "No": 0.45},  # Split
        volume=20000,
        volume_24h=5000,
        tags=["weather", "temperature"],
        token_ids={"Yes": "tok-la-yes-1", "No": "tok-la-no-1"},
    ),
    PolymarketMarket(
        id="seed-7",
        condition_id="seed-rain-seattle-1",
        question="Will it rain in Seattle on May 11, 2026?",
        slug="seattle-rain-may11",
        outcomes=["Yes", "No"],
        outcome_prices={"Yes": 0.50, "No": 0.50},  # Toss-up
        volume=11000,
        volume_24h=2800,
        tags=["weather", "rain"],
        token_ids={"Yes": "tok-sea-yes-1", "No": "tok-sea-no-1"},
    ),
]


# ─── Demo Runner ──────────────────────────────────────────────────────────

class DemoAutopilot:
    """Demo harness that seeds markets and runs the full autopilot."""

    def __init__(self, cities: list[str] = None):
        self.cities = [c.lower() for c in (cities or [])]
        self.db: AutopilotDB = None
        self.weather: OpenMeteoClient = None
        self.radar: RainViewerClient = None

    async def run(self):
        console.print()
        console.print(Panel(
            "🌤️  POLYMARKET WEATHER BOT - LIVE DEMO\n"
            "   Using real weather data from Open-Meteo + RainViewer",
            style="bold cyan",
            subtitle=f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        ))

        # Init
        self.db = AutopilotDB(starting_capital=10_000)
        self.weather = OpenMeteoClient()
        self.radar = RainViewerClient()

        strategies = [
            TemperatureStrategy(self.weather),
            PrecipitationStrategy(self.weather),
        ]

        # Filter markets by city
        markets = SEED_MARKETS
        if self.cities:
            markets = [
                m for m in SEED_MARKETS
                if any(c in m.question.lower() for c in self.cities)
            ]
            console.print(f"[dim]📌 Filtered to: {', '.join(self.cities)} ({len(markets)} markets)[/dim]\n")

        # ── PHASE 1: Show the markets ──────────────────────────────────
        console.print("[bold]📋 SEEDED MARKETS (mispriced on purpose):[/bold]\n")
        table = Table(show_header=True, header_style="bold white", border_style="blue")
        table.add_column("#", style="dim", width=3)
        table.add_column("Market Question", style="white", width=55)
        table.add_column("YES Price", justify="right", style="green")
        table.add_column("NO Price", justify="right", style="red")
        table.add_column("Volume", justify="right")

        for i, m in enumerate(markets, 1):
            table.add_row(
                str(i),
                m.question,
                f"{m.outcome_prices.get('Yes', 0):.0%}",
                f"{m.outcome_prices.get('No', 0):.0%}",
                f"${m.volume_24h:,}",
            )
        console.print(table)
        console.print()

        # ── PHASE 2: Fetch real weather data ───────────────────────────
        console.print("[bold]🔍 PHASE 2: Fetching REAL weather forecasts...[/bold]\n")

        forecasts = {}
        for i, market in enumerate(markets, 1):
            parsed = parse_weather_market(market.question, market.condition_id)
            if not parsed.city:
                continue

            loc = await self.weather.geocode_first(parsed.city, parsed.state or "")
            if not loc:
                console.print(f"  [red]❌ Could not geocode {parsed.city}[/red]")
                continue

            target = parsed.target_date or (date.today() + timedelta(days=7))
            days_out = (target - date.today()).days
            actual_days = min(max(days_out + 3, 3), 16)

            fc = await self.weather.get_forecast(loc.latitude, loc.longitude, actual_days)
            forecasts[market.condition_id] = (parsed, fc)

            # Find the relevant day
            for df in fc.daily:
                if df.date == target:
                    console.print(
                        f"  ✅ {parsed.city}: {df.date} → "
                        f"High: {df.temp_max:.1f}°C ({df.temp_max*9/5+32:.0f}°F) | "
                        f"Low: {df.temp_min:.1f}°C ({df.temp_min*9/5+32:.0f}°F) | "
                        f"Rain prob: {df.precip_prob_max:.0f}% | "
                        f"Snow: {df.snowfall_sum:.1f}cm | "
                        f"std: ±{df.temp_max_std:.1f}°C"
                    )
                    break

        console.print()

        # ── PHASE 3: Evaluate strategies and show decisions ─────────────
        console.print("[bold]🧠 PHASE 3: Strategy evaluation & trade decisions...[/bold]\n")

        signals_table = Table(
            show_header=True, header_style="bold white", border_style="green"
        )
        signals_table.add_column("#", style="dim", width=3)
        signals_table.add_column("Market", width=30)
        signals_table.add_column("Model Prob", justify="right", style="cyan")
        signals_table.add_column("Market Price", justify="right")
        signals_table.add_column("Edge", justify="right", style="bold magenta")
        signals_table.add_column("Action", justify="center", style="bold")
        signals_table.add_column("Size", justify="right", style="blue")
        signals_table.add_column("Reason", width=40)

        trades_opened = 0
        for i, market in enumerate(markets, 1):
            if market.condition_id not in forecasts:
                continue

            parsed, fc = forecasts[market.condition_id]
            yes_price = market.outcome_prices.get("Yes", 0.5)

            # Try temp strategy
            signal = None
            for strat in strategies:
                try:
                    signal = await strat.evaluate(
                        parsed=parsed,
                        market_price_yes=yes_price,
                        token_id_yes=market.token_ids.get("Yes", ""),
                        token_id_no=market.token_ids.get("No", ""),
                        max_position=100,
                    )
                    if signal and signal.edge >= 0.05:
                        break
                except Exception:
                    continue

            if signal:
                # Record paper trade
                self.db.deduct_cash(signal.size_usdc)
                tid = self.db.open_trade(
                    condition_id=signal.condition_id,
                    market_question=signal.market_question,
                    strategy=signal.strategy,
                    outcome=signal.outcome,
                    side=signal.side,
                    entry_price=signal.market_price,
                    quantity=signal.size_usdc,
                )
                trades_opened += 1

                action = ("[bold green]BUY YES[/bold green]" 
                         if signal.outcome == "Yes" 
                         else "[bold red]BUY NO[/bold red]")
                short_reason = signal.explanation.split("Threshold:")[0].strip()[:40]

                signals_table.add_row(
                    str(i),
                    parsed.city or "?",
                    f"{signal.model_probability:.1%}",
                    f"{signal.market_price:.1%}",
                    f"{signal.edge:.1%}",
                    action,
                    f"${signal.size_usdc:.0f}",
                    short_reason,
                )
            else:
                signals_table.add_row(
                    str(i),
                    parsed.city or "?",
                    f"{0.5:.0%}",
                    f"{yes_price:.0%}",
                    f"{0.0:.1%}",
                    "[dim]SKIP[/dim]",
                    "$0",
                    "Edge below threshold",
                )

        console.print(signals_table)

        # ── PHASE 4: Portfolio summary ─────────────────────────────────
        self.db.snapshot_portfolio()
        self.db.record_daily_stats()
        portfolio = self.db.get_portfolio_state()
        stats = self.db.get_all_time_stats()

        console.print()
        console.print(Panel(
            f"📊 Portfolio Value: ${portfolio['total_value']:,.2f} | "
            f"Cash: ${portfolio['cash']:,.2f} | "
            f"Invested: ${portfolio['invested']:,.2f} | "
            f"Open: {portfolio['open_positions']} pos | "
            f"Trades: {trades_opened} this cycle",
            title="🏦 Portfolio",
            border_style="yellow"
        ))

        # Show open trades
        open_trades = self.db.get_open_trades()
        if open_trades:
            console.print("[bold]🔓 Open Positions:[/bold]")
            for t in open_trades:
                q = (t["market_question"] or "?")[:80]
                console.print(
                    f"  #{t['id']} | {t['strategy']} | {t['side']} {t['outcome']} "
                    f"@ {t['entry_price']:.3f} | ${t['cost_basis']:.0f} | {q}"
                )

        # ── PHASE 5: Strategy breakdown ────────────────────────────────
        breakdown = self.db.get_strategy_breakdown()
        if breakdown:
            console.print()
            console.print("[bold]🎯 Strategy Performance:[/bold]")
            for s in breakdown:
                console.print(
                    f"  {s['strategy']}: {s['trades']} trades, "
                    f"W:{s['wins']} L:{s['losses']}, "
                    f"P&L: ${s['total_pnl']:+,.2f}"
                )

        # Cleanup
        await self.weather.close()
        await self.radar.close()
        self.db.close()

        console.print()
        console.print(Panel(
            "✅ Demo complete! The bot evaluated real weather data,\n"
            "found mispriced markets, and opened paper positions.\n\n"
            f"Run again to see it manage exits: [bold]python demo.py[/bold]",
            style="bold green"
        ))


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--cities", "-c", multiple=True, default=None,
                  help="Filter by city (e.g. -c 'New York' -c 'Chicago')")
    def main(cities):
        demo = DemoAutopilot(list(cities) if cities else None)
        asyncio.run(demo.run())

    main()
