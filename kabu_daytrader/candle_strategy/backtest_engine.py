"""
CandleBacktestEngine：ローソク足連続パターン戦略専用のバックテスト実行エンジン。

既存のbacktest.BacktestEngine（RSI等ベースの戦略）とは完全に独立している。
分足データの読込・リサンプリング（backtest.data_loader）は共通の汎用基盤として
再利用するが、エントリー・決済の判定ロジックそのものは共有しない。

単独で実行する分には external_has_position は常にFalse（＝制約なし）として
動作する。既存戦略のバックテストと組み合わせて「同一銘柄では排他的」を
再現したい場合は、external_has_position に既存戦略側のPositionManagerの
has_position を渡すことで連携できる。
"""

from dataclasses import dataclass, field
from datetime import date, time
from typing import Callable, Dict, List, Optional

from backtest.data_loader import group_bars_by_timestamp
from backtest.models import Bar

from .broker_client import SimulatedMarginBrokerClient
from .models import CandleClosedPositionResult, CandleDirection, CandleLogEntry, CandleOrderReason, CandleOrderStatus
from .order_executor import CandleOrderExecutor
from .position_manager import CandlePositionManager
from .strategy_engine import CandleStrategyEngine


@dataclass
class CandleBacktestConfig:
    initial_buying_power: float = 1_000_000.0
    shares_per_symbol: int = 100
    consecutive_bars_n: int = 3
    """エントリーに必要な連続同色バー本数（既定3）。"""
    reverse_bars_m: int = 2
    """決済条件となる通算逆足バー本数（既定2）。"""
    take_profit_pct: float = 0.035
    """利確ライン（既定+3.5%）。"""
    stop_loss_pct: float = 0.015
    """損切ライン（既定-1.5%）。"""
    force_close_time: time = time(15, 0)
    external_has_position: Optional[Callable[[str], bool]] = None
    """他戦略が当該銘柄を保有中かを確認する関数。指定しなければ単独運用（制約なし）。"""


@dataclass
class CandleEquityPoint:
    timestamp: object
    cumulative_pnl: float


@dataclass
class CandleBacktestResult:
    trades: List[CandleClosedPositionResult] = field(default_factory=list)
    equity_curve: List[CandleEquityPoint] = field(default_factory=list)
    log_entries: List[CandleLogEntry] = field(default_factory=list)
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


