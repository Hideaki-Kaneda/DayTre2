"""
PositionManager：建玉（ポジション）の管理。

基本設計書4節の要件を担保する:
  - 同一銘柄への重複エントリー禁止（1銘柄につき同時に持てるポジションは1つまで）
  - 異なる複数銘柄の同時保有は前提
"""

from datetime import datetime
from typing import Dict, List, Optional

from .models import ClosedPositionResult, OrderReason, Position, PositionDirection


class DuplicateEntryError(Exception):
    """既にポジションを保有している銘柄へ重複エントリーしようとした場合に送出する。"""


class PositionManager:
    def __init__(self):
        self._positions: Dict[str, Position] = {}

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> List[Position]:
        return list(self._positions.values())

    def open_position(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        entry_order_id: str,
        entry_at: datetime,
        direction: PositionDirection = PositionDirection.LONG,
    ) -> Position:
        if self.has_position(symbol):
            raise DuplicateEntryError(
                f"銘柄 {symbol} は既にポジションを保有しているため、重複エントリーできません"
            )
        position = Position(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            entry_order_id=entry_order_id,
            entry_at=entry_at,
            direction=direction,
            high_water_mark=entry_price,
        )
        self._positions[symbol] = position
        return position

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_at: datetime,
        reason: OrderReason,
    ) -> ClosedPositionResult:
        position = self._positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"銘柄 {symbol} の保有ポジションが見つかりません")

        if position.direction == PositionDirection.SHORT:
            # 売り建て（空売り）は値下がりが利益になる
            realized_pnl = (position.entry_price - exit_price) * position.qty
        else:
            realized_pnl = (exit_price - position.entry_price) * position.qty

        return ClosedPositionResult(
            symbol=symbol,
            qty=position.qty,
            entry_price=position.entry_price,
            exit_price=exit_price,
            entry_at=position.entry_at,
            exit_at=exit_at,
            reason=reason,
            realized_pnl=realized_pnl,
            direction=position.direction,
        )

    def position_count(self) -> int:
        return len(self._positions)

    def update_high_water_mark(self, symbol: str, current_price: float) -> None:
        """保有中銘柄の最高値を更新する（トレール決済の基準として使う）。"""
        position = self._positions.get(symbol)
        if position is not None and current_price > position.high_water_mark:
            position.high_water_mark = current_price
