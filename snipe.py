#!/usr/bin/env python3
"""
BTC Sniper — watches 1 specific Polymarket market and re-evaluates every N seconds.

Ultra-lean: 2 CoinGecko calls + 1 Gamma call per cycle.
Safe at 10-15 second intervals on the free API tier.

Usage:
    python snipe.py             # Watch the BTC→$1M market, trade if edge persists
    python snipe.py --interval 15
    python snipe.py --live      # Continuously re-evaluate + re-enter on edge
"""

import asyncio
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from feed.coingecko import CoinGeckoFeed
from polymarket.client import GammaClient
from strategies.btc_threshold import BTCThresholdStrategy, BTCThresholdSignal
from autopilot import AutopilotDB

console = Console()

# The one market we care about
TARGET_MARKET = {
    "condition_id": "0x2e29a0b3b80f0b74eab9c3b0d3bb4b0a5c84c5e6d8f7a6b5c4d3e2f1a0b9c8d7",
    "question": "Will bitcoin hit $1m before GTA VI?",
    "token_id_yes": "105267568073659068217311993901927962476298440625043565106676088842803600775810",
    "token_id_no": "91863162118308663069733924043159186005106558783397508844234610341221325526200",
    "target_price": 1_000_000,
    "days_estimate": 550,
}


class BTCSniper:
    """Watches one market like a hawk."""

    def __init__(self, max_position: float = 100, min_edge: float = 0.05):
        self.max_position = max_position
        self.min_edge = min_edge
        self.feed = CoinGeckoFeed()
        self.gamma = GammaClient()
        self.strategy = BTCThresholdStrategy(self.feed)
        self.db = AutopilotDB(starting_capital=20_000)
        self.last_signal: Optional[BTCThresholdSignal] = None
        self.cycles = 0
        self.trades_opened = 0
        self.trades_closed = 0
        self.total_pnl = 0.0

    async def tick(self) -> dict:
        """One evaluation tick. Returns status dict."""
        self.cycles += 1
        m = TARGET_MARKET

        # 1. Get current market price from Gamma
        try:
            market = await self.gamma.get_market(m["condition_id"])
            yes_price = market.outcome_prices.get("Yes", 0.5) if market else 0.5
        except Exception:
            yes_price = 0.5

        # 2. Evaluate the barrier model
        signal = await self.strategy.evaluate_barrier(
            condition_id=m["condition_id"],
            token_id_yes=m["token_id_yes"],
            token_id_no=m["token_id_no"],
            market_question=m["question"],
            market_price_yes=yes_price,
            target_price=m["target_price"],
            days_to_expiry=m["days_estimate"],
            max_position=self.max_position,
        )

        self.last_signal = signal

        # 3. Manage existing positions
        existing = self.db.get_open_trades_for_market(m["condition_id"])
        if existing:
            for trade in existing:
                await self._check_exit(trade, signal)

        # 4. Enter new position if no existing and edge exists
        if not existing and signal and signal.edge >= self.min_edge:
            self._enter(signal)

        return {
            "cycle": self.cycles,
            "btc_spot": signal.btc_spot if signal else None,
            "yes_price": yes_price,
            "model_prob": signal.model_probability if signal else None,
            "edge": signal.edge if signal else 0,
            "signal": signal,
            "open_positions": len(self.db.get_open_trades()),
        }

    def _enter(self, signal: BTCThresholdSignal):
        """Open a paper position."""
        try:
            self.db.deduct_cash(signal.size_usdc)
            self.db.open_trade(
                condition_id=signal.condition_id,
                market_question=signal.market_question,
                strategy="BTCBarrier",
                outcome=signal.outcome,
                side=signal.side,
                entry_price=signal.market_price_yes if signal.outcome == "Yes" else 1 - signal.market_price_yes,
                quantity=signal.size_usdc,
            )
            self.trades_opened += 1
        except Exception as e:
            console.print(f"[red]Entry error: {e}[/red]")

    async def _check_exit(self, trade: dict, signal):
        """Check if we should exit an open position."""
        if not signal:
            return

        # Exit if edge reversed significantly
        if signal.outcome != trade["outcome"]:
            # Opposite signal now — close
            self._exit_trade(trade, signal.market_price_yes, "Signal reversed")
            return

        # Exit on high conviction take-profit
        if trade["outcome"] == "Yes" and signal.model_probability > 0.95:
            self._exit_trade(trade, signal.model_probability, f"High conviction ({signal.model_probability:.0%})")
            return
        if trade["outcome"] == "No" and signal.model_probability < 0.05:
            self._exit_trade(trade, 1 - signal.model_probability, f"High conviction NO ({signal.model_probability:.0%})")
            return

        # Exit if edge decayed significantly from entry
        current_edge = abs(signal.model_probability - float(trade["entry_price"]))
        if current_edge < 0.02:
            self._exit_trade(trade, float(trade["entry_price"]), f"Edge decayed ({current_edge:.1%})")

    def _exit_trade(self, trade: dict, exit_price: float, reason: str):
        """Close a paper position."""
        result = self.db.close_trade(trade["id"], exit_price, reason)
        if result:
            refund = trade["cost_basis"] + result["pnl"]
            self.db.add_cash(refund)
            self.trades_closed += 1
            self.total_pnl += result["pnl"]
            pnl_sign = "+" if result["pnl"] >= 0 else ""
            console.print(
                f"  🔒 Closed #{trade['id']}: {pnl_sign}${result['pnl']:.2f} | {reason}"
            )

    def display(self, status: dict):
        """One-line status display."""
        s = status["signal"]
        pf = self.db.get_portfolio_state()

        if s:
            edge_str = f"[bold magenta]{s.edge:.1%}[/bold magenta]"
            model_str = f"[cyan]{s.model_probability:.1%}[/cyan]"
            mkt_str = f"{status['yes_price']:.1%}"
            btc_str = f"[bold]${s.btc_spot:,.0f}[/bold]"

            action = "BUY YES" if s.outcome == "Yes" else "BUY NO"
            action_color = "green" if s.outcome == "Yes" else "red"

            console.print(
                f"#{status['cycle']:04d} │ {btc_str} │ "
                f"Model: {model_str} vs Mkt: {mkt_str} │ "
                f"Edge: {edge_str} │ "
                f"[bold {action_color}]{action}[/bold {action_color}] ${s.size_usdc:.0f} │ "
                f"Portfolio: ${pf['total_value']:,.0f} ({pf['total_return_pct']:+.1f}%) │ "
                f"Open: {status['open_positions']}"
            )
        else:
            console.print(f"#{status['cycle']:04d} │ No signal")

    async def close(self):
        await self.feed.close()
        await self.gamma.close()
        self.db.close()


