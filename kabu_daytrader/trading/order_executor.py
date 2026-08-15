"""
OrderExecutor：発注・決済の実行を担う。

基本設計書8.2〜8.3節の処理フローに対応:
  - エントリー：スクリーナー順位の上位から、現物買付余力がなくなるまで順次発注
  - 同一銘柄への重複エントリーはしない
  - 決済：シグナル・損切・日次上限到達・強制引け・PUSH切断の各理由に応じて成行決済

PositionManager・RiskManagerと連携し、実際の発注処理（BrokerClient）は
差し替え可能にしてある（本番はkabuステーションREST API、テストはSimulatedBrokerClient）。
"""

import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .broker_client import BrokerClient
from .models import ClosedPositionResult, OrderReason, OrderResult, OrderStatus, PositionDirection
from .position_manager import PositionManager
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(
        self,
        broker: BrokerClient,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        shares_per_symbol: int = 100,
        on_position_closed: Optional[Callable[[ClosedPositionResult], None]] = None,
    ):
        self.broker = broker
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.shares_per_symbol = shares_per_symbol
        self.on_position_closed = on_position_closed
        """
        ポジションが決済確定するたびに呼ばれるコールバック（任意）。
        backtest.BacktestEngine の取引履歴収集や、将来的なDBへの
        orders/positions記録・GUIへの通知等、本番/バックテスト共通で
        使える差し込みポイントとして用意している。
        """

    # ------------------------------------------------------------------
    # エントリー（信用取引：買い建て＝LONG、売り建て＝SHORT）
    # ------------------------------------------------------------------
    def try_entry(
        self,
        symbol: str,
        current_price: float,
        now: Optional[datetime] = None,
        direction: PositionDirection = PositionDirection.LONG,
    ) -> Optional[OrderResult]:
        """
        1銘柄への新規エントリーを試みる（信用取引。買い建て＝LONG、売り建て＝SHORT）。
        以下の場合は発注そのものを行わず None を返す（発注失敗としてログしない）:
          - 新規エントリー停止中（日次上限到達／PUSH切断確定後）
          - entry_start_time より前（寄り付き直後のボラティリティが高い時間帯を回避）
          - 決済直後のクールダウン中（reentry_cooldown_bars本のバーが経過していない）
          - 既に当該銘柄のポジションを保有中（方向を問わず重複エントリー禁止）
          - 買付余力が不足している

        now: エントリー可否の時刻判定に使う時刻。省略時は現在時刻を使う
             （バックテストではTickのタイムスタンプを明示的に渡すこと）。
        direction: PositionDirection.LONG（買い建て）または SHORT（売り建て＝空売り）。
        """
        now = now or datetime.now()

        if self.risk_manager.trading_halted:
            logger.info("新規エントリー停止中のためスキップ: %s", symbol)
            return None

        if not self.risk_manager.is_entry_time_allowed(now):
            logger.debug(
                "エントリー許可時刻(%s)前のためスキップ: %s (現在時刻=%s)",
                self.risk_manager.entry_start_time, symbol, now.time(),
            )
            return None

        if not self.risk_manager.is_reentry_allowed(symbol):
            logger.debug("決済後のクールダウン中のためスキップ: %s", symbol)
            return None

        if self.position_manager.has_position(symbol):
            logger.debug("既にポジション保有中のためスキップ（重複エントリー禁止）: %s", symbol)
            return None

        estimated_cost = current_price * self.shares_per_symbol
        if self.broker.get_buying_power() < estimated_cost:
            logger.info("買付余力不足のためエントリー見送り: %s (概算必要額=%.0f)", symbol, estimated_cost)
            return None

        if direction == PositionDirection.LONG:
            result = self.broker.place_margin_buy_to_open(symbol, self.shares_per_symbol)
        else:
            result = self.broker.place_margin_sell_to_open(symbol, self.shares_per_symbol)
        result.reason = OrderReason.SIGNAL

        if result.status == OrderStatus.FILLED and result.filled_price is not None:
            self.position_manager.open_position(
                symbol=symbol,
                qty=self.shares_per_symbol,
                entry_price=result.filled_price,
                entry_order_id=result.order_id,
                entry_at=result.filled_at or datetime.now(),
                direction=direction,
            )
        else:
            logger.warning("エントリー発注失敗: %s direction=%s reason=%s", symbol, direction, result.error_message)

        return result

    def try_entries_in_rank_order(
        self,
        ranked_candidates: List[Tuple[str, float, PositionDirection]],
        now: Optional[datetime] = None,
    ) -> List[OrderResult]:
        """
        ranked_candidates: [(symbol, current_price, direction), ...] スクリーナー順位順
        （リストの先頭が最も優先度が高い銘柄）。方向はLONG/SHORTが混在してよい。

        買付余力が尽きた時点、新規エントリーが停止された時点、または
        entry_start_timeより前の時点で、それ以降の銘柄への発注は行わない
        （要件定義書4.4節の買付順序どおり）。
        既にポジションを保有している銘柄はスキップして次の候補へ進む。

        now: エントリー可否の時刻判定に使う時刻。省略時は現在時刻を使う。
        """
        now = now or datetime.now()

        if not self.risk_manager.is_entry_time_allowed(now):
            logger.debug(
                "エントリー許可時刻(%s)前のため候補への発注を見送ります（現在時刻=%s）",
                self.risk_manager.entry_start_time, now.time(),
            )
            return []

        results: List[OrderResult] = []
        for symbol, price, direction in ranked_candidates:
            if self.risk_manager.trading_halted:
                logger.info("新規エントリー停止中のため以降の候補への発注を打ち切ります")
                break

            if self.position_manager.has_position(symbol):
                continue  # 重複エントリー禁止。次の候補へ

            if not self.risk_manager.is_reentry_allowed(symbol):
                continue  # 決済後のクールダウン中。次の候補へ

            estimated_cost = price * self.shares_per_symbol
            if self.broker.get_buying_power() < estimated_cost:
                logger.info("買付余力が尽きたため、以降の候補への発注を打ち切ります（%s以降）", symbol)
                break

            result = self.try_entry(symbol, price, now=now, direction=direction)
            if result is not None:
                results.append(result)

        return results

    # ------------------------------------------------------------------
    # 決済（信用返済。保有ポジションのdirectionに応じて反対売買を行う）
    # ------------------------------------------------------------------
    def try_exit(
        self, symbol: str, current_price: float, reason: OrderReason
    ) -> Optional[OrderResult]:
        position = self.position_manager.get_position(symbol)
        if position is None:
            logger.debug("保有ポジションがないため決済スキップ: %s", symbol)
            return None

        if position.direction == PositionDirection.LONG:
            result = self.broker.place_margin_sell_to_close(symbol, position.qty)
        else:
            result = self.broker.place_margin_buy_to_close(symbol, position.qty)
        result.reason = reason

        if result.status == OrderStatus.FILLED and result.filled_price is not None:
            closed = self.position_manager.close_position(
                symbol=symbol,
                exit_price=result.filled_price,
                exit_at=result.filled_at or datetime.now(),
                reason=reason,
            )
            self.risk_manager.record_realized_pnl(closed.realized_pnl)
            self.risk_manager.record_exit(symbol)
            if self.on_position_closed is not None:
                self.on_position_closed(closed)
        else:
            logger.error(
                "決済発注に失敗しました（要リトライ・要手動確認）: %s reason=%s error=%s",
                symbol, reason, result.error_message,
            )

        return result

    def check_and_apply_stop_loss(
        self, symbol: str, current_price: float
    ) -> Optional[OrderResult]:
        position = self.position_manager.get_position(symbol)
        if position is None:
            return None
        if self.risk_manager.is_stop_loss_triggered(position.entry_price, current_price):
            return self.try_exit(symbol, current_price, OrderReason.STOP_LOSS)
        return None

    def check_and_apply_trailing_stop(
        self, symbol: str, current_price: float, ar_value: Optional[float]
    ) -> Optional[OrderResult]:
        """
        AR（当日の値幅目安）× trailing_multiplier をトレール幅として、
        保有中銘柄の最高値からその幅だけ下落したら成行決済する。

        呼び出し前に position_manager.update_high_water_mark() で
        当該Tickの現在値を反映しておくこと（このメソッド自体は
        最高値の更新は行わず、判定のみ行う）。

        ar_value: 対象銘柄のAR。まだ確定していない（is_ready()前）場合はNoneを渡すこと。
                  Noneの場合はトレール判定自体を行わない（発動しない）。
        """
        position = self.position_manager.get_position(symbol)
        if position is None or ar_value is None:
            return None

        trail_width = ar_value * self.risk_manager.trailing_multiplier
        trail_stop_price = position.high_water_mark - trail_width
        if current_price <= trail_stop_price:
            return self.try_exit(symbol, current_price, OrderReason.TRAILING_STOP)
        return None

    def close_all_positions(
        self, reason: OrderReason, price_lookup: Callable[[str], float]
    ) -> List[OrderResult]:
        """
        保有中の全ポジションを成行決済する。
        日次上限到達／強制引け／PUSH切断確定 のいずれかで呼び出される想定。
        """
        results: List[OrderResult] = []
        for position in list(self.position_manager.get_all_positions()):
            price = price_lookup(position.symbol)
            result = self.try_exit(position.symbol, price, reason)
            if result is not None:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # 定期チェック（RiskManagerと連動した強制決済トリガー）
    # ------------------------------------------------------------------
    def check_forced_liquidation(
        self, now: datetime, price_lookup: Callable[[str], float]
    ) -> List[OrderResult]:
        """
        RiskManager.should_force_close_all() の結果に応じて、
        必要であれば保有ポジション全件を強制決済する。
        トレーディングループから定期的（例: 数秒〜数十秒間隔）に呼び出す想定。

        大引け時刻(force_close_time)を過ぎている間は毎回reasonがFORCE_CLOSE_TIME等を
        返し続けるが、保有ポジションが既に0件であれば実行することは何もないため、
        ログ出力・決済処理はスキップする（時刻経過後もタイマーが動き続ける限り
        警告ログが延々と出続けてしまう問題を防ぐため）。
        """
        reason = self.risk_manager.should_force_close_all(now)
        if reason is None:
            return []
        if not self.position_manager.get_all_positions():
            return []
        logger.warning("強制決済トリガー発生: reason=%s", reason)
        return self.close_all_positions(reason, price_lookup)