class CandleBacktestEngine:
    def __init__(self, config: CandleBacktestConfig):
        self.config = config
        self._trades: List[CandleClosedPositionResult] = []
        self._log_entries: List[CandleLogEntry] = []

    def run(self, bars: List[Bar]) -> CandleBacktestResult:
        strategy_engine = CandleStrategyEngine(consecutive_bars_n=self.config.consecutive_bars_n)
        broker = SimulatedMarginBrokerClient(initial_buying_power=self.config.initial_buying_power)
        position_manager = CandlePositionManager()

        self._trades = []
        self._log_entries = []
        executor = CandleOrderExecutor(
            broker=broker,
            position_manager=position_manager,
            shares_per_symbol=self.config.shares_per_symbol,
            take_profit_pct=self.config.take_profit_pct,
            stop_loss_pct=self.config.stop_loss_pct,
            reverse_bars_m=self.config.reverse_bars_m,
            external_has_position=self.config.external_has_position,
            on_position_closed=self._on_position_closed,
        )

        equity_curve: List[CandleEquityPoint] = []
        last_prices: Dict[str, float] = {}

        groups = group_bars_by_timestamp(bars)

        for timestamp, bar_group in groups:
            broker.set_current_time(timestamp)

            # 1) 全銘柄の連続カウント更新＋価格反映
            for bar in bar_group:
                strategy_engine.update(bar)
                broker.set_current_price(bar.symbol, bar.close)
                last_prices[bar.symbol] = bar.close

            # 2) 保有中銘柄：逆足カウント更新 → 決済判定（損切→利確→逆足M本）
            for bar in bar_group:
                position = position_manager.get_position(bar.symbol)
                if position is None:
                    continue
                from .strategy_engine import bar_direction
                if bar_direction(bar) != CandleDirection.NEUTRAL and bar_direction(bar) != position.direction:
                    position_manager.record_reverse_bar(bar.symbol)

                result = executor.check_exit_conditions(bar.symbol, bar.close)
                if result is not None and result.status == CandleOrderStatus.FILLED:
                    self._log_exit(result, timestamp)

            # 3) 未保有銘柄：エントリー判定
            for bar in bar_group:
                if position_manager.has_position(bar.symbol):
                    continue
                direction = strategy_engine.evaluate_entry(bar.symbol)
                if direction is not None:
                    result = executor.try_entry(bar.symbol, direction, bar.close)
                    if result is not None and result.status == CandleOrderStatus.FILLED:
                        self._log_entry(result, timestamp)

            # 4) 強制引け決済
            if timestamp.time() >= self.config.force_close_time:
                closed_results = executor.close_all_positions(
                    CandleOrderReason.FORCE_CLOSE_TIME, price_lookup=lambda s: last_prices.get(s, 0.0)
                )
                for r in closed_results:
                    if r.status == CandleOrderStatus.FILLED:
                        self._log_exit(r, timestamp)

            equity_curve.append(CandleEquityPoint(timestamp=timestamp, cumulative_pnl=self._cumulative_pnl()))

        if position_manager.get_all_positions():
            closed_results = executor.close_all_positions(
                CandleOrderReason.BACKTEST_END, price_lookup=lambda s: last_prices.get(s, 0.0)
            )
            for r in closed_results:
                if r.status == CandleOrderStatus.FILLED:
                    self._log_exit(r, groups[-1][0] if groups else None)
            if groups:
                equity_curve.append(CandleEquityPoint(timestamp=groups[-1][0], cumulative_pnl=self._cumulative_pnl()))

        return self._build_result(equity_curve)

    def _cumulative_pnl(self) -> float:
        return sum(t.realized_pnl for t in self._trades)

    def _on_position_closed(self, closed: CandleClosedPositionResult) -> None:
        self._trades.append(closed)

    def _log_entry(self, result, timestamp) -> None:
        self._log_entries.append(
            CandleLogEntry(
                timestamp=result.filled_at or timestamp, event_type="ENTRY", symbol=result.symbol,
                direction=result.direction, qty=result.qty, price=result.filled_price,
                reason=result.reason.value, realized_pnl=None,
            )
        )

    def _log_exit(self, result, timestamp) -> None:
        # 直近のクローズ結果から実現損益を引く（同一銘柄・直近1件と仮定）
        matching = next((t for t in reversed(self._trades) if t.symbol == result.symbol), None)
        realized_pnl = matching.realized_pnl if matching is not None else None
        self._log_entries.append(
            CandleLogEntry(
                timestamp=result.filled_at or timestamp, event_type="EXIT", symbol=result.symbol,
                direction=result.direction, qty=result.qty, price=result.filled_price,
                reason=result.reason.value, realized_pnl=realized_pnl,
            )
        )

    def _build_result(self, equity_curve: List[CandleEquityPoint]) -> CandleBacktestResult:
        total_pnl = self._cumulative_pnl()
        win_count = sum(1 for t in self._trades if t.realized_pnl >= 0)
        lose_count = sum(1 for t in self._trades if t.realized_pnl < 0)

        max_drawdown = 0.0
        peak = 0.0
        for point in equity_curve:
            peak = max(peak, point.cumulative_pnl)
            max_drawdown = max(max_drawdown, peak - point.cumulative_pnl)

        return CandleBacktestResult(
            trades=list(self._trades), equity_curve=equity_curve,
            log_entries=sorted(self._log_entries, key=lambda e: e.timestamp),
            total_pnl=total_pnl, win_count=win_count, lose_count=lose_count, max_drawdown=max_drawdown,
        )
