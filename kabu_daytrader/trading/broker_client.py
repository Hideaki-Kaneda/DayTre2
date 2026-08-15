"""
BrokerClient: OrderExecutorが発注に使う外部インターフェース。

実運用では api/rest_client.py の RestClient がこのプロトコルを満たす
実装（kabuステーションAPIへの実際のHTTP呼び出し）を提供する。
ここでは trading/ パッケージを rest_client.py に依存させず単体テスト
できるよう、Protocolとテスト用のシンプルなシミュレータのみを置く。
"""

from datetime import datetime
from typing import Protocol

from .models import OrderResult


class BrokerClient(Protocol):
    """kabuステーションREST APIラッパーが実装すべき最小インターフェース。"""

    def get_buying_power(self) -> float:
        """現物買付可能額を返す。"""
        ...

    def place_market_buy(self, symbol: str, qty: int) -> OrderResult:
        """成行買い注文を発注する（現物）。"""
        ...

    def place_market_sell(self, symbol: str, qty: int) -> OrderResult:
        """成行売り注文を発注する（現物）。"""
        ...

    def place_margin_buy_to_open(self, symbol: str, qty: int) -> OrderResult:
        """信用新規買い（買い建て）。"""
        ...

    def place_margin_sell_to_open(self, symbol: str, qty: int) -> OrderResult:
        """信用新規売り（売り建て＝空売り）。"""
        ...

    def place_margin_sell_to_close(self, symbol: str, qty: int) -> OrderResult:
        """買い建て玉の返済（反対売買の売り）。"""
        ...

    def place_margin_buy_to_close(self, symbol: str, qty: int) -> OrderResult:
        """売り建て玉の返済（反対売買の買い）。"""
        ...


class SimulatedBrokerClient:
    """
    単体テスト・バックテスト用の簡易シミュレータ。
    実際のkabuステーションAPIには接続せず、与えられた価格で
    即座に約定したものとして扱う。

    使い方:
        broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
        broker.set_current_price("9432", 150.0)
        result = broker.place_market_buy("9432", 100)
    """

    def __init__(self, initial_buying_power: float):
        self.buying_power = initial_buying_power
        self._prices: dict[str, float] = {}
        self._next_order_id = 1
        self.current_time: datetime | None = None
        """バックテストでは実時間ではなくシミュレーション上の時刻を使う必要があるため、
        BacktestEngineが各Tick処理時に set_current_time() で更新する。
        本番相当のリアルタイム利用（テスト等）では None のままで良く、
        その場合は datetime.now() にフォールバックする。"""

    def set_current_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def set_current_time(self, dt: datetime) -> None:
        self.current_time = dt

    def _now(self) -> datetime:
        return self.current_time or datetime.now()

    def _issue_order_id(self) -> str:
        order_id = f"SIM-{self._next_order_id:06d}"
        self._next_order_id += 1
        return order_id

    def get_buying_power(self) -> float:
        return self.buying_power

    def place_market_buy(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderReason, OrderSide, OrderStatus  # 遅延importで循環回避

        now = self._now()
        price = self._prices.get(symbol)
        if price is None:
            return OrderResult(
                order_id=self._issue_order_id(), symbol=symbol, side=OrderSide.BUY,
                qty=qty, status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=f"価格情報がありません: {symbol}",
            )

        cost = price * qty
        if cost > self.buying_power:
            return OrderResult(
                order_id=self._issue_order_id(), symbol=symbol, side=OrderSide.BUY,
                qty=qty, status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message="余力不足",
            )

        self.buying_power -= cost
        return OrderResult(
            order_id=self._issue_order_id(), symbol=symbol, side=OrderSide.BUY,
            qty=qty, status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
            requested_at=now, filled_at=now, filled_price=price,
        )

    def place_market_sell(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderReason, OrderSide, OrderStatus

        now = self._now()
        price = self._prices.get(symbol)
        if price is None:
            return OrderResult(
                order_id=self._issue_order_id(), symbol=symbol, side=OrderSide.SELL,
                qty=qty, status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=f"価格情報がありません: {symbol}",
            )

        self.buying_power += price * qty
        return OrderResult(
            order_id=self._issue_order_id(), symbol=symbol, side=OrderSide.SELL,
            qty=qty, status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
            requested_at=now, filled_at=now, filled_price=price,
        )

    # ------------------------------------------------------------------
    # 信用取引（新規売り＝空売り含む）のシミュレーション
    # ------------------------------------------------------------------
    def _fill_margin(self, symbol: str, qty: int, side, is_open: bool) -> OrderResult:
        from .models import OrderReason, OrderStatus

        now = self._now()
        price = self._prices.get(symbol)
        if price is None:
            return OrderResult(
                order_id=self._issue_order_id(), symbol=symbol, side=side,
                qty=qty, status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=f"価格情報がありません: {symbol}",
            )

        cost = price * qty
        if is_open and cost > self.buying_power:
            return OrderResult(
                order_id=self._issue_order_id(), symbol=symbol, side=side,
                qty=qty, status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message="余力不足",
            )

        # 簡易シミュレーションのため、新規建て時に余力を確保し決済時に解放する
        # （信用取引の証拠金計算の精密なシミュレーションは行わない）
        if is_open:
            self.buying_power -= cost
        else:
            self.buying_power += cost

        return OrderResult(
            order_id=self._issue_order_id(), symbol=symbol, side=side,
            qty=qty, status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
            requested_at=now, filled_at=now, filled_price=price,
        )

    def place_margin_buy_to_open(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderSide
        return self._fill_margin(symbol, qty, OrderSide.BUY, is_open=True)

    def place_margin_sell_to_open(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderSide
        return self._fill_margin(symbol, qty, OrderSide.SELL, is_open=True)

    def place_margin_sell_to_close(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderSide
        return self._fill_margin(symbol, qty, OrderSide.SELL, is_open=False)

    def place_margin_buy_to_close(self, symbol: str, qty: int) -> OrderResult:
        from .models import OrderSide
        return self._fill_margin(symbol, qty, OrderSide.BUY, is_open=False)
