"""
VWAP（出来高加重平均価格）指標。

kabuステーションAPIのPUSH配信では出来高が「その時点までの累計出来高」
で送られてくるため、呼び出し側（PushClientWorker）で前回累計との差分を
計算し、PriceTick.volume には「直近の約定出来高（差分）」を渡すこと。

セッション（取引日）が変わったら自動的に累計をリセットする。
tick.volume が None または 0 の場合は価格情報のみのTickとみなし、
VWAPの累計計算には反映しない（現在値の把握には使わないため）。

params:
    session_reset (bool): 日付が変わったら自動リセットするか。既定 True

value() の戻り値:
    {
        "vwap": VWAP値,
        "deviation_pct": (price - vwap) / vwap * 100  # 現在値のVWAPからの乖離率(%)
    }
"""

from typing import Any, Dict, Optional
from datetime import date

from .base import Indicator
from .models import PriceTick


class VWAPIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.session_reset: bool = bool(self.params.get("session_reset", True))

        self._cum_price_volume = 0.0
        self._cum_volume = 0.0
        self._last_price: Optional[float] = None
        self._session_date: Optional[date] = None

    def _maybe_reset_session(self, tick: PriceTick) -> None:
        if not self.session_reset:
            return
        tick_date = tick.timestamp.date()
        if self._session_date is None:
            self._session_date = tick_date
        elif tick_date != self._session_date:
            # 日付が変わった＝新しい取引セッション → 累計をリセット
            self._cum_price_volume = 0.0
            self._cum_volume = 0.0
            self._session_date = tick_date

    def update(self, tick: PriceTick) -> None:
        self._maybe_reset_session(tick)
        self._last_price = tick.price

        if tick.volume:
            self._cum_price_volume += tick.price * tick.volume
            self._cum_volume += tick.volume

    def value(self) -> Dict[str, Any]:
        if self._cum_volume == 0 or self._last_price is None:
            return {"vwap": None, "deviation_pct": None}

        vwap = self._cum_price_volume / self._cum_volume
        deviation_pct = (self._last_price - vwap) / vwap * 100.0 if vwap else None
        return {"vwap": vwap, "deviation_pct": deviation_pct}

    def is_ready(self) -> bool:
        return self._cum_volume > 0

    def reset(self) -> None:
        # VWAPはセッション単位でのリセットのみを想定するため、
        # 基底クラスの再__init__ではなくここで明示的に初期化する
        self._cum_price_volume = 0.0
        self._cum_volume = 0.0
        self._last_price = None
        self._session_date = None
