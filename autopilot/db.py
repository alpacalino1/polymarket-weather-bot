"""
SQLite database layer for paper trading - trade logs, portfolio tracking,
and performance analytics.

Inspired by the Polymarket Autopilot blueprint:
https://github.com/hesamsheikh/awesome-openclaw-usecases
"""

import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL,        -- 'Yes' or 'No'
    side TEXT NOT NULL,            -- 'BUY' or 'SELL'
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity REAL NOT NULL,
    cost_basis REAL NOT NULL,      -- total USDC spent
    pnl REAL,                      -- realized P&L in USDC
    entry_time TIMESTAMPTZ NOT NULL DEFAULT (datetime('now')),
    exit_time TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open', 'closed', 'cancelled'
    notes TEXT,
    signal_json TEXT               -- original TradeSignal JSON for debugging
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    invested REAL NOT NULL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    open_positions INTEGER,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    trades_opened INTEGER DEFAULT 0,
    trades_closed INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0.0,
    starting_capital REAL,
    ending_capital REAL,
    win_rate REAL,
    best_strategy TEXT,
    best_strategy_pnl REAL
);

CREATE TABLE IF NOT EXISTS market_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    model_prob REAL,
    market_price REAL,
    edge REAL,
    action TEXT,  -- 'BUY_YES', 'BUY_NO', 'SKIP'
    check_time TIMESTAMPTZ NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AutopilotDB:
    """SQLite database for paper trading and autopilot tracking."""

    def __init__(self, db_path: str = "", starting_capital: float = 10_000):
        if not db_path:
            db_path = str(Path.home() / ".polymarket-weather-bot" / "autopilot.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.starting_capital = starting_capital
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        """Initialize the database schema."""
        self.conn.executescript(DB_SCHEMA)
        # Set starting capital if not already set
        self._set_config("starting_capital", str(self.starting_capital))
        self._set_config("initialized_at", datetime.now().isoformat())
        self.conn.commit()

    def _set_config(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
            (key, value)
        )

    def get_config(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # ── Trade Operations ───────────────────────────────────────────────

    def open_trade(
        self,
        condition_id: str,
        market_question: str,
        strategy: str,
        outcome: str,
        side: str,
        entry_price: float,
        quantity: float,
        signal_json: str = "",
    ) -> int:
        """Record a new paper trade. Returns trade ID."""
        cost_basis = round(entry_price * quantity, 2)
        cursor = self.conn.execute(
            """INSERT INTO paper_trades 
               (condition_id, market_question, strategy, outcome, side,
                entry_price, quantity, cost_basis, status, signal_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (condition_id, market_question, strategy, outcome, side,
             entry_price, quantity, cost_basis, signal_json)
        )
        self.conn.commit()
        return cursor.lastrowid

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        notes: str = "",
    ) -> Optional[dict]:
        """Close a paper trade and calculate P&L."""
        trade = self.conn.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not trade:
            return None

        # Calculate P&L
        entry_cost = trade["cost_basis"]
        exit_value = round(exit_price * trade["quantity"], 2)

        if trade["outcome"] == "Yes":
            pnl = round(exit_value - entry_cost, 2)
        else:
            # For NO positions: max payout per share is 1.0
            # If we bought NO at entry_price, profit = (1-p) * qty - cost
            # This handles it correctly: sell NO back at exit_price (which is NO price)
            pnl = round(exit_value - entry_cost, 2)

        self.conn.execute(
            """UPDATE paper_trades 
               SET exit_price = ?, pnl = ?, exit_time = datetime('now'),
                   status = 'closed', notes = ?
               WHERE id = ?""",
            (exit_price, pnl, notes, trade_id)
        )
        self.conn.commit()

        return {
            "trade_id": trade_id,
            "pnl": pnl,
            "entry_price": trade["entry_price"],
            "exit_price": exit_price,
            "quantity": trade["quantity"],
            "market_question": trade["market_question"],
            "strategy": trade["strategy"],
        }

    def cancel_trade(self, trade_id: int):
        """Cancel an open trade."""
        self.conn.execute(
            "UPDATE paper_trades SET status = 'cancelled' WHERE id = ?",
            (trade_id,)
        )
        self.conn.commit()

    def get_open_trades(self) -> list[dict]:
        """Get all currently open paper positions."""
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY entry_time"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades_for_market(self, condition_id: str) -> list[dict]:
        """Get open trades for a specific market."""
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' AND condition_id = ?",
            (condition_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Portfolio ──────────────────────────────────────────────────────

    def get_portfolio_state(self) -> dict:
        """Get current portfolio state."""
        cash = float(self.get_config("cash", str(self.starting_capital)))

        open_trades = self.get_open_trades()
        invested = sum(t["cost_basis"] for t in open_trades)

        # Sum realized P&L from closed trades
        closed = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM paper_trades WHERE status = 'closed'"
        ).fetchone()
        realized_pnl = float(closed["total_pnl"])

        total_value = cash + invested + realized_pnl

        return {
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_value": round(total_value, 2),
            "open_positions": len(open_trades),
            "starting_capital": self.starting_capital,
            "total_return_pct": round((total_value - self.starting_capital) 
                                       / self.starting_capital * 100, 2),
        }

    def snapshot_portfolio(self):
        """Save a portfolio snapshot."""
        state = self.get_portfolio_state()
        self.conn.execute(
            """INSERT INTO portfolio_snapshots 
               (total_value, cash, invested, realized_pnl, open_positions)
               VALUES (?, ?, ?, ?, ?)""",
            (state["total_value"], state["cash"], state["invested"],
             state["realized_pnl"], state["open_positions"])
        )
        self.conn.commit()

    def update_cash(self, new_cash: float):
        """Update available cash (after a trade)."""
        self._set_config("cash", str(round(new_cash, 2)))

    def deduct_cash(self, amount: float) -> float:
        """Deduct cash for a new position. Returns new cash balance."""
        cash = float(self.get_config("cash", str(self.starting_capital)))
        new_cash = cash - amount
        self._set_config("cash", str(round(new_cash, 2)))
        return new_cash

    def add_cash(self, amount: float) -> float:
        """Add cash (e.g., from closing a position)."""
        cash = float(self.get_config("cash", str(self.starting_capital)))
        new_cash = cash + amount
        self._set_config("cash", str(round(new_cash, 2)))
        return new_cash

    # ── Stats & Reporting ──────────────────────────────────────────────

    def get_all_time_stats(self) -> dict:
        """Get all-time performance stats."""
        total = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE status = 'closed'"
        ).fetchone()
        wins = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE status = 'closed' AND pnl > 0"
        ).fetchone()
        losses = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE status = 'closed' AND pnl < 0"
        ).fetchone()
        pnl = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM paper_trades WHERE status = 'closed'"
        ).fetchone()

        total_trades = total["cnt"]
        return {
            "total_trades": total_trades,
            "wins": wins["cnt"],
            "losses": losses["cnt"],
            "win_rate": round(wins["cnt"] / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl": round(pnl["total"], 2),
            "avg_win": round(
                self.conn.execute(
                    "SELECT COALESCE(AVG(pnl), 0) as avg FROM paper_trades WHERE status = 'closed' AND pnl > 0"
                ).fetchone()["avg"], 2
            ),
            "avg_loss": round(
                self.conn.execute(
                    "SELECT COALESCE(AVG(pnl), 0) as avg FROM paper_trades WHERE status = 'closed' AND pnl < 0"
                ).fetchone()["avg"], 2
            ),
        }

    def get_strategy_breakdown(self) -> list[dict]:
        """Get performance breakdown by strategy."""
        rows = self.conn.execute(
            """SELECT strategy, 
                      COUNT(*) as trades, 
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                      ROUND(COALESCE(SUM(pnl), 0), 2) as total_pnl,
                      ROUND(COALESCE(AVG(pnl), 0), 2) as avg_pnl
               FROM paper_trades WHERE status = 'closed'
               GROUP BY strategy ORDER BY total_pnl DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Get the most recent trades."""
        rows = self.conn.execute(
            "SELECT * FROM paper_trades ORDER BY entry_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trades_today(self) -> list[dict]:
        """Get today's trades."""
        today = date.today().isoformat()
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE date(entry_time) = ? ORDER BY entry_time DESC",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_daily_stats(self) -> dict:
        """Compute and save daily stats snapshot. Returns the stats dict."""
        today = date.today().isoformat()
        today_trades = self.conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN pnl > 0 AND status = 'closed' THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN pnl <= 0 AND status = 'closed' THEN 1 ELSE 0 END) as losses,
                      SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END) as pnl
               FROM paper_trades WHERE date(entry_time) = ?""",
            (today,)
        ).fetchone()

        portfolio = self.get_portfolio_state()

        # Find best strategy today
        best = self.conn.execute(
            """SELECT strategy, SUM(pnl) as spnl 
               FROM paper_trades 
               WHERE date(entry_time) = ? AND status = 'closed'
               GROUP BY strategy ORDER BY spnl DESC LIMIT 1""",
            (today,)
        ).fetchone()

        stats = {
            "date": today,
            "trades_opened": today_trades["total"] or 0,
            "trades_closed": (today_trades["wins"] or 0) + (today_trades["losses"] or 0),
            "wins": today_trades["wins"] or 0,
            "losses": today_trades["losses"] or 0,
            "realized_pnl": round(today_trades["pnl"] or 0, 2),
            "starting_capital": self.starting_capital,
            "ending_capital": portfolio["total_value"],
            "win_rate": (round((today_trades["wins"] or 0) / 
                         (((today_trades["wins"] or 0) + (today_trades["losses"] or 0)) or 1) * 100, 1)),
            "best_strategy": best["strategy"] if best else "",
            "best_strategy_pnl": round(best["spnl"], 2) if best else 0,
        }

        # Upsert daily stats
        self.conn.execute(
            """INSERT INTO daily_stats 
               (date, trades_opened, trades_closed, wins, losses, realized_pnl,
                starting_capital, ending_capital, win_rate, best_strategy, best_strategy_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               trades_opened = excluded.trades_opened,
               trades_closed = excluded.trades_closed,
               wins = excluded.wins,
               losses = excluded.losses,
               realized_pnl = excluded.realized_pnl,
               ending_capital = excluded.ending_capital,
               win_rate = excluded.win_rate,
               best_strategy = excluded.best_strategy,
               best_strategy_pnl = excluded.best_strategy_pnl""",
            (stats["date"], stats["trades_opened"], stats["trades_closed"],
             stats["wins"], stats["losses"], stats["realized_pnl"],
             stats["starting_capital"], stats["ending_capital"],
             stats["win_rate"], stats["best_strategy"], stats["best_strategy_pnl"])
        )
        self.conn.commit()
        return stats

    def record_market_check(
        self,
        condition_id: str,
        market_question: str,
        model_prob: float,
        market_price: float,
        edge: float,
        action: str,
    ):
        """Log a market evaluation (whether we traded or not)."""
        self.conn.execute(
            """INSERT INTO market_checks 
               (condition_id, market_question, model_prob, market_price, edge, action)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (condition_id, market_question, round(model_prob, 4),
             round(market_price, 4), round(edge, 4), action)
        )
        self.conn.commit()

    # ── Maintenance ────────────────────────────────────────────────────

    def close(self):
        self.conn.close()
