"""
backtest/ パッケージ共通のデータモデル。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from trading.models import ClosedPositionResult


@dataclass
class Bar:
    """1分足データ1本分。J-Quants API（分足）のCSVエクスポートを想定。"""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class EquityPoint:
    timestamp: datetime
    cumulative_pnl: float


@dataclass
class OrderLogEntry:
    """
    バックテスト実行中に発生したエントリー・決済（利確/損切/強制決済等）を
    時系列で記録するログ1行分。CSV出力・GUI表示の両方で使う。
    """

    timestamp: datetime
    event_type: str  # "ENTRY" / "EXIT"
    symbol: str
    qty: int
    price: float
    reason: str  # trading.OrderReason.value（SIGNAL/STOP_LOSS/FORCE_CLOSE_TIME/BACKTEST_END等）
    realized_pnl: Optional[float] = None  # EXITのみ設定（ENTRYはNone）


@dataclass
class BacktestResult:
    trades: List[ClosedPositionResult] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    log_entries: List[OrderLogEntry] = field(default_factory=list)
    """エントリー・決済を時系列で記録した詳細ログ。CSV出力する際はこれを使う。"""

    total_pnl: float = 0.0
    win_count: int = 0
    lose_count: int = 0
    max_drawdown: float = 0.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> Optional[float]:
        if self.trade_count == 0:
            return None
        return self.win_count / self.trade_count

    @property
    def average_win(self) -> Optional[float]:
        wins = [t.realized_pnl for t in self.trades if t.realized_pnl >= 0]
        return sum(wins) / len(wins) if wins else None

    @property
    def average_loss(self) -> Optional[float]:
        losses = [t.realized_pnl for t in self.trades if t.realized_pnl < 0]
        return sum(losses) / len(losses) if losses else None
