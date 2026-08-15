"""
BacktestEngine：本番と同一の SignalEngine / RiskManager / OrderExecutor を
再利用して、過去の分足データ上でシミュレーションを行う。

基本設計書4.8節の要件（本番ロジックとバックテストロジックの共通化）を
実現するため、本番監視ループが呼ぶのと同じ trading.OrderExecutor /
trading.RiskManager / signals.SignalEngine をそのまま使う。
異なるのはデータの供給元（PUSH API か 分足CSV か）だけである。
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from indicators import PriceTick
from signals import SignalEngine
from trading import (
    ClosedPositionResult,
    OrderExecutor,
    OrderReason,
    OrderStatus,
    PositionDirection,
    PositionManager,
    RiskManager,
    SimulatedBrokerClient,
)

from .data_loader import group_bars_by_timestamp
from .models import Bar, BacktestResult, EquityPoint, OrderLogEntry


@dataclass
class BacktestConfig:
    indicator_config: Dict[str, Dict[str, Any]]
    entry_rule: Dict[str, Any]
    exit_rule: Dict[str, Any]
    entry_rule_short: Optional[Dict[str, Any]] = None
    """売り＝信用新規売り（SHORT）のエントリールール。省略時は売り側は一切発火しない。"""
    exit_rule_short: Optional[Dict[str, Any]] = None
    """売り（SHORT）の決済ルール。"""
    initial_buying_power: float = 1_000_000.0
    shares_per_symbol: int = 100
    stop_loss_pct: float = 0.97
    """
    【廃止・後方互換のみ】固定%損切ライン。既定の運用フローではAR×倍率の
    トレール決済（trailing_multiplier）に置き換えられており、本エンジンの
    メインループからは呼び出されない（RiskManagerインスタンスには渡すが未使用）。
    """
    daily_profit_target: float = 20_000.0
    daily_max_loss: float = 20_000.0
    force_close_time: time = time(15, 0)
    entry_start_time: time = time(9, 0)
    """この時刻より前は新規エントリーを行わない（既定09:00＝実質無制限）。"""
    trailing_multiplier: float = 2.0
    """トレール決済の基準幅 = AR × trailing_multiplier。未設定なら既定の2.0（AR×2）で運用する。"""
    reentry_cooldown_bars: int = 3
    """決済後、同一銘柄への再エントリーを禁止するバー本数（既定3本）。0で無効化。"""
    ar_indicator_key: Optional[str] = None
    """
    indicator_config内で opening_range_ar 型の指標を使っているキー名。
    Noneの場合、indicator_configから type=="opening_range_ar" の指標を自動検出する
    （複数ある場合は最初に見つかったものを使う）。見つからなければトレール決済は
    発動しない（ar_valueが常にNoneのため）。
    """
    symbol_ranks: Optional[Dict[str, int]] = None
    """スクリーナー順位。{"9432": 1, "7203": 2, ...}
    未指定の銘柄は最下位（順位付けなし）として扱われる。"""


def _find_ar_indicator_key(indicator_config: Dict[str, Dict[str, Any]]) -> Optional[str]:
    for key, spec in indicator_config.items():
        if spec.get("type") == "opening_range_ar":
            return key
    return None


class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self._trades: List[ClosedPositionResult] = []
        self._log_entries: List[OrderLogEntry] = []

    def run(self, bars: List[Bar]) -> BacktestResult:
        signal_engine = SignalEngine(
            indicator_config=self.config.indicator_config,
            entry_rule=self.config.entry_rule,
            exit_rule=self.config.exit_rule,
            entry_rule_short=self.config.entry_rule_short,
            exit_rule_short=self.config.exit_rule_short,
        )
        broker = SimulatedBrokerClient(initial_buying_power=self.config.initial_buying_power)
        position_manager = PositionManager()
        risk_manager = RiskManager(
            stop_loss_pct=self.config.stop_loss_pct,
            daily_profit_target=self.config.daily_profit_target,
            daily_max_loss=self.config.daily_max_loss,
            force_close_time=self.config.force_close_time,
            entry_start_time=self.config.entry_start_time,
            trailing_multiplier=self.config.trailing_multiplier,
            reentry_cooldown_bars=self.config.reentry_cooldown_bars,
        )
        ar_indicator_key = self.config.ar_indicator_key or _find_ar_indicator_key(self.config.indicator_config)

        self._trades = []
        self._log_entries = []
        executor = OrderExecutor(
            broker=broker,
            position_manager=position_manager,
            risk_manager=risk_manager,
            shares_per_symbol=self.config.shares_per_symbol,
            on_position_closed=self._on_position_closed,
        )

        symbol_ranks = self.config.symbol_ranks or {}

        def rank_of(symbol: str) -> int:
            return symbol_ranks.get(symbol, len(symbol_ranks) + 1)

        equity_curve: List[EquityPoint] = []
        current_date: Optional[date] = None
        last_prices: Dict[str, float] = {}

        groups = group_bars_by_timestamp(bars)

        for timestamp, bar_group in groups:
            if current_date != timestamp.date():
                current_date = timestamp.date()
                risk_manager.start_new_trading_day(current_date)

            # 1) 指標更新（全銘柄）＋ シミュレータへの現在値・現在時刻反映
            broker.set_current_time(timestamp)
            for bar in bar_group:
                tick = PriceTick(
                    symbol=bar.symbol, timestamp=timestamp, price=bar.close, volume=bar.volume,
                    open=bar.open, high=bar.high, low=bar.low,
                )
                signal_engine.update(tick)
                risk_manager.tick_reentry_cooldown(bar.symbol)
                broker.set_current_price(bar.symbol, bar.close)
                last_prices[bar.symbol] = bar.close

            # 2) 保有中銘柄：最高値更新 → トレール決済 → exit_ruleの順で判定
            #    （方向がLONGならexit_rule、SHORTならexit_rule_shortを使う）
            for bar in bar_group:
                position = position_manager.get_position(bar.symbol)
                if position is None:
                    continue
                position_manager.update_high_water_mark(bar.symbol, bar.close)
                ar_value = None
                if ar_indicator_key is not None:
                    ar_value = signal_engine.get_context(bar.symbol).snapshot().get(ar_indicator_key, {}).get("ar")
                result = executor.check_and_apply_trailing_stop(bar.symbol, bar.close, ar_value)
                if result is not None:
                    continue  # トレール決済済みならexit_rule判定は不要

                if position.direction == PositionDirection.LONG:
                    exit_event = signal_engine.evaluate_exit(bar.symbol)
                else:
                    exit_event = signal_engine.evaluate_exit_short(bar.symbol)
                if exit_event is not None:
                    executor.try_exit(bar.symbol, bar.close, OrderReason.SIGNAL)

            # 3) 未保有銘柄：entry_rule（買い）・entry_rule_short（売り）を満たした
            #    候補をランク順に発注（両方向の候補が混在してよい）
            entry_candidates = []
            for bar in bar_group:
                if position_manager.has_position(bar.symbol):
                    continue
                entry_event = signal_engine.evaluate_entry(bar.symbol)
                if entry_event is not None:
                    entry_candidates.append((bar.symbol, bar.close, PositionDirection.LONG))
                    continue  # 同一バーで買い・売り両方は成立させない（先に買いを優先）
                entry_event_short = signal_engine.evaluate_entry_short(bar.symbol)
                if entry_event_short is not None:
                    entry_candidates.append((bar.symbol, bar.close, PositionDirection.SHORT))
            entry_candidates.sort(key=lambda spd: rank_of(spd[0]))
            if entry_candidates:
                entry_results = executor.try_entries_in_rank_order(entry_candidates, now=timestamp)
                for result in entry_results:
                    self._log_entry_event(result)

            # 4) 日次上限・強制引け決済のチェック（RiskManagerのトリガーに従う）
            executor.check_forced_liquidation(timestamp, price_lookup=lambda s: last_prices.get(s, 0.0))

            equity_curve.append(EquityPoint(timestamp=timestamp, cumulative_pnl=self._cumulative_pnl()))

        # データ期間終了時点で残っているポジションは強制決済して損益を確定させる
        if position_manager.get_all_positions():
            executor.close_all_positions(
                OrderReason.BACKTEST_END, price_lookup=lambda s: last_prices.get(s, 0.0)
            )
            if groups:
                equity_curve.append(
                    EquityPoint(timestamp=groups[-1][0], cumulative_pnl=self._cumulative_pnl())
                )

        return self._build_result(equity_curve)

    def _cumulative_pnl(self) -> float:
        return sum(t.realized_pnl for t in self._trades)

    def _on_position_closed(self, closed: ClosedPositionResult) -> None:
        """OrderExecutorから決済確定のたびに呼ばれる。取引履歴とログの両方に記録する。"""
        self._trades.append(closed)
        self._log_entries.append(
            OrderLogEntry(
                timestamp=closed.exit_at,
                event_type="EXIT",
                symbol=closed.symbol,
                qty=closed.qty,
                price=closed.exit_price,
                reason=closed.reason.value,
                realized_pnl=closed.realized_pnl,
            )
        )

    def _log_entry_event(self, result) -> None:
        if result.status != OrderStatus.FILLED or result.filled_price is None:
            return
        self._log_entries.append(
            OrderLogEntry(
                timestamp=result.filled_at,
                event_type="ENTRY",
                symbol=result.symbol,
                qty=result.qty,
                price=result.filled_price,
                reason=result.reason.value,
                realized_pnl=None,
            )
        )

    def _build_result(self, equity_curve: List[EquityPoint]) -> BacktestResult:
        total_pnl = self._cumulative_pnl()
        win_count = sum(1 for t in self._trades if t.realized_pnl >= 0)
        lose_count = sum(1 for t in self._trades if t.realized_pnl < 0)

        max_drawdown = 0.0
        peak = 0.0
        for point in equity_curve:
            peak = max(peak, point.cumulative_pnl)
            drawdown = peak - point.cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)

        log_entries = sorted(self._log_entries, key=lambda e: e.timestamp)

        return BacktestResult(
            trades=list(self._trades),
            equity_curve=equity_curve,
            log_entries=log_entries,
            total_pnl=total_pnl,
            win_count=win_count,
            lose_count=lose_count,
            max_drawdown=max_drawdown,
        )
