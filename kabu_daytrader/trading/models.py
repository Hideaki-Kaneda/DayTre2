"""
trading/ パッケージ共通のデータモデル。

DB設計（基本設計書6節 orders / positions テーブル）に対応させている。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    FAILED = "FAILED"


class OrderReason(str, Enum):
    """orders.reason カラムに対応。決済系の理由はRiskManagerが判定する。"""

    SIGNAL = "SIGNAL"
    STOP_LOSS = "STOP_LOSS"
    DAILY_PROFIT_TARGET = "DAILY_PROFIT_TARGET"
    DAILY_MAX_LOSS = "DAILY_MAX_LOSS"
    FORCE_CLOSE_TIME = "FORCE_CLOSE_TIME"
    PUSH_DISCONNECT = "PUSH_DISCONNECT"
    TRAILING_STOP = "TRAILING_STOP"
    """AR（当日の値幅目安）×倍率によるトレール決済。"""
    BACKTEST_END = "BACKTEST_END"
    """バックテストのデータ期間が尽きた時点で残っていたポジションを
    強制決済する場合に使用（本番では発生しない）。"""


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    status: OrderStatus
    reason: OrderReason
    requested_at: datetime
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    error_message: Optional[str] = None


@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    entry_order_id: str
    entry_at: datetime
    status: str = "OPEN"  # 'OPEN' / 'CLOSED'
    high_water_mark: float = 0.0
    """保有中に記録した最高値（トレール決済の基準）。open_position()時にentry_priceで初期化する。"""


@dataclass
class ClosedPositionResult:
    """決済完了時にPositionManagerが返す記録。日次損益集計等に使う。"""

    symbol: str
    qty: int
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    reason: OrderReason
    realized_pnl: float
