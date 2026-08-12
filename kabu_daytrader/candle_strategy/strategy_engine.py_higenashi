"""
CandleStrategyEngine：確定した3分足バーを1本ずつ受け取り、
「N本連続陽線／陰線」のエントリー判定と、保有中ポジションの
「通算逆足M本」判定を行う。

指標プラグイン（indicators/）とは異なるため、SignalEngineの仕組みには
乗せず、この戦略専用のシンプルな状態機械として実装している。
本番・バックテストの両方から同一クラスを使うことで、ロジックの乖離を防ぐ
（基本設計の「本番/バックテスト共通化」の方針を踏襲）。
"""

from datetime import date as date_type
from typing import Dict, Optional

from backtest.models import Bar

from .models import CandleDirection


def bar_direction(bar: Bar) -> CandleDirection:
    if bar.close > bar.open:
        return CandleDirection.LONG
    if bar.close < bar.open:
        return CandleDirection.SHORT
    return CandleDirection.NEUTRAL


class CandleStrategyEngine:
    def __init__(self, consecutive_bars_n: int = 3):
        self.consecutive_bars_n = consecutive_bars_n
        """エントリーに必要な連続同色バー本数（既定3）。"""

        self._streak_direction: Dict[str, CandleDirection] = {}
        self._streak_count: Dict[str, int] = {}
        self._session_date: Dict[str, date_type] = {}

    def _maybe_reset_session(self, symbol: str, bar: Bar) -> None:
        today = bar.timestamp.date()
        if self._session_date.get(symbol) is None:
            self._session_date[symbol] = today
            return
        if self._session_date[symbol] != today:
            self._session_date[symbol] = today
            self._streak_direction.pop(symbol, None)
            self._streak_count.pop(symbol, None)

    def update(self, bar: Bar) -> None:
        """確定バーを1本取り込み、連続同色カウントを更新する。"""
        self._maybe_reset_session(bar.symbol, bar)
        direction = bar_direction(bar)

        if direction == CandleDirection.NEUTRAL:
            # 同値（始値=終値）はどちらの連続性も途切れさせる
            self._streak_direction[bar.symbol] = CandleDirection.NEUTRAL
            self._streak_count[bar.symbol] = 0
            return

        if self._streak_direction.get(bar.symbol) == direction:
            self._streak_count[bar.symbol] = self._streak_count.get(bar.symbol, 0) + 1
        else:
            self._streak_direction[bar.symbol] = direction
            self._streak_count[bar.symbol] = 1

    def evaluate_entry(self, symbol: str) -> Optional[CandleDirection]:
        """
        連続同色バーがちょうどconsecutive_bars_n本に達した瞬間のみその方向を返す。
        N本を超えて連続しても（＝count > N）再度は発火しない
        （count >= N のままだと、決済直後の同一バーで即座に再エントリーしてしまうため。
        ちょうどN本目という単発のシグナルとして扱う）。
        """
        count = self._streak_count.get(symbol, 0)
        direction = self._streak_direction.get(symbol)
        if direction in (CandleDirection.LONG, CandleDirection.SHORT) and count == self.consecutive_bars_n:
            return direction
        return None

    def get_streak(self, symbol: str) -> tuple[Optional[CandleDirection], int]:
        """現在の連続カウント状況を返す（GUI表示・デバッグ用）。"""
        return self._streak_direction.get(symbol), self._streak_count.get(symbol, 0)
