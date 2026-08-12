"""
ボリンジャーバンド指標。

params:
    period (int): 期間。既定 20
    num_std (float): バンド幅（標準偏差の倍率）。既定 2.0

value() の戻り値:
    {
        "middle": 移動平均（中心線）,
        "upper": 上限バンド,
        "lower": 下限バンド,
        "percent_b": (price - lower) / (upper - lower)  ※0=下限, 1=上限,
        "position": "ABOVE_UPPER" | "BELOW_LOWER" | "WITHIN",
        "bbw": (upper - lower) / middle  ※バンド幅を中心線で正規化した値。
               ボラティリティの収縮・拡大を銘柄・価格帯を問わず比較する指標として使う
    }

SignalEngineのルール設定例（基本設計書3節）
    {"indicator": "bollinger", "field": "position", "op": "==", "value": "ABOVE_UPPER"}
のように "position" フィールドで判定できる。
"""

import statistics
from collections import deque
from typing import Any, Dict, Optional

from .base import Indicator
from .models import PriceTick


class BollingerBandIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.period: int = int(self.params.get("period", 20))
        self.num_std: float = float(self.params.get("num_std", 2.0))
        if self.period < 2:
            raise ValueError("period は2以上を指定してください（標準偏差の計算に必要）")

        self._window: deque[float] = deque(maxlen=self.period)
        self._last_price: Optional[float] = None

    def update(self, tick: PriceTick) -> None:
        self._window.append(tick.price)
        self._last_price = tick.price

    def value(self) -> Dict[str, Any]:
        if len(self._window) < self.period or self._last_price is None:
            return {
                "middle": None,
                "upper": None,
                "lower": None,
                "percent_b": None,
                "position": None,
                "bbw": None,
            }

        middle = sum(self._window) / self.period
        # 標本標準偏差ではなく母標準偏差を使用（一般的なボリンジャーバンドの定義に合わせる）
        std = statistics.pstdev(self._window)
        upper = middle + self.num_std * std
        lower = middle - self.num_std * std

        price = self._last_price
        if upper == lower:
            percent_b = 0.5
        else:
            percent_b = (price - lower) / (upper - lower)

        if price > upper:
            position = "ABOVE_UPPER"
        elif price < lower:
            position = "BELOW_LOWER"
        else:
            position = "WITHIN"

        bbw = (upper - lower) / middle if middle else None

        return {
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "percent_b": percent_b,
            "position": position,
            "bbw": bbw,
        }

    def is_ready(self) -> bool:
        return len(self._window) == self.period

    def seed(
        self,
        prev_close: float | None = None,
        prev_bb_upper: float | None = None,
        prev_bb_middle: float | None = None,
        prev_bb_lower: float | None = None,
        **_kwargs,
    ) -> bool:
        """
        前日のミドルバンド・上限・下限から、平均(=middle)と母標準偏差が
        正確に一致するウィンドウを逆算して構築する。

        母標準偏差std_targetは (upper - lower) / (2 * num_std) から求める
        （upper-middle と middle-lower の両方を使って平均を取り、
        非対称な入力値にもある程度頑健にしている）。

        平均・標準偏差の2つの統計量だけからは元の系列を一意に復元できないため、
        ウィンドウの半数を middle+d、残り半数を middle-d に配置する
        （必要なら1点だけmiddleちょうどの値を入れる）ことで、
        目標の平均・母標準偏差を厳密に満たす系列を構成している。
        """
        if prev_bb_upper is None or prev_bb_middle is None or prev_bb_lower is None:
            return False

        std_target = ((prev_bb_upper - prev_bb_middle) + (prev_bb_middle - prev_bb_lower)) / (2 * self.num_std)
        std_target = max(std_target, 0.0)

        n = self.period
        has_center_point = n % 2 == 1
        pair_count = (n - 1) // 2 if has_center_point else n // 2

        if std_target == 0 or pair_count == 0:
            values = [prev_bb_middle] * n
        else:
            # (n - k)組の±dペアで目標の母標準偏差を満たすよう d を調整する
            # （k=1: 中央にmiddleちょうどの値を1つ置く奇数期間の場合）
            k = 1 if has_center_point else 0
            d = std_target * (n / (n - k)) ** 0.5
            values = []
            if has_center_point:
                values.append(prev_bb_middle)
            for _ in range(pair_count):
                values.append(prev_bb_middle + d)
                values.append(prev_bb_middle - d)

        self._window.clear()
        self._window.extend(values[:n])
        self._last_price = prev_close if prev_close is not None else prev_bb_middle
        return True
