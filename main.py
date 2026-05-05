#!/usr/bin/env python3
"""
Polymarket Weather Trading Bot
================================
Finds weather-related prediction markets on Polymarket and trades them
using free weather and radar APIs.

Features:
- Discovers weather markets on Polymarket (temperature, rain, snow, storms)
- Fetches forecasts from Open-Meteo (global, free) + NWS (US, free)
- Incorporates radar data (RainViewer) for short-term precipitation
- Calculates model probabilities from weather data
- Identifies mispriced markets and places trades
- Multiple modes: scan-only, paper trading, live trading
- Risk management with position sizing and loss limits

Usage:
    # Scan for opportunities (no trading)
    python main.py

    # Scan once and exit
    python main.py --once

    # Paper trade (simulate trades, log results)
    python main.py --mode paper

    # Live trading
    python main.py --mode live

    # Custom scan interval
    python main.py --interval 10

    # Override max position
    python main.py --max-position 50

    # Filter by specific cities
    python main.py --cities "New York" "Chicago" "Miami"
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from rich.progress import Progress

from config import AppConfig, config
from weather import (
    OpenMeteoClient,
    NWSClient,
    RainViewerClient,
)
from polymarket import (
    GammaClient,
    PolymarketClobClient,
    WeatherMarketParser,
    parse_weather_market,
)
from strategies import TemperatureStrategy, PrecipitationStrategy, TradeSignal
from utils import setup_logging, get_logger

console = Console()


# ─── Bot Core ─────────────────────────────────────────────────────────────

class WeatherTradingBot:
    """Main bot orchestrator."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.logger = None

        # Clients (initialized in setup)
        self.weather_client = OpenMeteoClient()
        self.nws_client = NWSClient()
        self.radar_client = RainViewerClient()
        self.gamma_client = GammaClient(cfg.polymarket.gamma_url)
        self.market_parser = WeatherMarketParser()

        # Polymarket CLOB client (for trading)
        self.clob_client: Optional[PolymarketClobClient] = None
        if cfg.trading.is_paper or cfg.trading.is_live:
            self.clob_client = PolymarketClobClient(
                private_key=cfg.polymarket.private_key,
                chain_id=cfg.polymarket.chain_id,
                clob_url=cfg.polymarket.clob_url,
            )

        # Strategies
        self.temp_strategy = TemperatureStrategy(self.weather_client)
        self.precip_strategy = PrecipitationStrategy(self.weather_client)

        # State
        self.active_positions: dict[str, float] = {}  # condition_id -> size
        self.last_checked: dict[str, datetime] = {}   # condition_id -> last check time
        self.daily_pnl: float = 0.0
        self.trade_count_today: int = 0
        self.start_time = datetime.now()
        self.signals_found: list[TradeSignal] = []

    async def setup(self):
        """Initialize connections and validate configuration."""
        self.logger = setup_logging(
            level=self.cfg.log_level,
            log_file=self.cfg.log_file,
        )
        self.logger.info("🚀 Polymarket Weather Trading Bot starting...")
        self.logger.info(f"Mode: {self.cfg.trading.bot_mode}")
        self.logger.info(f"Trading: {'✅ Enabled' if self.cfg.trading.is_live else '📋 Scan only'}")

        # Validate trading config
        if self.cfg.trading.is_live:
            if not self.cfg.polymarket.is_configured:
                self.logger.error(
                    "❌ Live trading requires POLYMARKET_PRIVATE_KEY. "
                    "Set it in .env or environment."
                )
                sys.exit(1)
            if self.clob_client and not self.clob_client.is_ready:
                self.logger.error(
                    "❌ Polymarket CLOB client failed to initialize. "
                    "Check your private key and network connection."
                )
                sys.exit(1)
            self.logger.info("✅ Polymarket CLOB client ready")

        if self.cfg.trading.is_paper:
            self.logger.info("📝 Paper trading mode - trades will be simulated")

    async def shutdown(self):
        """Clean shutdown."""
        self.logger.info("Shutting down...")
        await self.weather_client.close()
        await self.nws_client.close()
        await self.radar_client.close()
        await self.gamma_client.close()
        if self.clob_client:
            try:
                self.clob_client.cancel_all()
            except Exception:
                pass

    # ── Main Loop ───────────────────────────────────────────────────────

    async def run_once(self) -> list[TradeSignal]:
        """Execute one full scan cycle. Returns discovered signals."""
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"🔍 Scanning cycle @ {datetime.now().strftime('%H:%M:%S')}")
        self.logger.info(f"{'='*60}")

        # 1. Get radar data for short-term precipitation awareness
        radar_data = None
        try:
            radar_data = await self.radar_client.get_radar()
            self.logger.info(
                f"📡 Radar: intensity={radar_data.intensity:.2f}, "
                f"trend={radar_data.trend}, "
                f"frames={len(radar_data.past)}+{len(radar_data.nowcast)}"
            )
        except Exception as e:
            self.logger.warning(f"Radar fetch failed: {e}")

        # 2. Discover weather markets
        try:
            all_markets = await self.gamma_client.search_weather_markets(limit=100)
        except Exception as e:
            self.logger.error(f"Market discovery failed: {e}")
            all_markets = []

        active_weather_markets = []
        for m in all_markets:
            if m.closed:
                continue
            if m.volume_24h < self.cfg.trading.min_market_volume_usdc:
                continue
            active_weather_markets.append(m)

        self.logger.info(
            f"📊 Found {len(active_weather_markets)} active weather markets "
            f"(filtered from {len(all_markets)} total)"
        )

        # 3. Parse and evaluate each market
        signals = []
        checked = 0
        skipped = 0

        for market in active_weather_markets:
            # Rate limit: don't check the same market too often
            last_check = self.last_checked.get(market.condition_id)
            cooldown = timedelta(minutes=self.cfg.trading.market_check_cooldown_minutes)
            if last_check and (datetime.now() - last_check) < cooldown:
                skipped += 1
                continue

            # Parse the question
            parsed = parse_weather_market(
                market.question, market.condition_id
            )

            if not parsed.is_weather or parsed.confidence < 0.3:
                skipped += 1
                continue

            # Apply location filter if configured
            if self.cfg.focus_locations:
                loc_match = any(
                    loc.lower() in f"{parsed.city} {parsed.state}".lower()
                    for loc in self.cfg.focus_locations
                )
                if not loc_match:
                    skipped += 1
                    continue

            # Get token IDs for Yes/No
            token_yes = market.token_ids.get("Yes", "")
            token_no = market.token_ids.get("No", "")

            # Get current market price (from Gamma data)
            yes_price = market.outcome_prices.get("Yes", 0.5)
            if not yes_price or yes_price == 0:
                yes_price = 0.5  # Default if price unavailable

            try:
                # Evaluate with temperature strategy
                if parsed.metric == "temperature":
                    signal = await self.temp_strategy.evaluate(
                        parsed=parsed,
                        market_price_yes=yes_price,
                        token_id_yes=token_yes,
                        token_id_no=token_no,
                        max_position=self.cfg.trading.max_position_usdc,
                    )
                elif parsed.metric in ("precipitation", "snowfall"):
                    signal = await self.precip_strategy.evaluate(
                        parsed=parsed,
                        market_price_yes=yes_price,
                        token_id_yes=token_yes,
                        token_id_no=token_no,
                        max_position=self.cfg.trading.max_position_usdc,
                    )
                else:
                    signal = None  # Unsupported metric type

                if signal:
                    signals.append(signal)
                    self.last_checked[market.condition_id] = datetime.now()
                    checked += 1

            except Exception as e:
                self.logger.debug(f"Error evaluating {market.question[:80]}: {e}")

        # 4. Report results
        self._print_scan_results(signals, radar_data, checked, skipped)

        # 5. Execute trades if not in scan-only mode
        if signals and (self.cfg.trading.is_live or self.cfg.trading.is_paper):
            for signal in signals:
                await self._execute_trade(signal)

        self.signals_found = signals
        return signals

    async def run_loop(self):
        """Run the bot continuously with a scan interval."""
        self.logger.info(f"🔄 Continuous mode: scanning every {self.cfg.trading.scan_interval_minutes} min")

        while True:
            try:
                await self.run_once()
            except Exception as e:
                self.logger.error(f"Scan cycle error: {e}", exc_info=True)

            # Wait for next scan
            interval = self.cfg.trading.scan_interval_minutes * 60
            self.logger.info(f"⏳ Next scan in {self.cfg.trading.scan_interval_minutes} minutes...")
            await asyncio.sleep(interval)

    # ── Trade Execution ──────────────────────────────────────────────────

    async def _execute_trade(self, signal: TradeSignal):
        """Execute a trade based on the signal."""
        # Check risk limits
        total_exposure = sum(self.active_positions.values())
        if total_exposure + signal.size_usdc > self.cfg.trading.max_total_exposure_usdc:
            self.logger.warning(
                f"⚠️  Max exposure reached ({total_exposure:.0f} USDC). Skipping trade."
            )
            return

        if len(self.active_positions) >= self.cfg.trading.max_concurrent_positions:
            self.logger.warning("⚠️  Max concurrent positions. Skipping trade.")
            return

        if self.daily_pnl < -self.cfg.trading.max_daily_loss_usdc:
            self.logger.warning(f"⚠️  Daily loss limit hit ({self.daily_pnl:.0f} USDC). Stopping.")
            return

        # Determine order price: use market price slightly above/below for fill
        if signal.side == "BUY":
            order_price = min(signal.market_price + 0.02, 0.99)  # Slightly above market
        else:
            order_price = max(signal.market_price - 0.02, 0.01)  # Slightly below market

        order_price = round(order_price, 4)

        if self.cfg.trading.is_paper:
            # Paper trade
            self.logger.info(
                f"📝 PAPER TRADE: {signal.side} {signal.size_usdc:.1f} USDC of "
                f"{signal.outcome} @ {order_price:.4f} | {signal.market_question[:80]}..."
            )
            self.active_positions[signal.condition_id] = signal.size_usdc
            self.trade_count_today += 1

        elif self.cfg.trading.is_live and self.clob_client:
            # Live trade
            self.logger.info(
                f"🔴 LIVE TRADE: {signal.side} {signal.size_usdc:.1f} USDC of "
                f"{signal.outcome} @ {order_price:.4f}"
            )

            result = self.clob_client.place_order(
                token_id=signal.token_id,
                side=signal.side,
                price=order_price,
                size=signal.size_usdc,
            )

            if result.success:
                self.logger.info(f"  ✅ Order placed: {result.order_id}")
                if result.transaction_hash:
                    self.logger.info(f"  📜 TX: {result.transaction_hash[:20]}...")
                self.active_positions[signal.condition_id] = signal.size_usdc
                self.trade_count_today += 1
            else:
                self.logger.error(f"  ❌ Order failed: {result.error}")

    # ── Display ──────────────────────────────────────────────────────────

    def _print_scan_results(
        self,
        signals: list[TradeSignal],
        radar_data,
        checked: int,
        skipped: int,
    ):
        """Pretty-print scan results using Rich."""
        runtime = datetime.now() - self.start_time

        # Header
        console.print()
        console.print(Panel(
            Text(f"🌤️  Weather Bot Scan Results", style="bold cyan"),
            subtitle=f"Runtime: {runtime} | Checked: {checked} | Skipped: {skipped}"
        ))

        if not signals:
            console.print("[yellow]No trading opportunities found this cycle.[/yellow]")
            console.print(f"[dim]Markets checked: {checked}, skipped (cooldown/confidence): {skipped}[/dim]")
            return

        console.print(f"[green]✨ Found {len(signals)} trading opportunities![/green]\n")

        # Build table
        table = Table(
            title="Trading Signals",
            show_header=True,
            header_style="bold white",
            border_style="blue",
        )
        table.add_column("#", style="dim", width=3)
        table.add_column("City", style="cyan", width=18)
        table.add_column("Date", style="yellow", width=12)
        table.add_column("Metric", style="green", width=14)
        table.add_column("Model %", justify="right", width=10)
        table.add_column("Market %", justify="right", width=10)
        table.add_column("Edge", justify="right", style="bold magenta", width=8)
        table.add_column("Action", style="bold", width=12)
        table.add_column("Size", justify="right", style="blue", width=8)

        for i, sig in enumerate(signals, 1):
            # Parse location from explanation
            city_str = "?"
            for city_kw in ["City:", "for"]:
                if city_kw in sig.explanation:
                    parts = sig.explanation.split(city_kw, 1)
                    if len(parts) > 1:
                        city_str = parts[1].split(".")[0].split(",")[0].strip()
                        break

            # Determine action color
            if sig.outcome == "Yes":
                action_style = "[bold green]BUY YES[/bold green]"
            else:
                action_style = "[bold red]BUY NO[/bold red]"

            table.add_row(
                str(i),
                city_str[:18],
                "",  # Date from market question
                sig.strategy[:14],
                f"{sig.model_probability:.1%}",
                f"{sig.market_price:.1%}",
                f"{sig.edge:.1%}",
                action_style,
                f"${sig.size_usdc:.0f}",
            )

        console.print(table)

        # Print detailed explanations
        console.print("\n[bold]Signal Details:[/bold]")
        for i, sig in enumerate(signals, 1):
            console.print(f"[dim]{i}.[/dim] {sig.explanation}")

        # Summary
        total_edge = sum(s.edge * s.size_usdc for s in signals)
        console.print(f"\n[bold]Total opportunity: {len(signals)} signals[/bold]")


