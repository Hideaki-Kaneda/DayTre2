"""
MACDIndicator：DIF・DEA・MACD（ヒストグラム）を算出する。

用語（ユーザー確定・中華圏でよく使われる表記に準拠）:
    DIF = EMA(終値, fast_period) - EMA(終値, slow_period)
    DEA = EMA(DIF, signal_period)
    MACD（ヒストグラム） = (DIF - DEA) * 2

クロス判定用に、直前の値との比較結果も value() に含める:
    macd_crossed_up   : MACD（ヒストグラム）がマイナスからプラスに反転した瞬間
    macd_crossed_down : MACD（ヒストグラム）がプラスからマイナスに反転した瞬間
    dif_crossed_dea_up   : DIFがDEAを下から上へ抜けた瞬間（ゴールデンクロス相当）
    dif_crossed_dea_down : DIFがDEAを上から下へ抜けた瞬間（デッドクロス相当）

これらはルール側で {"op": "==", "value": true} のように参照する
（例: {"indicator": "macd", "field": "dif_crossed_dea_up", "op": "==", "value": true}）。
"""

from typing import Any, Dict, Optional

from .base import Indicator
from .models import PriceTick


class MACDIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.fast_period = int(self.params.get("fast_period", 12))
        self.slow_period = int(self.params.get("slow_period", 26))
        self.signal_period = int(self.params.get("signal_period", 9))

        self._ema_fast: Optional[float] = None
        self._ema_slow: Optional[float] = None
        self._dea: Optional[float] = None
        self._dif: Optional[float] = None
        self._histogram: Optional[float] = None
        self._prev_dif: Optional[float] = None
        self._prev_dea: Optional[float] = None
        self._prev_histogram: Optional[float] = None
        self._count = 0

    def update(self, tick: PriceTick) -> None:
        price = tick.price
        self._count += 1

        k_fast = 2.0 / (self.fast_period + 1)
        k_slow = 2.0 / (self.slow_period + 1)
        k_signal = 2.0 / (self.signal_period + 1)

        self._ema_fast = price if self._ema_fast is None else price * k_fast + self._ema_fast * (1 - k_fast)
        self._ema_slow = price if self._ema_slow is None else price * k_slow + self._ema_slow * (1 - k_slow)
        dif = self._ema_fast - self._ema_slow
        new_dea = dif if self._dea is None else dif * k_signal + self._dea * (1 - k_signal)
        histogram = (dif - new_dea) * 2

        # クロス判定は「今回の更新で計算した新しい値」対「更新前（＝前回時点）の値」で行うため、
        # 上書きする前に退避しておく
        self._prev_dif = self._dif
        self._prev_dea = self._dea
        self._prev_histogram = self._histogram

        self._dif = dif
        self._dea = new_dea
        self._histogram = histogram

    def is_ready(self) -> bool:
        # 実務上の簡易基準：slow_period分のバーが溜まっていればEMAは十分収束しているとみなす
        return self._count >= self.slow_period

    def value(self) -> Dict[str, Any]:
        if not self.is_ready():
            return {
                "dif": None, "dea": None, "macd": None,
                "macd_crossed_up": False, "macd_crossed_down": False,
                "dif_crossed_dea_up": False, "dif_crossed_dea_down": False,
            }

        crossed_up = self._prev_histogram is not None and self._prev_histogram < 0 and self._histogram >= 0
        crossed_down = self._prev_histogram is not None and self._prev_histogram > 0 and self._histogram <= 0
        dif_crossed_dea_up = (
            self._prev_dif is not None and self._prev_dea is not None
            and self._prev_dif <= self._prev_dea and self._dif > self._dea
        )
        dif_crossed_dea_down = (
            self._prev_dif is not None and self._prev_dea is not None
            and self._prev_dif >= self._prev_dea and self._dif < self._dea
        )

        return {
            "dif": self._dif, "dea": self._dea, "macd": self._histogram,
            "macd_crossed_up": crossed_up, "macd_crossed_down": crossed_down,
            "dif_crossed_dea_up": dif_crossed_dea_up, "dif_crossed_dea_down": dif_crossed_dea_down,
        }
