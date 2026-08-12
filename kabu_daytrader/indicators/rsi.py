"""
RSI（Relative Strength Index）指標。ワイルダー(Wilder)方式の平滑化を使用。

params:
    period (int): 期間。既定 14

value() の戻り値キー: f"rsi{period}" （例: {"rsi14": 32.1}）
"""

from typing import Any, Dict, Optional

from .base import Indicator
from .models import PriceTick


class RSIIndicator(Indicator):
    def __init__(self, params: Dict[str, Any] | None = None):
        super().__init__(params)
        self.period: int = int(self.params.get("period", 14))
        if self.period < 1:
            raise ValueError("period は1以上を指定してください")

        self._key = f"rsi{self.period}"
        self._prev_price: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._change_count = 0  # これまでに取り込んだ「価格変化」の件数
        self._seed_gain_sum = 0.0
        self._seed_loss_sum = 0.0

    def update(self, tick: PriceTick) -> None:
        price = tick.price
        if self._prev_price is None:
            self._prev_price = price
            return

        change = price - self._prev_price
        self._prev_price = price
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None:
            # 最初のperiod件は単純平均でシードする（Wilder方式の標準的な初期化）
            self._seed_gain_sum += gain
            self._seed_loss_sum += loss
            self._change_count += 1
            if self._change_count == self.period:
                self._avg_gain = self._seed_gain_sum / self.period
                self._avg_loss = self._seed_loss_sum / self.period
        else:
            # 以降はワイルダーの平滑化式で更新
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

    def value(self) -> Dict[str, Any]:
        if self._avg_gain is None or self._avg_loss is None:
            return {self._key: None}

        if self._avg_gain == 0 and self._avg_loss == 0:
            # 値動きが全くない（例：前日終値を複数回投入したウォームアップ直後等）
            # 場合は「買われすぎ/売られすぎ」のどちらでもない中立値として50を返す。
            # avg_loss==0だけを見て100と判定すると、実際には値動きゼロなのに
            # 「強い上昇トレンド」と誤判定してしまうため、先にこのケースを分離している。
            rsi = 50.0
        elif self._avg_loss == 0:
            rsi = 100.0
        elif self._avg_gain == 0:
            rsi = 0.0
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        return {self._key: rsi}

    def is_ready(self) -> bool:
        return self._avg_gain is not None

    def seed(
        self, prev_close: float | None = None, prev_rsi: float | None = None, **_kwargs
    ) -> bool:
        """
        前日終値時点のRSI値から、内部の平均上昇幅/平均下落幅(avg_gain/avg_loss)を
        逆算してシードする。

        RSI = 100 - 100/(1+RS)、RS = avg_gain/avg_loss という定義から、
        RS = prev_rsi / (100 - prev_rsi) が導ける。
        比率(RS)さえ合っていればRSIの値自体は再現できるが、その後の実際の
        値動きがどれだけRSIを動かすかは avg_gain/avg_loss の「絶対的な大きさ」にも
        依存するため、前日終値の0.1%程度を典型的な値動き幅の目安として
        スケールを決めている（銘柄の値がさ・値動きの荒さに応じた厳密な値ではなく、
        あくまで妥当な近似値）。
        """
        if prev_rsi is None:
            return False
        prev_rsi = max(0.001, min(prev_rsi, 99.999))  # 0/100ちょうどでの0除算を避ける

        rs = prev_rsi / (100.0 - prev_rsi)
        baseline = (prev_close * 0.001) if prev_close else 1.0
        baseline = max(baseline, 1e-6)

        self._avg_gain = baseline * rs / (1.0 + rs)
        self._avg_loss = baseline / (1.0 + rs)
        self._prev_price = prev_close
        self._change_count = self.period  # 通常フローのシード期間は完了済み扱いにする
        return True