# ─── CLI ──────────────────────────────────────────────────────────────────

@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--mode", "-m",
    type=click.Choice(["scan", "paper", "live"]),
    default=None,
    help="Bot mode: scan (find opportunities), paper (simulate), live (real trades)"
)
@click.option(
    "--once", "-1",
    is_flag=True,
    help="Run a single scan and exit"
)
@click.option(
    "--interval", "-i",
    type=int,
    default=None,
    help="Scan interval in minutes (default: 15)"
)
@click.option(
    "--max-position", "-p",
    type=float,
    default=None,
    help="Max USDC per position (default: 100)"
)
@click.option(
    "--min-edge", "-e",
    type=float,
    default=None,
    help="Minimum edge to trade, e.g. 0.05 = 5% (default: 0.05)"
)
@click.option(
    "--cities", "-c",
    multiple=True,
    default=None,
    help="Focus on specific cities (can repeat: -c 'New York' -c 'Chicago')"
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default=None,
    help="Log level"
)
def main(mode, once, interval, max_position, min_edge, cities, log_level):
    """🌤️  Polymarket Weather Trading Bot

    Discovers weather-related prediction markets on Polymarket,
    analyzes them using free weather APIs, and trades mispriced markets.
    """
    # Apply CLI overrides to config
    if mode:
        config.trading.bot_mode = mode
    if interval:
        config.trading.scan_interval_minutes = interval
    if max_position:
        config.trading.max_position_usdc = max_position
    if min_edge is not None:
        config.trading.min_edge = min_edge
    if cities:
        config.focus_locations = list(cities)
    if log_level:
        config.log_level = log_level

    # Validate
    if config.trading.is_live and not config.polymarket.private_key:
        console.print(
            "[bold red]❌ Live trading requires POLYMARKET_PRIVATE_KEY[/bold red]\n"
            "Set it in .env file or as an environment variable.\n"
            "Copy .env.example to .env and add your key."
        )
        sys.exit(1)

    # Run the bot
    bot = WeatherTradingBot(config)

    async def run():
        try:
            await bot.setup()

            if once:
                await bot.run_once()
            else:
                await bot.run_loop()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]🛑 Shutting down...[/bold yellow]")
        except Exception as e:
            console.print(f"[bold red]Fatal error: {e}[/bold red]")
            import traceback
            traceback.print_exc()
        finally:
            await bot.shutdown()

    asyncio.run(run())


if __name__ == "__main__":
    main()
