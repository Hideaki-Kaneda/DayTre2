"""
SignalEngine：指標群の更新とentry_rule/exit_ruleの判定を行う中核モジュール。

基本設計書3節・4.3節の要件に対応:
  - 本番監視・バックテストの両方から同一ロジックを呼び出せること
    （PriceTickを順次投入していくだけで動作するため、本番/バックテストで
    分岐しない。データの供給元（PUSH API か CSV）だけが異なる）
  - 指標の拡張は indicators/registry.py 側に追加するだけで、
    このSignalEngineの実装は変更不要
"""

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from typing import Any, Dict, List, Optional

from indicators import Indicator, PriceTick, create_indicator

from .models import SignalEvent
from .rule_config import RuleGroup, load_rule


@dataclass
class SymbolContext:
    """銘柄ごとの指標インスタンス群と最新Tickを保持する。"""

    symbol: str
    indicators: Dict[str, Indicator] = dataclass_field(default_factory=dict)
    last_tick: Optional[PriceTick] = None

    def update(self, tick: PriceTick) -> None:
        self.last_tick = tick
        for indicator in self.indicators.values():
            indicator.update(tick)

    def all_ready(self) -> bool:
        return all(ind.is_ready() for ind in self.indicators.values())

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """{"sma_short": {"sma5": 1234.5}, "rsi14": {"rsi14": 28.3}, ...} の形で返す。"""
        return {key: ind.value() for key, ind in self.indicators.items()}

    def reset(self) -> None:
        for ind in self.indicators.values():
            ind.reset()
        self.last_tick = None


