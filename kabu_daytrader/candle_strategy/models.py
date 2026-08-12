"""
ローソク足連続パターン戦略（candle_strategy）専用のデータモデル。

既存のtrading/signalsパッケージとは完全に独立させている
（ユーザー要望：「これは今までのエントリーと決済の処理とは分けてください」）。
命名・構造は既存のtrading.models/trading.risk_managerに似せてはいるが、
コード上の依存関係は持たない（duck typingで疎結合にする）。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class CandleDirection(str, Enum):
    """陽線（LONG方向）/ 陰線（SHORT方向）。始値と終値が同値の場合はNEUTRAL。"""

    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class CandleOrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    FAILED = "FAILED"


class CandleOrderReason(str, Enum):
    """orders系テーブルのreasonカラムに相当。既存trading.OrderReasonとは別物。"""

    ENTRY_CONSECUTIVE_CANDLES = "ENTRY_CONSECUTIVE_CANDLES"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    REVERSE_BARS = "REVERSE_BARS"
    FORCE_CLOSE_TIME = "FORCE_CLOSE_TIME"
    PUSH_DISCONNECT = "PUSH_DISCONNECT"
    BACKTEST_END = "BACKTEST_END"


@dataclass
class CandleOrderResult:
    order_id: str
    symbol: str
    direction: CandleDirection  # ポジション方向（エントリー/決済対象のポジションの向き）
    qty: int
    status: CandleOrderStatus
    reason: CandleOrderReason
    requested_at: datetime
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    error_message: Optional[str] = None


@dataclass
class CandlePosition:
    symbol: str
    direction: CandleDirection  # LONG（買い建て）/ SHORT（信用売り建て）
    qty: int
    entry_price: float
    entry_order_id: str
    entry_at: datetime
    status: str = "OPEN"
    reverse_bar_count: int = 0
    """エントリー方向と逆色のバーが確定するたびに加算する通算カウント
    （連続でなくてよい＝「通算」）。reverse_bars_m本に達したら決済する。"""


@dataclass
class CandleClosedPositionResult:
    symbol: str
    direction: CandleDirection
    qty: int
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    reason: CandleOrderReason
    realized_pnl: float


@dataclass
class CandleLogEntry:
    """バックテスト用の時系列ログ1行分（backtest.models.OrderLogEntryに相当）。"""

    timestamp: datetime
    event_type: str  # "ENTRY" / "EXIT"
    symbol: str
    direction: CandleDirection
    qty: int
    price: float
    reason: str
    realized_pnl: Optional[float] = None
