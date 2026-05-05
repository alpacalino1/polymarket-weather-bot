#!/usr/bin/env python3
"""
BTC Arbitrage Bot — scans real Polymarket markets and paper-trades edges.

Two strategies:
1. CLOB Spread Arb — orderbook YES+NO < $1 (rare, markets are efficient)
2. BTC Threshold Model — barrier probability vs market price

Usage:
    python run_btc.py           # Single scan, show opportunities
    python run_btc.py --trade   # Paper trade into autopilot DB
    python run_btc.py --live    # Continuous scanning every 60s
"""

import asyncio
import sys
import time
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from rich.live import Live

from feed.coingecko import CoinGeckoFeed
from polymarket.client import GammaClient
from strategies.spread_arb import SpreadArbScanner, ArbOpportunity
from strategies.btc_threshold import BTCThresholdStrategy, BTCThresholdSignal
from autopilot import AutopilotDB

console = Console()


class BTCRunner:
    """Combined BTC arbitrage runner — spread + threshold strategies."""

    BTC_SEARCH = ["bitcoin", "Bitcoin", "BTC", "btc"]

    def __init__(self, top_n: int = 50, min_volume: float = 500, paper_trade: bool = False):
        self.top_n = top_n
        self.min_volume = min_volume
        self.paper_trade = paper_trade
        self.gamma = GammaClient()
        self.scanner = SpreadArbScanner()
        self.feed = CoinGeckoFeed()
        self.btc_strat = BTCThresholdStrategy(self.feed)
        self.db = AutopilotDB(starting_capital=20_000) if paper_trade else None

    async def run_once(self) -> dict:
        """Execute one full scan cycle."""
        # ── BTC Spot ───────────────────────────────────────────────
        btc = await self.feed.get_btc_price()
        spot = btc.price_usd if btc else None

        # ── Discover BTC markets ───────────────────────────────────
        seen = set()
        btc_markets = []
        for term in self.BTC_SEARCH:
            try:
                markets = await self.gamma.search_markets(
                    query=term, limit=50, closed=False
                )
                for m in markets:
                    q_lower = m.question.lower()
                    is_btc = any(kw in q_lower for kw in ['bitcoin', 'btc', '₿', 'crypto'])
                    if is_btc and m.condition_id not in seen:
                        seen.add(m.condition_id)
                        if m.volume_24h >= self.min_volume and not m.closed:
                            btc_markets.append(m)
            except Exception:
                continue

        # ── Strategy 1: Spread Arb Scan ────────────────────────────
        spread_arbs = []
        for m in btc_markets:
            tid_y = m.token_ids.get("Yes", "")
            tid_n = m.token_ids.get("No", "")
            if tid_y and tid_n and tid_y not in ("[", "Yes"):
                try:
                    arb = await self.scanner.check_market(
                        tid_y, tid_n, m.condition_id, m.question, m.volume_24h
                    )
                    if arb:
                        spread_arbs.append(arb)
                except Exception:
                    pass

        # ── Strategy 2: BTC Threshold Model ────────────────────────
        threshold_signals = []
        for m in btc_markets:
            tid_y = m.token_ids.get("Yes", "")
            tid_n = m.token_ids.get("No", "")
            if not tid_y or not tid_n:
                continue

            q = m.question.lower()
            yes_price = m.outcome_prices.get("Yes", 0.5)

            # Parse target price + timeframe from question
            # "Will bitcoin hit $1m before GTA VI?"
            target = self._extract_price_target(q)
            days = self._extract_timeframe_days(q)

            if target and days:
                try:
                    sig = await self.btc_strat.evaluate_barrier(
                        condition_id=m.condition_id,
                        token_id_yes=tid_y,
                        token_id_no=tid_n,
                        market_question=m.question,
                        market_price_yes=yes_price,
                        target_price=target,
                        days_to_expiry=days,
                        max_position=100,
                    )
                    if sig:
                        threshold_signals.append(sig)
                except Exception:
                    pass

        # ── Paper trade if enabled ─────────────────────────────────
        trades_opened = 0
        if self.paper_trade and threshold_signals:
            for sig in threshold_signals:
                existing = self.db.get_open_trades_for_market(sig.condition_id)
                if existing:
                    continue
                try:
                    self.db.deduct_cash(sig.size_usdc)
                    self.db.open_trade(
                        condition_id=sig.condition_id,
                        market_question=sig.market_question,
                        strategy="BTCThreshold",
                        outcome=sig.outcome,
                        side=sig.side,
                        entry_price=sig.market_price_yes if sig.outcome == "Yes" else 1 - sig.market_price_yes,
                        quantity=sig.size_usdc,
                    )
                    trades_opened += 1
                except Exception:
                    pass

        return {
            "btc_spot": spot,
            "btc_change_24h": btc.change_24h_pct if btc else 0,
            "markets_found": len(btc_markets),
            "spread_arbs": spread_arbs,
            "threshold_signals": threshold_signals,
            "trades_opened": trades_opened,
        }

    def _extract_price_target(self, q: str) -> Optional[float]:
        """Extract BTC price target from question text."""
        import re
        patterns = [
            r'\$(\d[\d,]*(?:\.\d+)?)\s*(?:million|m|k|thousand)?',
            r'(\d[\d,]*)\s*(?:million|m)\b',
            r'(\d[\d,]*)[kK]\b',
        ]
        for pattern in patterns:
            match = re.search(pattern, q)
            if match:
                val = match.group(1).replace(",", "")
                val = float(val)
                if 'million' in q or 'm' in match.group(0).lower():
                    val *= 1_000_000
                elif 'k' in match.group(0).lower():
                    val *= 1_000
                return val
        return None

    def _extract_timeframe_days(self, q: str) -> Optional[int]:
        """Extract timeframe in days from question, or use default."""
        import re

        # Look for explicit dates
        month_map = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'may': 5, 'june': 6, 'july': 7, 'august': 8,
            'september': 9, 'october': 10, 'november': 11, 'december': 12,
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        }

        today = date.today()

        # Match "Month Year" or "Month Day, Year"
        date_match = re.search(
            r'(january|february|march|april|may|june|july|august|'
            r'september|october|november|december|jan|feb|mar|apr|'
            r'jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})?,?\s*(\d{4})?',
            q, re.IGNORECASE
        )
        if date_match:
            month = month_map.get(date_match.group(1).lower(), 6)
            day = int(date_match.group(2)) if date_match.group(2) else 1
            year = int(date_match.group(3)) if date_match.group(3) else today.year
            try:
                target = date(year, month, day)
                if target < today:
                    target = date(year + 1, month, day) if year < today.year + 2 else target
                return (target - today).days
            except ValueError:
                pass

        # "before GTA VI" → estimate ~1.5 years
        if 'gta vi' in q or 'gta 6' in q:
            return 550  # ~1.5 years

        # "before [event]" → default 1 year
        if re.search(r'before\s+\w', q):
            return 365

        # "by end of [year]" 
        year_match = re.search(r'end of (\d{4})', q)
        if year_match:
            target = date(int(year_match.group(1)), 12, 31)
            return (target - today).days

        # "this year" / "in 2026" etc
        year_match = re.search(r'(?:in|by)\s+(\d{4})', q)
        if year_match:
            target = date(int(year_match.group(1)), 12, 31)
            return (target - today).days

        # Default: 1 year
        return 365

    def display(self, results: dict):
        """Pretty display of results."""
        now = datetime.now().strftime("%H:%M:%S")
        spot = results["btc_spot"]
        chg = results["btc_change_24h"]
        sign = "+" if chg >= 0 else ""
        color = "green" if chg >= 0 else "red"

        console.print()
        console.print(Panel(
            Text(f"₿ BTC ARBITRAGE BOT", style="bold yellow"),
            subtitle=f"{now} | BTC: [bold]${spot:,.0f}[/bold] [{color}]{sign}{chg:.1f}%[/{color}]"
        ))

        console.print(
            f"[dim]{results['markets_found']} BTC markets found | "
            f"{len(results['spread_arbs'])} spread arbs | "
            f"{len(results['threshold_signals'])} threshold signals | "
            f"{'PAPER TRADE' if self.paper_trade else 'READ-ONLY'}[/dim]"
        )
        console.print()

        # ── Threshold signals (most interesting) ─────────────────────
        if results["threshold_signals"]:
            console.print(f"[bold magenta]🎯 THRESHOLD MODEL SIGNALS:[/bold magenta]\n")
            table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold white")
            table.add_column("#", style="dim", width=3)
            table.add_column("Market", width=40)
            table.add_column("Target", justify="right", width=10)
            table.add_column("Model P", justify="right", style="cyan", width=8)
            table.add_column("Market P", justify="right", width=8)
            table.add_column("Edge", justify="right", style="bold magenta", width=8)
            table.add_column("Action", width=14)
            table.add_column("Size", justify="right", style="blue", width=6)

            for i, sig in enumerate(results["threshold_signals"], 1):
                a = f"[bold green]BUY YES[/bold green]" if sig.outcome == "Yes" else f"[bold red]BUY NO[/bold red]"
                table.add_row(
                    str(i), sig.market_question[:40],
                    f"${sig.target_price:,.0f}" if sig.target_price < 1e6 else f"${sig.target_price/1e6:.0f}M",
                    f"{sig.model_probability:.1%}",
                    f"{sig.market_price_yes:.1%}",
                    f"{sig.edge:.1%}",
                    a,
                    f"${sig.size_usdc:.0f}",
                )
            console.print(table)

            # Details
            for i, sig in enumerate(results["threshold_signals"], 1):
                console.print(f"\n[bold]Signal {i} detail:[/bold]")
                console.print(f"  {sig.explanation}")
                console.print(f"  BTC spot: ${sig.btc_spot:,.0f} | "
                             f"σ annual: {sig.annual_vol*100:.0f}% | "
                             f"Days to expiry: {sig.days_to_expiry}")
        else:
            console.print("[yellow]No threshold model signals found.[/yellow]")

        # ── Spread arbs ──────────────────────────────────────────────
        if results["spread_arbs"]:
            console.print(f"\n[bold green]💎 CLOB SPREAD ARBS:[/bold green]\n")
            table2 = Table(box=box.SIMPLE, show_header=True, header_style="bold")
            table2.add_column("Market", width=45)
            table2.add_column("YES", justify="right", width=7)
            table2.add_column("NO", justify="right", width=7)
            table2.add_column("Cost", justify="right", style="red", width=7)
            table2.add_column("Profit", justify="right", style="green", width=8)
            table2.add_column("Size", justify="right", width=8)

            for a in results["spread_arbs"][:10]:
                table2.add_row(
                    a.market_question[:45],
                    f"{a.best_ask_yes:.4f}", f"{a.best_ask_no:.4f}",
                    f"{a.buy_cost:.4f}",
                    f"+{a.buy_profit_pct:.2f}%",
                    f"${a.max_size_usdc:,.0f}",
                )
            console.print(table2)

        # ── Portfolio if paper trading ───────────────────────────────
        if self.paper_trade and self.db:
            portfolio = self.db.get_portfolio_state()
            trades = results["trades_opened"]
            console.print()
            console.print(Panel(
                f"💰 Portfolio: ${portfolio['total_value']:,.2f} | "
                f"Cash: ${portfolio['cash']:,.2f} | "
                f"Open: {portfolio['open_positions']} pos | "
                f"Opened this cycle: {trades}",
                title="📊 Paper Trading Portfolio",
                border_style="yellow"
            ))

            open_trades = self.db.get_open_trades()
            if open_trades:
                for t in open_trades:
                    console.print(
                        f"  #{t['id']} | {t['strategy']} | {t['outcome']} @ {t['entry_price']:.3f} "
                        f"| ${t['cost_basis']:.0f} | {t['market_question'][:60]}..."
                    )

    async def close(self):
        await self.gamma.close()
        await self.scanner.close()
        await self.feed.close()
        if self.db:
            self.db.close()


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--live", "-l", is_flag=True, help="Continuous scanning")
    @click.option("--trade", "-t", is_flag=True, help="Paper trade signals into DB")
    @click.option("--top", "-n", type=int, default=50, help="Max markets to fetch")
    @click.option("--interval", "-i", type=int, default=60, help="Seconds between scans")
    def main(live, trade, top, interval):
        runner = BTCRunner(top_n=top, paper_trade=trade)

        async def run():
            try:
                if live:
                    while True:
                        results = await runner.run_once()
                        console.clear()
                        runner.display(results)
                        console.print(f"\n[dim]Ctrl+C to stop | Next in {interval}s[/dim]")
                        await asyncio.sleep(interval)
                else:
                    results = await runner.run_once()
                    runner.display(results)
            except KeyboardInterrupt:
                console.print("\n[bold yellow]🛑 Stopped.[/bold yellow]")
            finally:
                await runner.close()

        asyncio.run(run())

    main()
