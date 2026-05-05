"""
Autopilot runner - integrates weather strategies with paper trading database.

Runs the full autopilot loop:
1. Scan Polymarket for weather markets
2. Evaluate each market with strategies
3. Execute paper trades in SQLite with portfolio tracking
4. Manage open positions (check for exits)
5. Generate performance summaries
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from autopilot.db import AutopilotDB
from strategies import TradeSignal
from polymarket.markets import parse_weather_market
from polymarket.client import GammaClient
from weather import OpenMeteoClient, RainViewerClient

logger = logging.getLogger("pm-weather-bot")


class AutopilotRunner:
    """Orchestrates the full paper trading autopilot."""

    def __init__(
        self,
        db: AutopilotDB,
        gamma: GammaClient,
        weather: OpenMeteoClient,
        radar: RainViewerClient,
        strategies: list,
        max_position: float = 100,
        min_edge: float = 0.05,
        min_volume: float = 1000,
        cooldown_minutes: int = 30,
        exit_edge_threshold: float = 0.02,  # Close positions when edge drops below this
        max_open_positions: int = 10,
    ):
        self.db = db
        self.gamma = gamma
        self.weather = weather
        self.radar = radar
        self.strategies = strategies
        self.max_position = max_position
        self.min_edge = min_edge
        self.min_volume = min_volume
        self.cooldown_minutes = cooldown_minutes
        self.exit_edge_threshold = exit_edge_threshold
        self.max_open_positions = max_open_positions
        self.last_checked: dict[str, datetime] = {}

    async def run_cycle(self) -> dict:
        """Run one full autopilot cycle: scan, evaluate, trade, manage exits."""
        cycle_start = datetime.now()
        results = {
            "markets_scanned": 0,
            "markets_evaluated": 0,
            "signals_found": 0,
            "trades_opened": 0,
            "trades_closed": 0,
            "pnl_change": 0.0,
            "cycle_time_seconds": 0,
        }

        # ── 1. Get radar context ──────────────────────────────────────
        try:
            radar_data = await self.radar.get_radar()
            logger.debug(f"Radar: intensity={radar_data.intensity:.2f}, trend={radar_data.trend}")
        except Exception:
            radar_data = None

        # ── 2. Discover weather markets ───────────────────────────────
        try:
            markets = await self.gamma.search_weather_markets(limit=100)
        except Exception as e:
            logger.error(f"Market discovery failed: {e}")
            return results

        active = [m for m in markets 
                  if not m.closed and m.volume_24h >= self.min_volume]
        results["markets_scanned"] = len(active)
        logger.info(f"📊 {len(active)} active weather markets found")

        # ── 3. Manage existing positions first ───────────────────────
        await self._manage_exits(results)

        # ── 4. Evaluate markets for new entries ──────────────────────
        portfolio = self.db.get_portfolio_state()
        available_cash = portfolio["cash"]

        if portfolio["open_positions"] >= self.max_open_positions:
            logger.debug(f"Max positions ({self.max_open_positions}) reached, skipping entries")
        elif available_cash < self.max_position:
            logger.debug(f"Insufficient cash (${available_cash:.0f}), skipping entries")
        else:
            for market in active:
                # Cooldown check
                last = self.last_checked.get(market.condition_id)
                if last and (datetime.now() - last) < timedelta(minutes=self.cooldown_minutes):
                    continue

                # Skip if already in a position
                existing = self.db.get_open_trades_for_market(market.condition_id)
                if existing:
                    continue

                # Parse question
                parsed = parse_weather_market(market.question, market.condition_id)
                if not parsed.is_weather or parsed.confidence < 0.4:
                    # Log the check anyway
                    self.db.record_market_check(
                        market.condition_id, market.question, 0, 0, 0, "SKIP"
                    )
                    continue

                yes_price = market.outcome_prices.get("Yes", 0.5)
                token_yes = market.token_ids.get("Yes", "")
                token_no = market.token_ids.get("No", "")

                try:
                    signal = await self._evaluate_signal(
                        parsed, yes_price, token_yes, token_no
                    )

                    if signal:
                        results["signals_found"] += 1
                        trade_opened = self._execute_paper_trade(signal)
                        if trade_opened:
                            results["trades_opened"] += 1

                    results["markets_evaluated"] += 1
                    self.last_checked[market.condition_id] = datetime.now()

                except Exception as e:
                    logger.debug(f"Error evaluating {market.question[:60]}: {e}")

        # ── 5. Snapshot portfolio ─────────────────────────────────────
        self.db.snapshot_portfolio()

        # ── 6. Daily stats at end of day ──────────────────────────────
        self.db.record_daily_stats()

        results["cycle_time_seconds"] = round(
            (datetime.now() - cycle_start).total_seconds(), 1
        )
        return results

    async def _evaluate_signal(
        self,
        parsed,
        market_price_yes: float,
        token_id_yes: str,
        token_id_no: str,
    ) -> Optional[TradeSignal]:
        """Try all strategies and return the best signal."""
        for strategy in self.strategies:
            try:
                signal = await strategy.evaluate(
                    parsed=parsed,
                    market_price_yes=market_price_yes,
                    token_id_yes=token_id_yes,
                    token_id_no=token_id_no,
                    max_position=self.max_position,
                )
                if signal and signal.edge >= self.min_edge:
                    return signal
            except Exception:
                continue
        return None

    def _execute_paper_trade(self, signal: TradeSignal) -> bool:
        """Record a paper trade in the database."""
        try:
            portfolio = self.db.get_portfolio_state()
            if portfolio["cash"] < signal.size_usdc:
                logger.debug(f"Insufficient cash for {signal.market_question[:50]}")
                return False

            # Deduct cash
            self.db.deduct_cash(signal.size_usdc)

            # Record trade
            trade_id = self.db.open_trade(
                condition_id=signal.condition_id,
                market_question=signal.market_question,
                strategy=signal.strategy,
                outcome=signal.outcome,
                side=signal.side,
                entry_price=signal.market_price,
                quantity=signal.size_usdc,
                signal_json=json.dumps({
                    "condition_id": signal.condition_id,
                    "market_price": signal.market_price,
                    "model_probability": signal.model_probability,
                    "edge": signal.edge,
                    "confidence": signal.confidence,
                    "explanation": signal.explanation,
                }),
            )

            logger.info(
                f"📝 PAPER #{trade_id}: {signal.strategy} {signal.side} "
                f"{signal.outcome} @ {signal.market_price:.3f} | "
                f"${signal.size_usdc:.0f} | edge={signal.edge:.1%} | "
                f"\"{signal.market_question[:60]}...\""
            )

            # Log market check
            action = f"BUY_{signal.outcome.upper()}"
            self.db.record_market_check(
                signal.condition_id, signal.market_question,
                signal.model_probability, signal.market_price,
                signal.edge, action
            )

            return True

        except Exception as e:
            logger.error(f"Paper trade failed: {e}")
            return False

    async def _manage_exits(self, results: dict):
        """Check open positions for exit signals."""
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        logger.debug(f"Managing {len(open_trades)} open positions")

        for trade in open_trades:
            try:
                should_close, exit_price, reason = await self._check_exit(trade)
                if should_close:
                    closed = self.db.close_trade(
                        trade["id"], exit_price, notes=reason
                    )
                    if closed:
                        # Return cost basis + P&L to cash
                        refund = trade["cost_basis"] + closed["pnl"]
                        self.db.add_cash(refund)

                        pnl_sign = "+" if closed["pnl"] >= 0 else ""
                        logger.info(
                            f"🔒 CLOSED #{trade['id']}: {pnl_sign}${closed['pnl']:.2f} | "
                            f"entry={trade['entry_price']:.3f} → exit={exit_price:.3f} | "
                            f"\"{trade['market_question'][:60]}...\" | reason: {reason}"
                        )
                        results["trades_closed"] += 1
                        results["pnl_change"] += closed["pnl"]

            except Exception as e:
                logger.debug(f"Exit check failed for trade #{trade['id']}: {e}")

        if results["trades_closed"] > 0:
            logger.info(
                f"Portfolio change this cycle: ${results['pnl_change']:+.2f} "
                f"({results['trades_closed']} trades closed)"
            )

    async def _check_exit(self, trade: dict) -> tuple[bool, float, str]:
        """Determine if a position should be closed.

        Exit conditions:
        1. Market has resolved (can't check without API call, so skip)
        2. Edge has shrunk below threshold (re-evaluate with fresh weather data)
        3. Position held too long (>7 days) - time decay exit
        """
        condition_id = trade["condition_id"]

        # Time-based exit: close positions held > 7 days
        try:
            entry_time = datetime.fromisoformat(
                trade["entry_time"].replace("Z", "+00:00")
            )
            age_days = (datetime.now() - entry_time).days
            if age_days > 7:
                # Exit at current "market price" - for paper, use original price
                # (in reality you'd fetch current price from Polymarket)
                return True, trade["entry_price"], f"Time exit ({age_days}d held)"
        except (ValueError, TypeError):
            pass

        # Re-evaluate the market with fresh weather data
        # Parse the question and get current forecast
        parsed = parse_weather_market(trade["market_question"], condition_id)

        if not parsed.is_weather or not parsed.city or not parsed.target_date:
            # Can't evaluate - hold position
            return False, 0, ""

        try:
            location = await self.weather.geocode_first(parsed.city, parsed.state)
            if not location:
                return False, 0, ""

            now = datetime.now()
            days_out = (parsed.target_date - now.date()).days
            if days_out < 0:
                # Event date passed! Close regardless.
                return True, 1.0 if trade["outcome"] == "Yes" else 0.0, "Event date passed"

            forecast = await self.weather.get_forecast(
                latitude=location.latitude,
                longitude=location.longitude,
                forecast_days=min(days_out + 2, 16),
            )

            # Find forecast for target date
            target_fc = None
            for df in forecast.daily:
                if df.date == parsed.target_date:
                    target_fc = df
                    break
            if not target_fc and forecast.daily:
                idx = min(days_out, len(forecast.daily) - 1)
                target_fc = forecast.daily[idx]

            if not target_fc:
                return False, 0, ""

            # Recalculate probability
            if parsed.metric == "temperature" and parsed.temp_threshold_c is not None:
                above = parsed.temp_direction != "below"
                current_prob = self.weather.temperature_probability(
                    target_fc, parsed.temp_threshold_c, above
                )
            elif parsed.metric in ("precipitation", "snowfall"):
                current_prob = self.weather.precipitation_probability(target_fc)
            else:
                current_prob = 0.5

            # Calculate current edge
            current_edge = abs(current_prob - trade["entry_price"])

            if current_edge < self.exit_edge_threshold:
                return True, trade["entry_price"], f"Edge decayed ({current_edge:.1%})"
            elif current_prob > 0.95 and trade["outcome"] == "Yes":
                # High conviction - take profit
                return True, current_prob, f"High conviction ({current_prob:.1%}) - take profit"
            elif current_prob < 0.05 and trade["outcome"] == "No":
                return True, 1 - current_prob, f"High conviction NO ({current_prob:.1%}) - take profit"

        except Exception as e:
            logger.debug(f"Exit re-evaluation error: {e}")

        return False, 0, ""

    def generate_summary(self) -> str:
        """Generate a markdown summary for daily reporting."""
        portfolio = self.db.get_portfolio_state()
        stats = self.db.get_all_time_stats()
        strategy_breakdown = self.db.get_strategy_breakdown()
        open_trades = self.db.get_open_trades()
        recent = self.db.get_recent_trades(5)

        lines = [
            "## 🌤️ Polymarket Weather Bot - Autopilot Summary",
            f"**{datetime.now().strftime('%Y-%m-%d %H:%M')}**",
            "",
            "### 📊 Portfolio",
            f"- **Total Value**: ${portfolio['total_value']:,.2f}",
            f"- **Cash**: ${portfolio['cash']:,.2f}",
            f"- **Invested**: ${portfolio['invested']:,.2f}",
            f"- **Realized P&L**: ${portfolio['realized_pnl']:+,.2f}",
            f"- **Return**: {portfolio['total_return_pct']:+.1f}% (from ${self.db.starting_capital:,.0f})",
            f"- **Open Positions**: {portfolio['open_positions']}",
            "",
            "### 📈 Performance",
            f"- **Total Trades**: {stats['total_trades']}",
            f"- **Win Rate**: {stats['win_rate']:.1f}% ({stats['wins']}W / {stats['losses']}L)",
            f"- **Total P&L**: ${stats['total_pnl']:+,.2f}",
            f"- **Avg Win**: ${stats['avg_win']:+,.2f}",
            f"- **Avg Loss**: ${stats['avg_loss']:+,.2f}",
        ]

        if strategy_breakdown:
            lines.append("")
            lines.append("### 🎯 Strategy Breakdown")
            lines.append("| Strategy | Trades | Wins | Losses | Total P&L | Avg P&L |")
            lines.append("|----------|--------|------|--------|-----------|---------|")
            for s in strategy_breakdown:
                lines.append(
                    f"| {s['strategy']} | {s['trades']} | {s['wins']} | "
                    f"{s['losses']} | ${s['total_pnl']:+,.2f} | ${s['avg_pnl']:+,.2f} |"
                )

        if open_trades:
            lines.append("")
            lines.append("### 🔓 Open Positions")
            for t in open_trades[:10]:
                q = (t["market_question"] or "?")[:60]
                lines.append(
                    f"- #{t['id']}: {t['strategy']} {t['side']} {t['outcome']} "
                    f"@ {t['entry_price']:.3f} | ${t['cost_basis']:.0f} | {q}..."
                )

        if recent:
            lines.append("")
            lines.append("### 🕐 Recent Activity")
            for t in recent:
                status = "🔒" if t["status"] == "closed" else "🔓"
                pnl_str = f"${t['pnl']:+,.2f}" if t["pnl"] else "-"
                q = (t["market_question"] or "?")[:60]
                lines.append(
                    f"- {status} #{t['id']}: {t['strategy']} {t['outcome']} | "
                    f"PNL: {pnl_str} | {q}..."
                )

        return "\n".join(lines)
