"""
各テーブルへのCRUD操作。基本設計書6節のDB設計に対応する。

trading/ signals/ backtest/ の各データモデル（OrderResult, Position,
ClosedPositionResult, SignalEvent, DailyPnlState, BacktestResult）を
そのままSQLiteへ保存できる薄いアダプタとして提供する。
これらのモデル自体はsqlite3に依存しないままにしておくため、
変換・保存の責務はこのモジュールに閉じ込める。
"""

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from backtest.models import BacktestResult
from signals.models import SignalEvent
from trading.models import ClosedPositionResult, OrderResult, Position
from trading.risk_manager import DailyPnlState


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, result: OrderResult) -> None:
        self.conn.execute(
            """
            INSERT INTO orders
                (order_id, symbol_code, side, qty, status, reason,
                 requested_at, filled_at, filled_price, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                status=excluded.status, filled_at=excluded.filled_at,
                filled_price=excluded.filled_price, error_message=excluded.error_message
            """,
            (
                result.order_id or f"NOID-{result.requested_at.isoformat()}-{result.symbol}",
                result.symbol, result.side.value, result.qty, result.status.value,
                result.reason.value, _iso(result.requested_at), _iso(result.filled_at),
                result.filled_price, result.error_message,
            ),
        )
        self.conn.commit()

    def list_recent(self, limit: int = 100) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM orders ORDER BY requested_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()


class PositionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_open(self, position: Position) -> None:
        self.conn.execute(
            """
            INSERT INTO positions (symbol_code, qty, entry_price, entry_order_id, entry_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol_code) DO UPDATE SET
                qty=excluded.qty, entry_price=excluded.entry_price,
                entry_order_id=excluded.entry_order_id, entry_at=excluded.entry_at, status=excluded.status
            """,
            (position.symbol, position.qty, position.entry_price,
             position.entry_order_id, _iso(position.entry_at), position.status),
        )
        self.conn.commit()

    def remove(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol_code = ?", (symbol,))
        self.conn.commit()

    def list_open(self) -> List[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
        return cur.fetchall()


class ClosedTradeRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, closed: ClosedPositionResult) -> None:
        self.conn.execute(
            """
            INSERT INTO closed_trades
                (symbol_code, qty, entry_price, exit_price, entry_at, exit_at, reason, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (closed.symbol, closed.qty, closed.entry_price, closed.exit_price,
             _iso(closed.entry_at), _iso(closed.exit_at), closed.reason.value, closed.realized_pnl),
        )
        self.conn.commit()

    def list_by_date(self, trade_date: date) -> List[sqlite3.Row]:
        prefix = trade_date.isoformat()
        cur = self.conn.execute(
            "SELECT * FROM closed_trades WHERE exit_at LIKE ? ORDER BY exit_at", (f"{prefix}%",)
        )
        return cur.fetchall()


class SignalLogRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, event: SignalEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO signal_log (occurred_at, symbol_code, signal_type, rule_name, indicator_snapshot)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _iso(event.occurred_at), event.symbol, event.signal_type, event.rule_name,
                json.dumps(event.indicator_snapshot, ensure_ascii=False, default=str),
            ),
        )
        self.conn.commit()


class DailyPnlRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, state: DailyPnlState) -> None:
        self.conn.execute(
            """
            INSERT INTO daily_pnl (trade_date, realized_pnl, win_count, lose_count, daily_limit_hit, stopped_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                realized_pnl=excluded.realized_pnl, win_count=excluded.win_count,
                lose_count=excluded.lose_count, daily_limit_hit=excluded.daily_limit_hit,
                stopped_at=excluded.stopped_at
            """,
            (
                state.trade_date.isoformat(), state.realized_pnl, state.win_count,
                state.lose_count, state.daily_limit_hit, _iso(state.stopped_at),
            ),
        )
        self.conn.commit()

    def get(self, trade_date: date) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM daily_pnl WHERE trade_date = ?", (trade_date.isoformat(),)
        )
        return cur.fetchone()


class AppSettingsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat()),
        )
        self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        cur = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        return json.loads(row["value"])


class BacktestRunRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(
        self,
        result: BacktestResult,
        period_from: datetime,
        period_to: datetime,
        params: Dict[str, Any],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO backtest_runs
                (run_at, period_from, period_to, params_json, total_pnl, win_rate, max_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(), _iso(period_from), _iso(period_to),
                json.dumps(params, ensure_ascii=False, default=str),
                result.total_pnl, result.win_rate, result.max_drawdown,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_recent(self, limit: int = 20) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM backtest_runs ORDER BY run_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()
