"""
移動平均線（SMA / EMA）指標。

params:
    period (int): 期間。既定 25
    type (str): "sma"（単純移動平均、既定） または "ema"（指数移動平均）

value() の戻り値キーは f"{type}{period}" 形式
（例: period=25, type="sma" なら {"sma25": 1234.5}）。
"""

from collections import deque
from typing import Any, Dict, Optional

from .base import Indicator
from .models import PriceTick


class MovingAverageIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.period: int = int(self.params.get("period", 25))
        self.ma_type: str = str(self.params.get("type", "sma")).lower()
        if self.period < 1:
            raise ValueError("period は1以上を指定してください")
        if self.ma_type not in ("sma", "ema"):
            raise ValueError(f"未対応のtype: {self.ma_type}（sma または ema を指定）")

        self._window: deque[float] = deque(maxlen=self.period)
        self._ema_value: Optional[float] = None
        self._ema_multiplier = 2.0 / (self.period + 1)
        self._key = f"{self.ma_type}{self.period}"

    def update(self, tick: PriceTick) -> None:
        price = tick.price
        self._window.append(price)

        if self.ma_type == "ema":
            if self._ema_value is None:
                # 最初のperiod件が揃うまではEMAを確定させず、
                # period件揃った時点でSMAをシード値として使う（一般的な手法）
                if len(self._window) == self.period:
                    self._ema_value = sum(self._window) / self.period
            else:
                self._ema_value = (
                    price - self._ema_value
                ) * self._ema_multiplier + self._ema_value

    def value(self) -> Dict[str, Any]:
        if self.ma_type == "sma":
            if len(self._window) < self.period:
                return {self._key: None}
            return {self._key: sum(self._window) / self.period}
        else:
            return {self._key: self._ema_value}

    def is_ready(self) -> bool:
        if self.ma_type == "sma":
            return len(self._window) == self.period
        return self._ema_value is not None

    def seed(self, prev_close: float | None = None, **_kwargs) -> bool:
        """
        前日終値でウィンドウ（またはEMA初期値）を埋める。
        本来は期間分の過去の実データが理想だが、利用できるのが
        「前日終値」という1点の値のみのため、それで一律に埋める
        （＝前日は値動きがなかったものとして扱う近似）。
        """
        if prev_close is None:
            return False
        if self.ma_type == "sma":
            self._window.clear()
            self._window.extend([prev_close] * self.period)
        else:  # ema
            self._ema_value = prev_close
        return True
