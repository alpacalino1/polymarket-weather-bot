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

        # ── PHASE 2.5: Manage existing positions (exits) ─────────────
        existing_trades = self.db.get_open_trades()
        trades_closed = 0
        pnl_from_exits = 0.0
        closed_condition_ids: set[str] = set()

        if existing_trades:
            console.print(f"[bold]🔒 PHASE 2.5: Managing {len(existing_trades)} existing positions...[/bold]\n")
            for trade in existing_trades:
                # Re-evaluate with fresh forecast
                parsed = parse_weather_market(trade["market_question"], trade["condition_id"])
                if not parsed.city:
                    continue

                loc = await self.weather.geocode_first(parsed.city, parsed.state)
                if not loc:
                    continue

                target = parsed.target_date or (date.today() + timedelta(days=7))
                days_out = (target - date.today()).days
                if days_out < 0:
                    # Event passed! Close at outcome
                    exit_price = 1.0 if trade["outcome"] == "Yes" else 0.0
                    result = self.db.close_trade(trade["id"], exit_price, "Event date passed")
                    refund = trade["cost_basis"] + result["pnl"]
                    self.db.add_cash(refund)
                    closed_condition_ids.add(trade["condition_id"])
                    pnl_sign = "+" if result["pnl"] >= 0 else ""
                    console.print(
                        f"  🔒 #{trade['id']}: {pnl_sign}${result['pnl']:.2f} | "
                        f"\"{trade['market_question'][:60]}...\" | ⏰ Event date passed"
                    )
                    trades_closed += 1
                    pnl_from_exits += result["pnl"]
                    continue

                actual_days = min(max(days_out + 3, 3), 16)
                fc = await self.weather.get_forecast(loc.latitude, loc.longitude, actual_days)

                target_fc = None
                for df in fc.daily:
                    if df.date == target:
                        target_fc = df
                        break
                if not target_fc and fc.daily:
                    target_fc = fc.daily[min(days_out, len(fc.daily)-1)]

                if not target_fc:
                    continue

                # Recalculate edge
                yes_price = float(trade["entry_price"])
                if parsed.metric == "temperature" and parsed.temp_threshold_c is not None:
                    above = parsed.temp_direction != "below"
                    current_prob = self.weather.temperature_probability(
                        target_fc, parsed.temp_threshold_c, above
                    )
                elif parsed.metric in ("precipitation", "snowfall"):
                    current_prob = self.weather.precipitation_probability(target_fc)
                else:
                    current_prob = 0.5

                if trade["outcome"] == "Yes":
                    current_edge = current_prob - yes_price
                else:
                    current_edge = (1 - current_prob) - (1 - yes_price)

                abs_edge = abs(current_edge)

                # Exit conditions
                should_close = False
                reason = ""
                exit_price = yes_price

                if abs_edge < 0.02:
                    should_close = True
                    reason = f"Edge decayed ({abs_edge:.1%})"
                elif trade["outcome"] == "Yes" and current_prob > 0.95:
                    should_close = True
                    reason = f"High conviction ({current_prob:.1%}) - take profit"
                    exit_price = current_prob
                elif trade["outcome"] == "No" and current_prob < 0.05:
                    should_close = True
                    reason = f"High conviction NO ({current_prob:.1%}) - take profit"
                    exit_price = 1 - current_prob

                if should_close:
                    result = self.db.close_trade(trade["id"], exit_price, reason)
                    refund = trade["cost_basis"] + result["pnl"]
                    self.db.add_cash(refund)
                    closed_condition_ids.add(trade["condition_id"])
                    pnl_sign = "+" if result["pnl"] >= 0 else ""
                    console.print(
                        f"  🔒 #{trade['id']}: {pnl_sign}${result['pnl']:.2f} | "
                        f"entry {trade['entry_price']:.3f} → exit {exit_price:.3f} | "
                        f"\"{trade['market_question'][:50]}...\" | {reason}"
                    )
                    trades_closed += 1
                    pnl_from_exits += result["pnl"]
                else:
                    console.print(
                        f"  🔓 #{trade['id']}: HOLD | edge={abs_edge:.1%} | "
                        f"\"{trade['market_question'][:50]}...\""
                    )

            if trades_closed:
                console.print(f"\n  [bold green]{trades_closed} positions closed | P&L: ${pnl_from_exits:+,.2f}[/bold green]")
            console.print()

        # ── PHASE 3: Evaluate new entries ──────────────────────────────
        open_condition_ids = {t["condition_id"] for t in self.db.get_open_trades()}
        skip_ids = open_condition_ids | closed_condition_ids

        console.print("[bold]🧠 PHASE 3: Strategy evaluation & trade decisions...[/bold]\n")

        signals_table = Table(show_header=True, header_style="bold white", border_style="green")
        signals_table.add_column("#", style="dim", width=3)
        signals_table.add_column("City", width=12)
        signals_table.add_column("Forecast", width=14)
        signals_table.add_column("Model %", justify="right", style="cyan", width=8)
        signals_table.add_column("Market %", justify="right", width=8)
        signals_table.add_column("Edge", justify="right", style="bold magenta", width=8)
        signals_table.add_column("Action", justify="center", style="bold", width=12)
        signals_table.add_column("Size", justify="right", style="blue", width=6)
        signals_table.add_column("Reason", width=38)

        trades_opened = 0
        for i, market in enumerate(markets, 1):
            if market.condition_id not in forecasts:
                continue
            if market.condition_id in skip_ids:
                signals_table.add_row(str(i), "-", "-", "-", "-", "-", "[dim]HELD/SKIPPED[/dim]", "-", "Already managed this cycle")
                continue

            parsed, fc = forecasts[market.condition_id]
            yes_price = market.outcome_prices.get("Yes", 0.5)

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

                action_color = "green" if signal.outcome == "Yes" else "red"
                action = f"[bold {action_color}]BUY {signal.outcome.upper()}[/bold {action_color}]"

                # Show key forecast info
                target_fc = None
                for df in fc.daily:
                    if df.date == parsed.target_date:
                        target_fc = df
                        break
                if target_fc:
                    fcast = f"{(target_fc.temp_max*9/5+32):.0f}°F"
                else:
                    fcast = "?"

                signals_table.add_row(
                    str(i), parsed.city[:12], fcast,
                    f"{signal.model_probability:.1%}",
                    f"{signal.market_price:.1%}",
                    f"{signal.edge:.1%}",
                    action,
                    f"${signal.size_usdc:.0f}",
                    signal.explanation.split("Threshold:")[0][:38],
                )
            else:
                target_fc = None
                for df in fc.daily:
                    if df.date == parsed.target_date:
                        target_fc = df
                        break
                fcast = f"{(target_fc.temp_max*9/5+32):.0f}°F" if target_fc else "?"

                signals_table.add_row(
                    str(i), parsed.city[:12], fcast,
                    f"{0.5:.0%}", f"{yes_price:.0%}", "-",
                    "[dim]SKIP[/dim]", "-", "No edge"
                )

        console.print(signals_table)

        # ── PHASE 4: Portfolio summary ─────────────────────────────────
        self.db.snapshot_portfolio()
        self.db.record_daily_stats()
        portfolio = self.db.get_portfolio_state()

        console.print()
        console.print(Panel(
            f"💰 Portfolio Value: ${portfolio['total_value']:,.2f}  │  "
            f"Cash: ${portfolio['cash']:,.2f}  │  "
            f"Invested: ${portfolio['invested']:,.2f}  │  "
            f"Return: {portfolio['total_return_pct']:+.1f}%",
            title="🏦 Portfolio",
            border_style="yellow"
        ))

        # Show open trades compact
        open_trades = self.db.get_open_trades()
        if open_trades:
            console.print(f"\n[bold]🔓 {len(open_trades)} Open Positions:[/bold]")
            ot = Table(show_header=True, header_style="bold", border_style="dim blue")
            ot.add_column("ID", style="dim", width=4)
            ot.add_column("City", width=10)
            ot.add_column("Bet", width=18)
            ot.add_column("Entry", justify="right", width=6)
            ot.add_column("Cost", justify="right", width=6)
            for t in open_trades:
                parsed = parse_weather_market(t["market_question"], t["condition_id"])
                ot.add_row(
                    f"#{t['id']}",
                    parsed.city[:10],
                    f"{t['outcome']} {t['entry_price']:.2f}",
                    f"{t['entry_price']:.3f}",
                    f"${t['cost_basis']:.0f}",
                )
            console.print(ot)

        # Show recently closed this cycle
        if trades_closed:
            console.print(f"\n[bold]🔒 {trades_closed} Closed This Cycle:[/bold]")
            console.print(f"   P&L: ${pnl_from_exits:+,.2f}")

        # Strategy breakdown
        breakdown = self.db.get_strategy_breakdown()
        if breakdown:
            console.print("\n[bold]🎯 Strategy Performance (All-Time):[/bold]")
            for s in breakdown:
                console.print(
                    f"   {s['strategy']}: {s['trades']} trades │ "
                    f"W:{s['wins']} L:{s['losses']} │ "
                    f"P&L: ${s['total_pnl']:+,.2f}"
                )

        console.print()
        console.print(Panel(
            "✅ Run [bold]python demo.py[/bold] again — the bot will re-evaluate\n"
            "open positions with fresh weather data and close when edges decay!",
            style="bold green"
        ))

        await self.weather.close()
        await self.radar.close()
        self.db.close()


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--cities", "-c", multiple=True, default=None,
                  help="Filter by city (e.g. -c 'New York' -c 'Chicago')")
    def main(cities):
        demo = DemoAutopilot(list(cities) if cities else None)
        asyncio.run(demo.run())

    main()