class SignalEngine:
    def __init__(
        self,
        indicator_config: Dict[str, Dict[str, Any]],
        entry_rule: Dict[str, Any] | None,
        exit_rule: Dict[str, Any] | None,
    ):
        """
        indicator_config: 設定ファイルの "indicators" セクション。例:
            {
                "sma_short": {"type": "sma", "period": 5},
                "sma_long":  {"type": "sma", "period": 25},
                "rsi14":     {"type": "rsi", "period": 14},
                "bb":        {"type": "bollinger", "period": 20, "num_std": 2},
                "vwap":      {"type": "vwap"}
            }
        entry_rule / exit_rule: rule_config.RuleGroup.from_dict が読める形式のdict
        """
        self._indicator_config = indicator_config
        self.entry_rule: RuleGroup = load_rule(entry_rule)
        self.exit_rule: RuleGroup = load_rule(exit_rule)
        self._contexts: Dict[str, SymbolContext] = {}

    # ------------------------------------------------------------------
    # 銘柄管理
    # ------------------------------------------------------------------
    def register_symbol(self, symbol: str) -> SymbolContext:
        if symbol in self._contexts:
            return self._contexts[symbol]

        indicators = {
            key: create_indicator(spec["type"], {k: v for k, v in spec.items() if k != "type"})
            for key, spec in self._indicator_config.items()
        }
        ctx = SymbolContext(symbol=symbol, indicators=indicators)
        self._contexts[symbol] = ctx
        return ctx

    def unregister_symbol(self, symbol: str) -> None:
        self._contexts.pop(symbol, None)

    def get_context(self, symbol: str) -> SymbolContext:
        return self._contexts.get(symbol) or self.register_symbol(symbol)

    # ------------------------------------------------------------------
    # Tick取り込み
    # ------------------------------------------------------------------
    def update(self, tick: PriceTick) -> None:
        ctx = self.get_context(tick.symbol)
        ctx.update(tick)

    def warmup_symbol(
        self, symbol: str, seed_price: float, timestamp: datetime, max_ticks: int = 200
    ) -> int:
        """
        指標がis_ready()になるまで、同一価格のTickを繰り返し投入してウォームアップする。
        より精度の高いウォームアップには warmup_symbol_from_summary() を使うこと。
        こちらは、前日終値以外の要約値（前日RSI・BB等）が手元にない場合の
        簡易フォールバックとして残している。

        全指標がis_ready()になるか、max_ticksに達したら停止し、実際に
        投入した本数を返す。
        """
        ctx = self.get_context(symbol)
        count = 0
        while not ctx.all_ready() and count < max_ticks:
            tick = PriceTick(symbol=symbol, timestamp=timestamp, price=seed_price)
            ctx.update(tick)
            count += 1
        return count

    def warmup_symbol_from_bars(self, symbol: str, bars: List) -> Dict[str, bool]:
        """
        実際の過去の確定バー（backtest.models.Bar等、symbol/timestamp/open/high/low/
        close/volumeを持つオブジェクト）を時系列順に1本ずつ通常のupdate()へ
        流し込み、指標をウォームアップする。

        warmup_symbol_from_summary()（前日の要約値からseed()で近似復元する方式）
        と違い、実データそのものを使うため、対応する指標（SMA/RSI/BB等）は
        より正確な状態を再現できる。

        VWAP・AR（opening_range_ar）のように日次でリセットされる前提の指標は、
        過去の日付のバーを流し込んでもis_ready()にはならない（＝当日の実データが
        必要なため、これは想定どおりであり不具合ではない）。そのため、この
        メソッドの戻り値だけで「ウォームアップ全体が成功したか」を判断せず、
        指標ごとのis_ready()を見て、まだ準備できていない指標だけ
        warmup_symbol_from_summary()等の別手段で補うこと。

        戻り値: {指標キー: 実データ投入後にis_ready()になったか} の辞書
        """
        ctx = self.get_context(symbol)
        for bar in bars:
            tick = PriceTick(
                symbol=symbol, timestamp=bar.timestamp, price=bar.close, volume=bar.volume,
                open=bar.open, high=bar.high, low=bar.low,
            )
            ctx.update(tick)
        return {key: ind.is_ready() for key, ind in ctx.indicators.items()}

    def warmup_symbol_from_summary(
        self,
        symbol: str,
        timestamp: datetime,
        prev_close: Optional[float] = None,
        prev_open: Optional[float] = None,
        prev_high: Optional[float] = None,
        prev_low: Optional[float] = None,
        prev_rsi: Optional[float] = None,
        prev_bb_upper: Optional[float] = None,
        prev_bb_middle: Optional[float] = None,
        prev_bb_lower: Optional[float] = None,
        max_fallback_ticks: int = 200,
    ) -> Dict[str, bool]:
        """
        スクリーナーCSV等で用意した前日の要約値（終値・始値・高値・安値・
        RSI・ボリンジャーバンド）を使い、指標ごとにIndicator.seed()で
        直接ウォームアップする。

        既に（例えばwarmup_symbol_from_bars()による実データ投入で）
        is_ready()になっている指標はスキップし、seed()を呼ばない
        （呼んでしまうと、実データによる正確な状態を要約値ベースの
        近似で上書きしてしまうため）。

        各指標のseed()がFalse（対応する要約値がない、またはその指標自体が
        非対応）を返した場合は、prev_closeが分かっていれば、それを繰り返し
        投入するフォールバックでis_ready()を目指す（VWAPはそもそも
        日次リセットが前提のためこの対象外＝ウォームアップ不要）。

        戻り値: {指標キー: シードに成功したか（既にready済みだった場合もTrue）} の辞書
        （呼び出し元でログ出力・不備の把握に使える）
        """
        ctx = self.get_context(symbol)
        results: Dict[str, bool] = {}

        for key, ind in ctx.indicators.items():
            if ind.is_ready():
                results[key] = True
                continue

            seeded = ind.seed(
                prev_close=prev_close, prev_open=prev_open, prev_high=prev_high, prev_low=prev_low,
                prev_rsi=prev_rsi, prev_bb_upper=prev_bb_upper,
                prev_bb_middle=prev_bb_middle, prev_bb_lower=prev_bb_lower,
            )
            if not seeded and prev_close is not None:
                count = 0
                while not ind.is_ready() and count < max_fallback_ticks:
                    ind.update(PriceTick(symbol=symbol, timestamp=timestamp, price=prev_close))
                    count += 1
                seeded = ind.is_ready()
            results[key] = seeded

        return results

    # ------------------------------------------------------------------
    # シグナル判定
    # ------------------------------------------------------------------
    def evaluate_entry(self, symbol: str, rule_name: str = "entry_rule") -> Optional[SignalEvent]:
        return self._evaluate(symbol, self.entry_rule, "ENTRY", rule_name)

    def evaluate_exit(self, symbol: str, rule_name: str = "exit_rule") -> Optional[SignalEvent]:
        return self._evaluate(symbol, self.exit_rule, "EXIT", rule_name)

    def _evaluate(
        self, symbol: str, rule: RuleGroup, signal_type: str, rule_name: str
    ) -> Optional[SignalEvent]:
        ctx = self._contexts.get(symbol)
        if ctx is None or not ctx.all_ready() or ctx.last_tick is None:
            return None

        snapshot = ctx.snapshot()
        if not rule.evaluate(snapshot):
            return None

        return SignalEvent(
            symbol=symbol,
            signal_type=signal_type,  # type: ignore[arg-type]
            rule_name=rule_name,
            occurred_at=ctx.last_tick.timestamp,
            indicator_snapshot=snapshot,
        )

    def process_tick(self, tick: PriceTick, has_position: bool) -> Optional[SignalEvent]:
        """
        1件のTickを取り込み、保有状況に応じてentry/exitいずれかを評価する
        便利メソッド。本番監視・バックテストの双方でこのメソッドを
        呼び出す想定（呼び出し元でPositionManagerから has_position を渡す）。

        - 未保有 (has_position=False) → entry_rule のみ評価
        - 保有中 (has_position=True)  → exit_rule のみ評価
        """
        self.update(tick)
        if has_position:
            return self.evaluate_exit(tick.symbol)
        return self.evaluate_entry(tick.symbol)
