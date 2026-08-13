"""
DMIIndicator：+DI（PDI）・-DI（MDI）・ADXを算出する（Wilderの平滑化方式）。

パラメータ（ユーザー確定）:
    di_period : +DI/-DIの算出に使う期間（既定6）
    adx_period: ADX（DXの平滑化）に使う期間（既定14）

計算方法（Wilderの標準的なDMI/ADX算出方法）:
    +DM = high - prev_high （ただし (high-prev_high) > (prev_low-low) かつ >0 の場合のみ、それ以外は0）
    -DM = prev_low - low   （ただし (prev_low-low) > (high-prev_high) かつ >0 の場合のみ、それ以外は0）
    TR  = max(high-low, |high-prev_close|, |low-prev_close|)

    直近di_period本の+DM・-DM・TRをWilder平滑化（RSIのavg_gain/avg_lossと同じ考え方）し、
        +DI = 100 * smoothed(+DM) / smoothed(TR)
        -DI = 100 * smoothed(-DM) / smoothed(TR)
        DX  = 100 * |+DI - -DI| / (+DI + -DI)
    DXをさらにadx_period本Wilder平滑化した値がADX。

value()のフィールド名はユーザーの条件式表記に合わせて pdi（+DI）・mdi（-DI）・adx とする。
"""

from typing import Any, Dict, Optional

from .base import Indicator
from .models import PriceTick


class DMIIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.di_period = int(self.params.get("di_period", 14))
        self.adx_period = int(self.params.get("adx_period", 14))

        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._prev_close: Optional[float] = None

        # +DM/-DM/TRの平滑化状態（Wilder方式）
        self._smoothed_plus_dm: Optional[float] = None
        self._smoothed_minus_dm: Optional[float] = None
        self._smoothed_tr: Optional[float] = None
        self._dm_tr_count = 0  # di_period分溜まったかの判定に使う

        # DXの平滑化状態（ADX）
        self._smoothed_dx: Optional[float] = None
        self._dx_count = 0  # adx_period分のDXが溜まったかの判定に使う

        self._pdi: Optional[float] = None
        self._mdi: Optional[float] = None
        self._adx: Optional[float] = None

    def update(self, tick: PriceTick) -> None:
        high = tick.high if tick.high is not None else tick.price
        low = tick.low if tick.low is not None else tick.price
        close = tick.price

        if self._prev_high is None:
            # 1本目は+DM/-DM/TRを計算できない（前本の情報が無いため）
            self._prev_high, self._prev_low, self._prev_close = high, low, close
            return

        up_move = high - self._prev_high
        down_move = self._prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))

        self._prev_high, self._prev_low, self._prev_close = high, low, close

        if self._smoothed_tr is None:
            # 最初の1本目はそのままの値をシードにする
            self._smoothed_plus_dm = plus_dm
            self._smoothed_minus_dm = minus_dm
            self._smoothed_tr = tr
        else:
            n = self.di_period
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / n) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / n) + minus_dm
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / n) + tr
        self._dm_tr_count += 1

        if self._dm_tr_count < self.di_period or self._smoothed_tr == 0:
            self._pdi = None
            self._mdi = None
            return

        self._pdi = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
        self._mdi = 100.0 * self._smoothed_minus_dm / self._smoothed_tr

        di_sum = self._pdi + self._mdi
        dx = 0.0 if di_sum == 0 else 100.0 * abs(self._pdi - self._mdi) / di_sum

        if self._smoothed_dx is None:
            self._smoothed_dx = dx
        else:
            n = self.adx_period
            self._smoothed_dx = (self._smoothed_dx * (n - 1) + dx) / n
        self._dx_count += 1

        if self._dx_count >= self.adx_period:
            self._adx = self._smoothed_dx

    def is_ready(self) -> bool:
        return self._pdi is not None and self._mdi is not None and self._adx is not None

    def value(self) -> Dict[str, Any]:
        if not self.is_ready():
            return {"pdi": None, "mdi": None, "adx": None}
        return {"pdi": self._pdi, "mdi": self._mdi, "adx": self._adx}
