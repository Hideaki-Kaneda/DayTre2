"""
寄り付き直後の複数本（既定4本）のTR（True Range）平均から「当日のAR」を算出する指標。

用途：09:00〜09:12（3分足なら4本）のボラティリティから当日の値幅目安を求め、
トレール決済の基準幅（AR × 倍率）として使う。

TR（True Range）は当該本の高値・安値に加え、前本の終値からの窓開けも考慮する:
    TR = max(高値-安値, |高値-前本終値|, |安値-前本終値|)

bar_count本のTRが揃った時点でAR（平均値）を確定し、以降その日は値を凍結する
（is_ready()以降はupdate()を呼んでも値は変化しない）。日付が変わったら自動リセットする。

seed(prev_close=...)を使うと、当日1本目のTR計算に前日終値を使い、
前日からの窓開け（ギャップ）も含めた計算にできる。
"""

from datetime import date as date_type
from typing import Any, Dict, List, Optional

from .base import Indicator
from .models import PriceTick


class OpeningRangeARIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.bar_count: int = int(self.params.get("bar_count", 4))
        if self.bar_count < 1:
            raise ValueError("bar_count は1以上を指定してください")

        self._tr_values: List[float] = []
        self._prev_close: Optional[float] = None
        self._ar: Optional[float] = None
        self._session_date: Optional[date_type] = None

    def _maybe_reset_session(self, tick: PriceTick) -> None:
        today = tick.timestamp.date()
        if self._session_date is None:
            self._session_date = today
            return
        if today != self._session_date:
            self._session_date = today
            self._tr_values = []
            self._prev_close = None
            self._ar = None

    def update(self, tick: PriceTick) -> None:
        self._maybe_reset_session(tick)
        if self._ar is not None:
            return  # 当日分は確定済み。以降のTickでは更新しない

        high = tick.high if tick.high is not None else tick.price
        low = tick.low if tick.low is not None else tick.price
        close = tick.price

        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))

        self._tr_values.append(tr)
        self._prev_close = close

        if len(self._tr_values) >= self.bar_count:
            self._ar = sum(self._tr_values[: self.bar_count]) / self.bar_count

    def value(self) -> Dict[str, Any]:
        return {"ar": self._ar}

    def is_ready(self) -> bool:
        return self._ar is not None

    def seed(self, prev_close: float | None = None, **_kwargs) -> bool:
        """
        前日終値を、当日1本目のTR計算における「前本終値」として使う
        （前日からの窓開けをTRに含めるため）。

        ARの値自体は当日の実データが揃わないと意味がないため、is_ready()には
        ならない。ただし、SignalEngine側の「seed()がFalseなら前日終値を
        繰り返し投入するフォールバック」が発火すると、実在しない当日バーで
        ARが算出されてしまい無意味な値になるため、常にTrueを返して
        フォールバックを抑止する（前日終値を反映できたかに関わらず）。
        """
        if prev_close is not None:
            self._prev_close = prev_close
        return True
