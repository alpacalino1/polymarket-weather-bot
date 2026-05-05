#!/usr/bin/env python3
"""
Live BTC-Polymarket dashboard. No fake trades, no paper trading.
Just real data from real APIs, honestly displayed.

Shows:
- BTC spot price (CoinGecko, live)
- All BTC-tagged Polymarket markets with real prices
- CLOB orderbook depth + spread
- Gamma-vs-CLOB price divergence
- 24h volume, liquidity, price moves

APIs: CoinGecko (free), Binance (free), Polymarket Gamma (free), Polymarket CLOB (free)
All public, no API keys. Safe at 30-second intervals.

Usage:
    python live.py
    python live.py --interval 30
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from rich import box

console = Console()

# ── Data Fetchers ──────────────────────────────────────────────────────────

async def fetch_btc_spot() -> dict:
    """Get live BTC price from CoinGecko + Binance."""
    result = {"price": None, "change": 0, "volume": 0, "source": "none", "error": ""}
    
    # Try Binance first (faster, no rate limit issues)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.binance.com/api/v3/ticker/24hr", 
                           params={"symbol": "BTCUSDT"})
            if r.status_code == 200:
                d = r.json()
                result["price"] = float(d["lastPrice"])
                result["change"] = float(d["priceChangePercent"])
                result["volume"] = float(d["quoteVolume"])
                result["high"] = float(d["highPrice"])
                result["low"] = float(d["lowPrice"])
                result["source"] = "Binance"
                return result
    except Exception as e:
        result["error"] = f"Binance: {e}"

    # Fallback to CoinGecko
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids": "bitcoin", "vs_currencies": "usd",
                                   "include_24hr_change": "true", "include_24hr_vol": "true"})
            if r.status_code == 200:
                d = r.json().get("bitcoin", {})
                result["price"] = d.get("usd")
                result["change"] = d.get("usd_24h_change", 0) or 0
                result["volume"] = d.get("usd_24h_vol", 0) or 0
                result["source"] = "CoinGecko"
                return result
    except Exception as e:
        result["error"] += f" | CoinGecko: {e}"

    return result


async def fetch_btc_markets() -> list[dict]:
    """Fetch BTC-related Polymarket markets with real prices."""
    import json as _json
    
    def parse_json(v):
        if isinstance(v, str):
            try: return _json.loads(v)
            except: return v
        return v

    markets = []
    async with httpx.AsyncClient(timeout=15) as c:
        # Search with multiple BTC terms, deduplicate
        seen_ids = set()
        for term in ["bitcoin", "btc"]:
            if len(markets) >= 30:
                break
            try:
                r = await c.get("https://gamma-api.polymarket.com/markets",
                               params={"search": term, "limit": 25, "closed": "false"})
                if r.status_code != 200:
                    continue
                for raw in r.json():
                    cid = raw.get("conditionId", "")
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    
                    q = raw.get("question", "").lower()
                    # Only keep actual BTC/crypto markets
                    if not any(kw in q for kw in ["bitcoin", "btc", "₿"]):
                        continue
                    if raw.get("closed"):
                        continue
                    
                    outcomes = parse_json(raw.get("outcomes", []))
                    prices = parse_json(raw.get("outcomePrices", []))
                    clob_ids = parse_json(raw.get("clobTokenIds", []))
                    
                    m = {
                        "condition_id": cid,
                        "question": raw.get("question", ""),
                        "volume_24h": float(raw.get("volume24hr", 0)),
                        "volume_total": float(raw.get("volume", 0)),
                        "liquidity": float(raw.get("liquidity", 0)),
                        "yes_price": float(prices[0]) if isinstance(prices, list) and len(prices) > 0 else None,
                        "no_price": float(prices[1]) if isinstance(prices, list) and len(prices) > 1 else None,
                        "token_yes": str(clob_ids[0]) if isinstance(clob_ids, list) and len(clob_ids) > 0 else "",
                        "token_no": str(clob_ids[1]) if isinstance(clob_ids, list) and len(clob_ids) > 1 else "",
                        "end_date": raw.get("endDate", ""),
                        "tags": raw.get("tags", []),
                    }
                    markets.append(m)
            except Exception:
                continue
    
    return markets


async def fetch_clob_depth(markets: list[dict]) -> list[dict]:
    """Add CLOB orderbook data to markets (best bid/ask, spread, depth)."""
    async with httpx.AsyncClient(timeout=10) as c:
        for i, m in enumerate(markets):
            tid_y = m.get("token_yes", "")
            tid_n = m.get("token_no", "")
            if not tid_y or not tid_n:
                m["clob_ask_yes"] = m.get("yes_price")
                m["clob_ask_no"] = m.get("no_price")
                m["clob_bid_yes"] = None
                m["clob_bid_no"] = None
                m["clob_spread"] = None
                continue

            try:
                # Fetch both orderbooks
                r_y = await c.get("https://clob.polymarket.com/book",
                                 params={"token_id": tid_y})
                r_n = await c.get("https://clob.polymarket.com/book",
                                 params={"token_id": tid_n})
                
                if r_y.status_code == 200 and r_n.status_code == 200:
                    ob_y = r_y.json()
                    ob_n = r_n.json()
                    
                    asks_y = ob_y.get("asks", [])
                    asks_n = ob_n.get("asks", [])
                    bids_y = ob_y.get("bids", [])
                    bids_n = ob_n.get("bids", [])
                    
                    m["clob_ask_yes"] = float(asks_y[0]["price"]) if asks_y else m.get("yes_price")
                    m["clob_ask_no"] = float(asks_n[0]["price"]) if asks_n else m.get("no_price")
                    m["clob_bid_yes"] = float(bids_y[0]["price"]) if bids_y else None
                    m["clob_bid_no"] = float(bids_n[0]["price"]) if bids_n else None
                    
                    # Ask depth at best level
                    m["depth_yes"] = float(asks_y[0]["size"]) if asks_y else 0
                    m["depth_no"] = float(asks_n[0]["size"]) if asks_n else 0
                    
                    # Total CLOB spread: YES ask + NO ask
                    ya = m["clob_ask_yes"] or 0.5
                    na = m["clob_ask_no"] or 0.5
                    m["clob_cost"] = ya + na
                    
                    # Spread from parity
                    m["clob_spread"] = 1.0 - (ya + na)
                else:
                    m["clob_ask_yes"] = m.get("yes_price")
                    m["clob_ask_no"] = m.get("no_price")
            except Exception:
                m["clob_ask_yes"] = m.get("yes_price")
                m["clob_ask_no"] = m.get("no_price")
            
            # Rate limit CLOB
            if i % 3 == 2:
                await asyncio.sleep(0.3)
    
    return markets


# ── Dashboard ──────────────────────────────────────────────────────────────

def build_dashboard(btc: dict, markets: list[dict], cycle: int, elapsed: float) -> Layout:
    """Build the Rich layout for the live dashboard."""
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    
    now = datetime.now().strftime("%H:%M:%S")
    
    # Header
    btc_price = btc.get("price")
    if btc_price:
        chg = btc.get("change", 0)
        sign = "+" if chg >= 0 else ""
        color = "green" if chg >= 0 else "red"
        src = btc.get("source", "?")
        btc_str = (f"BTC: [bold]${btc_price:,.0f}[/bold] "
                  f"[{color}]{sign}{chg:.1f}%[/{color}] "
                  f"via {src}")
        if btc.get("high"):
            btc_str += f" | H: ${btc['high']:,.0f} L: ${btc['low']:,.0f}"
        if btc.get("volume"):
            btc_str += f" | 24h Vol: ${btc['volume']/1e9:.1f}B"
    else:
        btc_str = f"[red]BTC feed error: {btc.get('error', 'unknown')}[/red]"
    
    layout["header"].update(Panel(
        Text(f"📡 LIVE BTC-POLYMARKET DASHBOARD", style="bold yellow"),
        subtitle=f"#{cycle} | {now} | {btc_str} | Cycle: {elapsed:.1f}s"
    ))
    
    # Body: markets table + summary
    body = Layout()
    body.split_row(
        Layout(name="markets", ratio=3),
        Layout(name="sidebar", ratio=1),
    )
    
    # Markets table
    mkt_table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold white",
        title=f"{len(markets)} BTC Markets on Polymarket",
        title_style="bold cyan",
    )
    mkt_table.add_column("#", style="dim", width=3, no_wrap=True)
    mkt_table.add_column("Market", width=32)
    mkt_table.add_column("YES", justify="right", width=6)
    mkt_table.add_column("NO", justify="right", width=6)
    mkt_table.add_column("CLOB Cost", justify="right", width=7)
    mkt_table.add_column("Δ", justify="right", width=6)
    mkt_table.add_column("Vol 24h", justify="right", width=7)
    mkt_table.add_column("Liq", justify="right", width=7)
    
    arb_count = 0
    total_vol = 0
    total_liq = 0
    
    for i, m in enumerate(markets, 1):
        yp = m.get("yes_price")
        np = m.get("no_price")
        ya = m.get("clob_ask_yes")
        na = m.get("clob_ask_no")
        cost = m.get("clob_cost")
        
        yes_str = f"{yp:.3f}" if yp else "?"
        no_str = f"{np:.3f}" if np else "?"
        
        # Delta: Gamma price divergence from CLOB
        gamma_yes = yp or 0
        clob_yes = ya or gamma_yes
        delta = gamma_yes - clob_yes
        
        if cost and cost < 0.99:
            cost_str = f"[bold green]{cost:.4f}[/bold green]"
            arb_count += 1
        else:
            cost_str = f"{cost:.4f}" if cost else "?"
        
        delta_str = f"{delta:+.3f}" if delta else "-"
        if abs(delta) > 0.01:
            delta_str = f"[bold yellow]{delta_str}[/bold yellow]"
        
        vol_str = f"${m['volume_24h']:,.0f}" if m.get("volume_24h") else "$0"
        liq_str = f"${m.get('liquidity', 0):,.0f}"
        
        total_vol += m.get("volume_24h", 0)
        total_liq += m.get("liquidity", 0)
        
        mkt_table.add_row(
            str(i), m["question"][:32],
            yes_str, no_str,
            cost_str, delta_str,
            vol_str, liq_str,
        )
    
    body["markets"].update(mkt_table)
    
    # Sidebar stats
    sidebar_lines = [
        f"[bold]Market Summary[/bold]",
        f"",
        f"BTC markets: {len(markets)}",
        f"",
        f"Total 24h volume:",
        f"  ${total_vol:,.0f}",
        f"",
        f"Total liquidity:",
        f"  ${total_liq:,.0f}",
        f"",
        f"CLOB arb opps:",
    ]
    
    if arb_count:
        sidebar_lines.append(f"  [bold green]🔥 {arb_count} found[/bold green]")
    else:
        sidebar_lines.append(f"  [dim]0 (markets efficient)[/dim]")
    
    sidebar_lines += [
        f"",
        f"[bold]BTC Context[/bold]",
        f"",
    ]
    
    if btc_price and markets:
        # Model barrier prob for the largest BTC market
        best = markets[0]
        target_match = None
        import re
        for m in markets:
            mm = re.search(r'\$([\d,]+)\s*(million|m|k)?', m["question"].lower())
            if mm:
                val = float(mm.group(1).replace(",", ""))
                unit = mm.group(2) or ""
                if "million" in unit or "m" in unit:
                    val *= 1_000_000
                elif "k" in unit:
                    val *= 1_000
                if val > btc_price:
                    target_match = val
                    break
        
        if target_match:
            ratio = target_match / btc_price
            sidebar_lines.append(f"Top target: ${target_match:,.0f}")
            sidebar_lines.append(f"  = {ratio:.1f}x from spot")
            sidebar_lines.append(f"")
            
            # Quick barrier probability
            import numpy as np
            from scipy.stats import norm
            sigma = 0.70  # annual
            T = 1.0  # 1 year estimate
            prob = 2 * norm.cdf(np.log(btc_price/target_match) / (sigma * np.sqrt(T)))
            prob = max(min(prob, 1.0), 0.0)
            sidebar_lines.append(f"P(hit in ~1yr): {prob*100:.1f}%")
            sidebar_lines.append(f"  (σ={sigma*100:.0f}% annual)")
    
    sidebar_lines += [
        f"",
        f"[dim]APIs: Binance, CoinGecko,[/dim]",
        f"[dim]Gamma, CLOB[/dim]",
        f"[dim]All free, no keys[/dim]",
    ]
    
    body["sidebar"].update(Panel(
        "\n".join(sidebar_lines),
        title="📊 Stats",
        border_style="blue",
    ))
    
    layout["body"].update(body)
    return layout


# ── Main Loop ──────────────────────────────────────────────────────────────

async def main_loop(interval: int = 30):
    """Run the live dashboard continuously."""
    cycle = 0
    console.clear()
    
    # Use Live display for real-time updates
    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            cycle += 1
            t0 = time.time()
            
            # Fetch all data in parallel
            btc_task = fetch_btc_spot()
            markets_task = fetch_btc_markets()
            
            btc, markets = await asyncio.gather(btc_task, markets_task)
            
            # Add CLOB depth
            if markets:
                markets = await fetch_clob_depth(markets)
            
            elapsed = time.time() - t0
            
            # Build and display
            layout = build_dashboard(btc, markets, cycle, elapsed)
            live.update(layout)
            
            # Wait
            wait = max(interval - elapsed, 5)
            await asyncio.sleep(wait)


if __name__ == "__main__":
    import click
    
    @click.command()
    @click.option("--interval", "-i", type=int, default=30, 
                  help="Seconds between refreshes (default: 30)")
    def main(interval):
        """📡 Live BTC-Polymarket Dashboard — real data, no fake trades."""
        console.print("[bold yellow]Starting live dashboard...[/bold yellow]")
        console.print(f"[dim]Interval: {interval}s | Ctrl+C to stop[/dim]")
        try:
            asyncio.run(main_loop(interval))
        except KeyboardInterrupt:
            console.print("\n[bold yellow]🛑 Stopped.[/bold yellow]")

    main()
