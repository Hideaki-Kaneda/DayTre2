"""
CandlePositionManager：ローソク足連続パターン戦略専用のポジション管理。

trading.PositionManagerとは別物（意図的に分離）。1銘柄につき同時に
持てるのは1ポジション（LONGかSHORTのどちらか）まで、という制約は
trading.PositionManagerと同じ考え方だが、方向（LONG/SHORT）を持つ点が異なる。

同一銘柄について既存戦略（trading.PositionManager）と排他にする制御は、
このクラス単体では行わない。呼び出し元（CandleOrderExecutor）が
外部のhas_position()チェックを組み合わせて行う。
"""

from datetime import datetime
from typing import Dict, List, Optional

from .models import CandleClosedPositionResult, CandleDirection, CandleOrderReason, CandlePosition


class CandleDuplicateEntryError(Exception):
    """既にポジションを保有している銘柄へ重複エントリーしようとした場合に送出する。"""


class CandlePositionManager:
    def __init__(self):
        self._positions: Dict[str, CandlePosition] = {}

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[CandlePosition]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> List[CandlePosition]:
        return list(self._positions.values())

    def open_position(
        self,
        symbol: str,
        direction: CandleDirection,
        qty: int,
        entry_price: float,
        entry_order_id: str,
        entry_at: datetime,
    ) -> CandlePosition:
        if self.has_position(symbol):
            raise CandleDuplicateEntryError(
                f"銘柄 {symbol} は既にポジションを保有しているため、重複エントリーできません"
            )
        position = CandlePosition(
            symbol=symbol, direction=direction, qty=qty,
            entry_price=entry_price, entry_order_id=entry_order_id, entry_at=entry_at,
        )
        self._positions[symbol] = position
        return position

    def close_position(
        self, symbol: str, exit_price: float, exit_at: datetime, reason: CandleOrderReason,
    ) -> CandleClosedPositionResult:
        position = self._positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"銘柄 {symbol} の保有ポジションが見つかりません")

        if position.direction == CandleDirection.LONG:
            realized_pnl = (exit_price - position.entry_price) * position.qty
        else:  # SHORT：値下がりが利益になる
            realized_pnl = (position.entry_price - exit_price) * position.qty

        return CandleClosedPositionResult(
            symbol=symbol, direction=position.direction, qty=position.qty,
            entry_price=position.entry_price, exit_price=exit_price,
            entry_at=position.entry_at, exit_at=exit_at, reason=reason, realized_pnl=realized_pnl,
        )

    def record_reverse_bar(self, symbol: str) -> None:
        """エントリー方向と逆色のバーが確定した際に呼ぶ（通算カウントを+1する）。"""
        position = self._positions.get(symbol)
        if position is not None:
            position.reverse_bar_count += 1

    def position_count(self) -> int:
        return len(self._positions)
