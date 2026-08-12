"""
CandleOrderExecutor：ローソク足連続パターン戦略のエントリー・決済実行。

決済判定の優先順位（ユーザー確定仕様）:
    1. 損切（既定 -1.5%）
    2. 利確（既定 +3.5%）
    3. 通算逆足M本（既定2本）
1つのTick/バーで複数の条件を同時に満たしていても、最初に該当した
条件のみで決済する（優先順位の高いものを先にチェックする実装で担保）。

既存のtrading.OrderExecutor/RiskManagerとは独立しているが、
「同一銘柄では既存戦略と排他的に運用する」という要件を満たすため、
外部（trading側）のポジション有無を確認する関数を external_has_position
として受け取り、エントリー前にチェックする。
"""

import logging
from datetime import datetime
from typing import Callable, List, Optional

from .broker_client import MarginBrokerClient
from .models import (
    CandleClosedPositionResult,
    CandleDirection,
    CandleOrderReason,
    CandleOrderResult,
    CandleOrderStatus,
)
from .position_manager import CandlePositionManager

logger = logging.getLogger(__name__)


class CandleOrderExecutor:
    def __init__(
        self,
        broker: MarginBrokerClient,
        position_manager: CandlePositionManager,
        shares_per_symbol: int = 100,
        take_profit_pct: float = 0.035,
        stop_loss_pct: float = 0.015,
        reverse_bars_m: int = 2,
        external_has_position: Optional[Callable[[str], bool]] = None,
        on_position_closed: Optional[Callable[[CandleClosedPositionResult], None]] = None,
    ):
        """
        external_has_position: 他戦略（trading.PositionManager等）が当該銘柄を
            既に保有しているかを確認する関数。Trueが返る銘柄には新規エントリーしない
            （「同一銘柄では既存戦略と排他的にする」というユーザー確定仕様のため）。
        """
        self.broker = broker
        self.position_manager = position_manager
        self.shares_per_symbol = shares_per_symbol
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.reverse_bars_m = reverse_bars_m
        self.external_has_position = external_has_position
        self.on_position_closed = on_position_closed

    # ------------------------------------------------------------------
    # エントリー
    # ------------------------------------------------------------------
    def try_entry(self, symbol: str, direction: CandleDirection, current_price: float) -> Optional[CandleOrderResult]:
        """
        direction: CandleDirection.LONG（陽線連続）または SHORT（陰線連続＝空売り）
        """
        if self.position_manager.has_position(symbol):
            return None  # 自戦略内での重複エントリー禁止

        if self.external_has_position is not None and self.external_has_position(symbol):
            logger.debug("既存戦略が当該銘柄を保有中のためエントリーを見送り: %s", symbol)
            return None

        estimated_cost = current_price * self.shares_per_symbol
        if self.broker.get_buying_power() < estimated_cost:
            logger.info("買付余力不足のためエントリー見送り: %s (概算必要額=%.0f)", symbol, estimated_cost)
            return None

        if direction == CandleDirection.LONG:
            result = self.broker.place_market_buy_to_open(symbol, self.shares_per_symbol)
        else:
            result = self.broker.place_market_sell_to_open(symbol, self.shares_per_symbol)
        result.reason = CandleOrderReason.ENTRY_CONSECUTIVE_CANDLES

        if result.status == CandleOrderStatus.FILLED and result.filled_price is not None:
            self.position_manager.open_position(
                symbol=symbol, direction=direction, qty=self.shares_per_symbol,
                entry_price=result.filled_price, entry_order_id=result.order_id,
                entry_at=result.filled_at or datetime.now(),
            )
        else:
            logger.warning("エントリー発注失敗: %s direction=%s reason=%s", symbol, direction, result.error_message)

        return result

    # ------------------------------------------------------------------
    # 決済
    # ------------------------------------------------------------------
    def try_exit(self, symbol: str, current_price: float, reason: CandleOrderReason) -> Optional[CandleOrderResult]:
        position = self.position_manager.get_position(symbol)
        if position is None:
            return None

        if position.direction == CandleDirection.LONG:
            result = self.broker.place_market_sell_to_close(symbol, position.qty)
        else:
            result = self.broker.place_market_buy_to_close(symbol, position.qty)
        result.reason = reason

        if result.status == CandleOrderStatus.FILLED and result.filled_price is not None:
            closed = self.position_manager.close_position(
                symbol=symbol, exit_price=result.filled_price,
                exit_at=result.filled_at or datetime.now(), reason=reason,
            )
            if self.on_position_closed is not None:
                self.on_position_closed(closed)
        else:
            logger.error(
                "決済発注に失敗しました（要リトライ・要手動確認）: %s reason=%s error=%s",
                symbol, reason, result.error_message,
            )

        return result

    # ------------------------------------------------------------------
    # 決済判定（優先順位: 損切 → 利確 → 通算逆足M本）
    # ------------------------------------------------------------------
    def check_exit_conditions(self, symbol: str, current_price: float) -> Optional[CandleOrderResult]:
        position = self.position_manager.get_position(symbol)
        if position is None:
            return None

        if position.direction == CandleDirection.LONG:
            stop_price = position.entry_price * (1 - self.stop_loss_pct)
            profit_price = position.entry_price * (1 + self.take_profit_pct)
            is_stop = current_price <= stop_price
            is_profit = current_price >= profit_price
        else:  # SHORT：価格上昇が損失、下落が利益
            stop_price = position.entry_price * (1 + self.stop_loss_pct)
            profit_price = position.entry_price * (1 - self.take_profit_pct)
            is_stop = current_price >= stop_price
            is_profit = current_price <= profit_price

        # 1. 損切を最優先
        if is_stop:
            return self.try_exit(symbol, current_price, CandleOrderReason.STOP_LOSS)

        # 2. 次に利確
        if is_profit:
            return self.try_exit(symbol, current_price, CandleOrderReason.TAKE_PROFIT)

        # 3. 最後に通算逆足M本
        if position.reverse_bar_count >= self.reverse_bars_m:
            return self.try_exit(symbol, current_price, CandleOrderReason.REVERSE_BARS)

        return None

    # ------------------------------------------------------------------
    # 強制決済（大引け・PUSH切断・バックテスト終了時）
    # ------------------------------------------------------------------
    def close_all_positions(
        self, reason: CandleOrderReason, price_lookup: Callable[[str], float]
    ) -> List[CandleOrderResult]:
        results: List[CandleOrderResult] = []
        for position in list(self.position_manager.get_all_positions()):
            price = price_lookup(position.symbol)
            result = self.try_exit(position.symbol, price, reason)
            if result is not None:
                results.append(result)
        return results