if __name__ == "__main__":
    import click
    from datetime import timedelta

    @click.command()
    @click.option("--live", "-l", is_flag=True, help="Continuous + re-enter on edge")
    @click.option("--interval", "-i", type=int, default=20, help="Seconds between ticks (default: 20)")
    @click.option("--max-position", "-p", type=float, default=100)
    @click.option("--cycles", "-n", type=int, default=0, help="Max cycles (0=forever)")
    def main(live, interval, max_position, cycles):
        sniper = BTCSniper(max_position=max_position)

        async def run():
            console.print(Panel(
                Text("🎯 BTC SNIPER — Single Market Arb", style="bold yellow"),
                subtitle="Watching: \"Will bitcoin hit $1m before GTA VI?\""
            ))
            console.print(f"[dim]Interval: {interval}s | "
                         f"API calls/cycle: 3 (2 CoinGecko + 1 Gamma) | "
                         f"Min safe: 15s[/dim]")
            console.print(f"[dim]{'Live re-entry mode' if live else 'One-position mode'}[/dim]")
            console.print()

            n = 0
            try:
                while True:
                    n += 1
                    status = await sniper.tick()
                    sniper.display(status)

                    if live and not sniper.db.get_open_trades_for_market(TARGET_MARKET["condition_id"]):
                        # Edge still exists and no position → re-enter
                        signal = sniper.last_signal
                        if signal and signal.edge >= 0.05:
                            sniper._enter(signal)
                            console.print(f"  [dim]↻ Re-entered position[/dim]")

                    if cycles and n >= cycles:
                        break

                    await asyncio.sleep(interval)
            except KeyboardInterrupt:
                pass
            finally:
                # Summary
                pf = sniper.db.get_portfolio_state()
                console.print()
                console.print(Panel(
                    f"Cycles: {n} │ "
                    f"Trades opened: {sniper.trades_opened} │ "
                    f"Closed: {sniper.trades_closed} │ "
                    f"P&L: ${sniper.total_pnl:+,.2f} │ "
                    f"Portfolio: ${pf['total_value']:,.2f} ({pf['total_return_pct']:+.1f}%)",
                    title="🏁 Sniper Summary",
                    border_style="green"
                ))
                await sniper.close()

        asyncio.run(run())

    main()
