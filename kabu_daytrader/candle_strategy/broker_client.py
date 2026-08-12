"""
MarginBrokerClient：candle_strategyが発注に使う外部インターフェース。

現物のtrading.BrokerClientとは異なり、信用取引（空売り）に対応するため
「新規買い／新規売り（空売り）／返済買い／返済売り」の4種類の操作を持つ。

実運用ではapi.RestClientに信用取引用のメソッドを追加して満たす想定
（本ターンでは未実装。実際にkabuステーションAPIで信用注文を送るには
CashMargin=2（新規）/3（返済）、建玉を返済する際のHoldID指定など、
現物注文とは異なるパラメータ・仕様の確認が別途必要）。
テスト・バックテストでは SimulatedMarginBrokerClient を使う。
"""

from datetime import datetime
from typing import Dict, Protocol

from .models import CandleDirection, CandleOrderReason, CandleOrderResult, CandleOrderStatus


class MarginBrokerClient(Protocol):
    def get_buying_power(self) -> float: ...

    def place_market_buy_to_open(self, symbol: str, qty: int) -> CandleOrderResult:
        """新規買い建て（LONGエントリー）。現物買いまたは信用新規買い。"""
        ...

    def place_market_sell_to_close(self, symbol: str, qty: int) -> CandleOrderResult:
        """買い建て玉の決済（LONG決済）。現物売りまたは信用返済売り。"""
        ...

    def place_market_sell_to_open(self, symbol: str, qty: int) -> CandleOrderResult:
        """新規売り建て（SHORTエントリー＝空売り）。信用新規売り。"""
        ...

    def place_market_buy_to_close(self, symbol: str, qty: int) -> CandleOrderResult:
        """売り建て玉の決済（SHORT決済＝信用返済買い）。"""
        ...


class SimulatedMarginBrokerClient:
    """
    単体テスト・バックテスト用の簡易シミュレータ。
    trading.SimulatedBrokerClientと同様の考え方で、与えられた価格で
    即座に約定したものとして扱う。空売り（SHORT）もサポートする。
    """

    def __init__(self, initial_buying_power: float):
        self.buying_power = initial_buying_power
        self._prices: Dict[str, float] = {}
        self._next_order_id = 1
        self.current_time: datetime | None = None

    def set_current_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def set_current_time(self, dt: datetime) -> None:
        self.current_time = dt

    def _now(self) -> datetime:
        return self.current_time or datetime.now()

    def _issue_order_id(self) -> str:
        order_id = f"SIM-CANDLE-{self._next_order_id:06d}"
        self._next_order_id += 1
        return order_id

    def get_buying_power(self) -> float:
        return self.buying_power

    def _fill(self, symbol: str, qty: int, direction: CandleDirection, is_open: bool) -> CandleOrderResult:
        now = self._now()
        price = self._prices.get(symbol)
        if price is None:
            return CandleOrderResult(
                order_id=self._issue_order_id(), symbol=symbol, direction=direction, qty=qty,
                status=CandleOrderStatus.FAILED, reason=CandleOrderReason.ENTRY_CONSECUTIVE_CANDLES,
                requested_at=now, error_message=f"価格情報がありません: {symbol}",
            )

        cost = price * qty
        if is_open and cost > self.buying_power:
            return CandleOrderResult(
                order_id=self._issue_order_id(), symbol=symbol, direction=direction, qty=qty,
                status=CandleOrderStatus.FAILED, reason=CandleOrderReason.ENTRY_CONSECUTIVE_CANDLES,
                requested_at=now, error_message="余力不足",
            )

        # 簡易シミュレーションのため、余力は新規建て時に確保・決済時に解放する
        # （信用取引の証拠金計算の精密なシミュレーションは行わない）
        if is_open:
            self.buying_power -= cost
        else:
            self.buying_power += cost

        return CandleOrderResult(
            order_id=self._issue_order_id(), symbol=symbol, direction=direction, qty=qty,
            status=CandleOrderStatus.FILLED, reason=CandleOrderReason.ENTRY_CONSECUTIVE_CANDLES,
            requested_at=now, filled_at=now, filled_price=price,
        )

    def place_market_buy_to_open(self, symbol: str, qty: int) -> CandleOrderResult:
        return self._fill(symbol, qty, CandleDirection.LONG, is_open=True)

    def place_market_sell_to_close(self, symbol: str, qty: int) -> CandleOrderResult:
        return self._fill(symbol, qty, CandleDirection.LONG, is_open=False)

    def place_market_sell_to_open(self, symbol: str, qty: int) -> CandleOrderResult:
        return self._fill(symbol, qty, CandleDirection.SHORT, is_open=True)

    def place_market_buy_to_close(self, symbol: str, qty: int) -> CandleOrderResult:
        return self._fill(symbol, qty, CandleDirection.SHORT, is_open=False)
