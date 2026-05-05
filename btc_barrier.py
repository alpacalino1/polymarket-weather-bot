#!/usr/bin/env python3
"""
BTC Barrier — live volatility and price-target probability monitor.

Shows in real-time:
- BTC spot price, 24h change, high/low
- Annualized volatility (realized from OHLCV)
- Barrier probabilities for common upside targets and downside dips
- Volatility cone (how σ changes over different lookback windows)

Zero API keys. Just CoinGecko for data, scipy for math.

Usage:
    python btc_barrier.py              # Single snapshot
    python btc_barrier.py --live       # Live updating every 20s
    python btc_barrier.py --interval 10
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import httpx
import numpy as np
from scipy.stats import norm
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from rich import box

console = Console()


# ── Math ──────────────────────────────────────────────────────────────────

def barrier_probability(
    spot: float,
    target: float,
    days: int,
    sigma_annual: float,
    drift: float = 0.0,
) -> float:
    """Probability of GBM hitting barrier within time T.

    Works for upward barriers (target > spot) and downward dips (target < spot).
    Uses the first-passage time formula for Brownian motion with drift.

    Args:
        spot: Current price
        target: Barrier price
        days: Time horizon in days
        sigma_annual: Annualized volatility
        drift: Annual drift assumption (default 0 = no drift)
    """
    T = days / 365.25
    nu = drift - sigma_annual**2 / 2
    sigma_sqrt_T = sigma_annual * np.sqrt(T)

    if sigma_annual == 0 or T <= 0 or spot <= 0:
        return 0.0

    if target > spot:
        # Upward barrier
        d1 = (np.log(spot / target) + nu * T) / sigma_sqrt_T
        d2 = (np.log(spot / target) - nu * T) / sigma_sqrt_T
        prob = norm.cdf(d1) + (target / spot) ** (2 * nu / sigma_annual**2) * norm.cdf(d2)
    else:
        # Downward barrier (dip)
        d1 = (np.log(target / spot) - nu * T) / sigma_sqrt_T
        d2 = (np.log(target / spot) + nu * T) / sigma_sqrt_T
        prob = norm.cdf(d1) + (target / spot) ** (2 * nu / sigma_annual**2) * norm.cdf(d2)

    return float(np.clip(prob, 0.0, 1.0))


def implied_move(sigma_annual: float, days: int, confidence: float = 0.68) -> float:
    """Expected price move (as percentage) over given days at given confidence.

    At 68% confidence = 1σ move. At 95% = ~2σ.
    """
    T = days / 365.25
    z = norm.ppf(confidence + (1 - confidence) / 2)
    return float(z * sigma_annual * np.sqrt(T))


# ── Data ──────────────────────────────────────────────────────────────────

async def fetch_btc_data() -> dict:
    """Fetch BTC spot + OHLCV from CoinGecko. Returns dict with all fields."""
    async with httpx.AsyncClient(timeout=15) as c:
        # Spot
        try:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bitcoin",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_24hr_vol": "true",
                    "include_market_cap": "true",
                    "include_24h_high_low": "true",
                },
            )
            coin = r.json().get("bitcoin", {})
            spot = coin.get("usd")
            change_24h = coin.get("usd_24h_change", 0) or 0
            vol_24h = coin.get("usd_24h_vol", 0) or 0
            mcap = coin.get("usd_market_cap", 0) or 0
        except Exception:
            return {"error": "CoinGecko API failed"}

        # OHLCV for volatility calculation
        vols = {}
        closes_all = None
        try:
            r2 = await c.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
                params={"vs_currency": "usd", "days": 365},
            )
            rows = r2.json()
            if rows and len(rows) > 30:
                all_closes = np.array([row[4] for row in rows])
                closes_all = all_closes

                for window_days, label in [(7, "7d"), (14, "14d"), (30, "30d"), (90, "90d"), (180, "180d")]:
                    if len(all_closes) >= window_days:
                        window_closes = all_closes[-window_days:]
                        log_ret = np.diff(np.log(window_closes))
                        vols[label] = float(np.std(log_ret) * np.sqrt(365.25))
        except Exception:
            pass

    return {
        "spot": spot,
        "change_24h": change_24h,
        "vol_24h": vol_24h,
        "market_cap": mcap,
        "volatilities": vols,
        "closes": closes_all,
        "fetched_at": datetime.now(),
    }


# ── Display ────────────────────────────────────────────────────────────────

def build_dashboard(data: dict, cycle: int, elapsed: float) -> Layout:
    """Build the Rich dashboard layout."""
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
    )

    now = datetime.now().strftime("%H:%M:%S")
    spot = data.get("spot")

    if spot is None:
        layout["header"].update(
            Panel(f"[red]API Error: {data.get('error', 'unknown')}[/red]", title="BTC Barrier")
        )
        return layout

    chg = data["change_24h"]
    sign = "+" if chg >= 0 else ""
    color = "green" if chg >= 0 else "red"
    vol_b = data.get("vol_24h", 0) / 1e9
    mcap_b = data.get("market_cap", 0) / 1e12

    layout["header"].update(
        Panel(
            Text(f"₿ BTC BARRIER — Live Volatility Monitor", style="bold yellow"),
            subtitle=(
                f"#{cycle} | {now} | "
                f"BTC: [bold]${spot:,.0f}[/bold] "
                f"[{color}]{sign}{chg:.1f}%[/{color}] | "
                f"Vol: ${vol_b:.1f}B | MCap: ${mcap_b:.2f}T | "
                f"Cycle: {elapsed:.1f}s"
            ),
        )
    )

    vols = data.get("volatilities", {})

    # Body: two columns
    body = Layout()
    body.split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1),
    )

    # ── LEFT: Barrier probability table ────────────────────────────
    sigma = vols.get("30d", vols.get("90d", 0.50))
    day_targets = [1, 3, 7, 14, 30, 60, 90]

    btable = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold white",
        title=f"Barrier Probabilities (σ={sigma*100:.0f}% ann, μ=0)",
        title_style="bold cyan",
    )
    btable.add_column("Target", justify="right", style="cyan", width=10)
    btable.add_column("Δ%", justify="right", width=7)

    for d in day_targets:
        btable.add_column(f"{d}d", justify="right", width=7)

    # Upside targets
    for pct in [5, 10, 15, 20, 30, 50]:
        target = spot * (1 + pct / 100)
        row = [
            f"↑ +{pct}%",
            f"${target:,.0f}" if target < 1e6 else f"${target/1e6:.1f}M",
        ]
        for d in day_targets:
            prob = barrier_probability(spot, target, d, sigma)
            if prob > 0.80:
                row.append(f"[bold white on dark_green] {prob*100:.0f}% [/bold white on dark_green]")
            elif prob > 0.60:
                row.append(f"[bold green]{prob*100:.0f}%[/bold green]")
            elif prob > 0.01:
                row.append(f"{prob*100:.1f}%")
            else:
                row.append(f"[dim]{prob*100:.2f}%[/dim]")
        btable.add_row(*row)

    btable.add_section()

    # Downside dips
    for pct in [5, 10, 15, 20, 30, 40]:
        target = spot * (1 - pct / 100)
        row = [
            f"↓ -{pct}%",
            f"${target:,.0f}" if target > 1000 else f"${target:.0f}",
        ]
        for d in day_targets:
            prob = barrier_probability(spot, target, d, sigma)
            if prob > 0.80:
                row.append(f"[bold white on dark_red] {prob*100:.0f}% [/bold white on dark_red]")
            elif prob > 0.60:
                row.append(f"[bold red]{prob*100:.0f}%[/bold red]")
            elif prob > 0.01:
                row.append(f"{prob*100:.1f}%")
            else:
                row.append(f"[dim]{prob*100:.2f}%[/dim]")
        btable.add_row(*row)

    body["left"].update(btable)

    # ── RIGHT: Volatility cone + expected moves ────────────────────
    right_lines = [
        "[bold]Volatility Cone[/bold]",
        "",
    ]

    for label in ["7d", "14d", "30d", "90d", "180d"]:
        v = vols.get(label)
        if v:
            bar_len = int(v * 100 / 3)
            bar = "█" * min(bar_len, 30)
            right_lines.append(f"  {label}: [cyan]{v*100:5.1f}%[/cyan] {bar}")

    right_lines += [
        "",
        "[bold]1σ Expected Moves[/bold]",
        f"  (68% confidence, σ={sigma*100:.0f}%)",
        "",
    ]

    for d in [1, 3, 7, 14, 30]:
        move = implied_move(sigma, d) * 100
        move_usd = spot * move / 100
        right_lines.append(f"  {d:2d}d: ±{move:.1f}% (${move_usd:,.0f})")

    right_lines += [
        "",
        "[bold]Price Context[/bold]",
        "",
        f"  Spot: ${spot:,.0f}",
    ]

    # 30d range based on vol
    range_30d = implied_move(sigma, 30) * 100
    upper = spot * (1 + range_30d / 100)
    lower = spot * (1 - range_30d / 100)
    right_lines.append(f"  30d 1σ range:")
    right_lines.append(f"    ${lower:,.0f} — ${upper:,.0f}")

    # 2σ event probability in 30 days
    prob_2sigma = barrier_probability(spot, spot * (1 + 2 * range_30d / 100), 30, sigma)
    right_lines.append(f"")
    right_lines.append(f"  2σ event in 30d: {prob_2sigma*100:.1f}%")

    # ── Trade signals ──────────────────────────────────────────
    signals_long = []
    signals_short = []
    for pct in [5, 10, 15, 20]:
        for d in [3, 7, 14, 30]:
            # Upside
            prob_up = barrier_probability(spot, spot * (1 + pct / 100), d, sigma)
            if prob_up > 0.60:
                signals_long.append((pct, d, prob_up))
            # Downside
            prob_dn = barrier_probability(spot, spot * (1 - pct / 100), d, sigma)
            if prob_dn > 0.60:
                signals_short.append((pct, d, prob_dn))

    if signals_long or signals_short:
        right_lines += ["", "[bold]🎯 Signals (>60% prob)[/bold]", ""]
        for pct, d, prob in signals_long[:5]:
            target = spot * (1 + pct / 100)
            right_lines.append(
                f"  [bold green]LONG[/bold green] +{pct}% in {d}d "
                f"({prob*100:.0f}%) → ${target:,.0f}"
            )
        for pct, d, prob in signals_short[:5]:
            target = spot * (1 - pct / 100)
            right_lines.append(
                f"  [bold red]SHORT[/bold red] -{pct}% in {d}d "
                f"({prob*100:.0f}%) → ${target:,.0f}"
            )

    right_lines += [
        "",
        "[dim]Data: CoinGecko[/dim]",
        "[dim]Model: GBM first-passage[/dim]",
        "[dim]μ=0, realized vol[/dim]",
    ]

    body["right"].update(
        Panel("\n".join(right_lines), title="📊 Stats", border_style="blue")
    )

    layout["body"].update(body)
    return layout


# ── Main ──────────────────────────────────────────────────────────────────

async def run_live(interval: int = 20):
    """Run the dashboard in live-updating mode."""
    cycle = 0
    console.clear()

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            cycle += 1
            t0 = time.time()

            data = await fetch_btc_data()
            elapsed = time.time() - t0

            layout = build_dashboard(data, cycle, elapsed)
            live.update(layout)

            wait = max(interval - elapsed, 3)
            await asyncio.sleep(wait)


async def run_snapshot():
    """Single snapshot and exit."""
    t0 = time.time()
    data = await fetch_btc_data()
    elapsed = time.time() - t0

    layout = build_dashboard(data, 1, elapsed)

    # For terminal output (non-TUI), render the layout manually
    console.print(layout["header"])
    console.print()

    spot = data.get("spot")
    vols = data.get("volatilities", {})
    sigma = vols.get("30d", vols.get("90d", 0.50))

    if spot:
        # Barrier table
        day_targets = [1, 3, 7, 14, 30, 60, 90]
        table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold white",
                     title=f"Barrier Probabilities (σ={sigma*100:.0f}% ann, μ=0)")
        table.add_column("Target", justify="right", style="cyan", width=10)
        table.add_column("Price", justify="right", width=10)
        for d in day_targets:
            table.add_column(f"{d}d", justify="right", width=7)

        for pct in [5, 10, 15, 20, 30, 50]:
            target = spot * (1 + pct / 100)
            row = [f"↑ +{pct}%", f"${target:,.0f}"]
            for d in day_targets:
                prob = barrier_probability(spot, target, d, sigma)
                if prob > 0.80:
                    row.append(f"[bold white on dark_green] {prob*100:.0f}% [/bold white on dark_green]")
                elif prob > 0.60:
                    row.append(f"[bold green]{prob*100:.0f}%[/bold green]")
                elif prob > 0.01:
                    row.append(f"{prob*100:.1f}%")
                else:
                    row.append(f"[dim]{prob*100:.2f}%[/dim]")
            table.add_row(*row)

        table.add_section()
        for pct in [5, 10, 15, 20, 30, 40]:
            target = spot * (1 - pct / 100)
            row = [f"↓ -{pct}%", f"${target:,.0f}"]
            for d in day_targets:
                prob = barrier_probability(spot, target, d, sigma)
                if prob > 0.80:
                    row.append(f"[bold white on dark_red] {prob*100:.0f}% [/bold white on dark_red]")
                elif prob > 0.60:
                    row.append(f"[bold red]{prob*100:.0f}%[/bold red]")
                elif prob > 0.01:
                    row.append(f"{prob*100:.1f}%")
                else:
                    row.append(f"[dim]{prob*100:.2f}%[/dim]")
            table.add_row(*row)

        console.print(table)
        console.print()

        # ── TRADE SIGNALS ──────────────────────────────────────────
        signal_rows = []
        for label, targets, side in [
            ("LONG", [5, 10, 15, 20, 30, 50], "up"),
            ("SHORT", [5, 10, 15, 20, 30, 40], "down"),
        ]:
            for pct in targets:
                target = spot * (1 + pct / 100) if side == "up" else spot * (1 - pct / 100)
                for d in [1, 3, 7, 14, 30]:
                    prob = barrier_probability(spot, target, d, sigma)
                    if prob > 0.80:
                        signal_rows.append((side, pct, d, prob, "🔥 STRONG", "bold white on dark_green" if side == "up" else "bold white on dark_red"))
                    elif prob > 0.60:
                        signal_rows.append((side, pct, d, prob, "📈 LONG" if side == "up" else "📉 SHORT", "bold green" if side == "up" else "bold red"))

        if signal_rows:
            console.print(f"[bold]🎯 TRADE SIGNALS (σ={sigma*100:.0f}%):[/bold]\n")
            sig_table = Table(box=box.SIMPLE, show_header=True, header_style="bold white")
            sig_table.add_column("Dir", width=6)
            sig_table.add_column("Move", width=6)
            sig_table.add_column("In", width=4)
            sig_table.add_column("Prob", justify="right", width=6)
            sig_table.add_column("Signal", width=16)

            for side, pct, d, prob, label, style in signal_rows[:12]:
                dir_str = "LONG" if side == "up" else "SHORT"
                move_str = f"+{pct}%" if side == "up" else f"-{pct}%"
                sig_table.add_row(
                    f"[cyan]{dir_str}[/cyan]" if side == "up" else f"[magenta]{dir_str}[/magenta]",
                    move_str, f"{d}d", f"[bold]{prob*100:.0f}%[/bold]",
                    f"[{style}]{label}[/{style}]",
                )

            console.print(sig_table)
            console.print()
        else:
            console.print("[dim]No high-conviction trade signals (all probabilities < 60%)[/dim]")
            console.print()

        # Vol cone
        console.print(f"[bold]Volatility:[/bold]")
        for label in ["7d", "14d", "30d", "90d", "180d"]:
            v = vols.get(label)
            if v:
                bar = "█" * min(int(v * 100 / 2), 40)
                console.print(f"  {label}: {v*100:5.1f}% {bar}")

        # Expected moves
        console.print(f"\n[bold]1σ Expected Moves (68% confidence):[/bold]")
        for d in [1, 3, 7, 14, 30]:
            move = implied_move(sigma, d) * 100
            move_usd = spot * move / 100
            console.print(f"  {d:2d}d: ±{move:.1f}% (±${move_usd:,.0f})")

        range_30d = implied_move(sigma, 30) * 100
        upper = spot * (1 + range_30d / 100)
        lower = spot * (1 - range_30d / 100)
        console.print(f"\n[bold]30d 1σ range:[/bold] ${lower:,.0f} — ${upper:,.0f}")

    console.print(f"\n[dim]Snapshot took {elapsed:.1f}s | CoinGecko API | GBM first-passage model[/dim]")


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--live", "-l", is_flag=True, help="Live updating TUI mode")
    @click.option("--interval", "-i", type=int, default=20, help="Seconds between updates")
    def main(live, interval):
        """₿ BTC Barrier — real-time volatility & price-target probability monitor."""
        if live:
            console.print("[bold yellow]Starting live BTC Barrier dashboard...[/bold yellow]")
            try:
                asyncio.run(run_live(interval))
            except KeyboardInterrupt:
                console.print("\n[bold yellow]🛑 Stopped.[/bold yellow]")
        else:
            asyncio.run(run_snapshot())

    main()
