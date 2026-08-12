"""
RiskManager：損益管理・損切判定・日次上限判定・強制決済トリガーの一元管理。

基本設計書4.5節・4.6節、CLAUDE.mdの確定仕様に対応:
  - 損切ライン：買値の97%（既定、設定可）
  - 日次利益上限：2万円（既定、設定可）→ 到達で新規停止＋全決済
  - 日次最大損失：2万円（既定、設定可）→ 到達で新規停止＋全決済
  - 強制引け決済：目安15:00（設定可）
  - PUSH切断：3回リトライ失敗で全決済
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, List, Optional

from .models import OrderReason


@dataclass
class DailyPnlState:
    trade_date: date
    realized_pnl: float = 0.0
    win_count: int = 0
    lose_count: int = 0
    daily_limit_hit: Optional[str] = None  # None / "PROFIT_TARGET" / "MAX_LOSS"
    stopped_at: Optional[datetime] = None


class RiskManager:
    def __init__(
        self,
        stop_loss_pct: float = 0.97,
        daily_profit_target: float = 20_000,
        daily_max_loss: float = 20_000,
        force_close_time: time = time(15, 0),
        entry_start_time: time = time(9, 0),
        trailing_multiplier: float = 2.0,
        reentry_cooldown_bars: int = 3,
        push_reconnect_max_retry: int = 3,
    ):
        self.stop_loss_pct = stop_loss_pct
        """
        買値に対する固定損切ライン（例: 0.97 = 買値の97%）。
        当初はこれを主たる損切手段としていたが、AR（当日の値幅目安）×倍率による
        トレール決済（trailing_multiplier）に置き換えることが決定した。
        このパラメータ自体・is_stop_loss_triggered()は後方互換のため残しているが、
        既定の運用フローではトレール決済のみを使い、固定%損切は呼び出さない。
        """
        self.daily_profit_target = daily_profit_target
        self.daily_max_loss = daily_max_loss
        self.force_close_time = force_close_time
        self.entry_start_time = entry_start_time
        """
        この時刻より前は新規エントリーを行わない（既定09:00＝実質無制限）。
        寄り付き直後（9:00〜9:15頃）はボラティリティが高くリスクが大きいため、
        例えば09:12等に設定することで序盤の値動きを避けられる。
        決済（損切・利確・強制決済）はこの制約の対象外（保有ポジションは
        時刻に関わらず通常どおり管理する）。
        """
        self.trailing_multiplier = trailing_multiplier
        """
        トレール決済の基準幅を「AR × trailing_multiplier」で計算する際の倍率。
        既定2.0（GUIから変更可能。未設定ならAR×2で運用する）。
        """
        self.push_reconnect_max_retry = push_reconnect_max_retry

        self.reentry_cooldown_bars = reentry_cooldown_bars
        """
        決済後、同一銘柄への再エントリーを禁止するバー本数（既定3本）。
        「決済直後に同じ理由で即座に再エントリーしてしまう」ことを避けるための
        クールダウン。バー間隔（1分足・3分足等）に関わらず「何本分」で数える
        （例：3分足運用なら3本＝9分間、1分足運用なら3本＝3分間、と実際の
        経過時間はバー間隔に応じて自動的に変わる）。0を指定すると無効化される。
        """
        self._reentry_cooldown_remaining: Dict[str, int] = {}

        self._daily_state: Optional[DailyPnlState] = None
        self._trading_halted = False
        self._halt_reason: Optional[OrderReason] = None
        self._push_disconnected_permanently = False

    # ------------------------------------------------------------------
    # 日次状態管理
    # ------------------------------------------------------------------
    def start_new_trading_day(self, trade_date: date) -> None:
        self._daily_state = DailyPnlState(trade_date=trade_date)
        self._trading_halted = False
        self._halt_reason = None
        self._push_disconnected_permanently = False
        self._reentry_cooldown_remaining = {}

    @property
    def daily_state(self) -> DailyPnlState:
        if self._daily_state is None:
            # 明示的にstart_new_trading_dayを呼んでいない場合は当日で自動初期化する
            self.start_new_trading_day(date.today())
        return self._daily_state  # type: ignore[return-value]

    @property
    def trading_halted(self) -> bool:
        """Trueの間は新規エントリーを一切行ってはならない。"""
        return self._trading_halted

    @property
    def halt_reason(self) -> Optional[OrderReason]:
        return self._halt_reason

    # ------------------------------------------------------------------
    # 損切判定
    # ------------------------------------------------------------------
    def is_stop_loss_triggered(self, entry_price: float, current_price: float) -> bool:
        """買値の stop_loss_pct 倍を下回ったら損切対象。"""
        threshold = entry_price * self.stop_loss_pct
        return current_price < threshold

    # ------------------------------------------------------------------
    # 強制引け決済
    # ------------------------------------------------------------------
    def is_force_close_time(self, now: datetime) -> bool:
        return now.time() >= self.force_close_time

    # ------------------------------------------------------------------
    # 寄り付き直後のエントリー禁止時間帯
    # ------------------------------------------------------------------
    def is_entry_time_allowed(self, now: datetime) -> bool:
        """
        entry_start_time以降であれば新規エントリーを許可する。
        決済（損切・利確・強制決済）はこの制約の対象外。
        """
        return now.time() >= self.entry_start_time

    # ------------------------------------------------------------------
    # 決済後の再エントリークールダウン（分足×reentry_cooldown_bars本は無エントリー）
    # ------------------------------------------------------------------
    def record_exit(self, symbol: str) -> None:
        """
        決済確定時に呼ぶ。reentry_cooldown_barsが1以上なら、以降その本数分の
        バーが確定するまで当該銘柄への新規エントリーを禁止する。
        """
        if self.reentry_cooldown_bars > 0:
            self._reentry_cooldown_remaining[symbol] = self.reentry_cooldown_bars

    def tick_reentry_cooldown(self, symbol: str) -> None:
        """
        当該銘柄のバーが1本確定するたびに呼ぶ。クールダウン残数を1減らし、
        0になったら解除する。クールダウン対象外の銘柄には何もしない。
        """
        remaining = self._reentry_cooldown_remaining.get(symbol)
        if remaining is None:
            return
        if remaining <= 1:
            del self._reentry_cooldown_remaining[symbol]
        else:
            self._reentry_cooldown_remaining[symbol] = remaining - 1

    def is_reentry_allowed(self, symbol: str) -> bool:
        """クールダウン中でなければTrue（クールダウン未使用の銘柄も常にTrue）。"""
        return self._reentry_cooldown_remaining.get(symbol, 0) <= 0

    # ------------------------------------------------------------------
    # PUSH切断
    # ------------------------------------------------------------------
    def notify_push_disconnected_permanently(self) -> None:
        """
        PUSH API側が最大リトライ回数（push_reconnect_max_retry）を
        使い切っても再接続できなかった場合に呼び出す。
        以降、新規エントリーは停止し、保有ポジションは強制決済対象となる。
        """
        self._push_disconnected_permanently = True
        self._trading_halted = True
        self._halt_reason = OrderReason.PUSH_DISCONNECT

    @property
    def push_disconnected_permanently(self) -> bool:
        return self._push_disconnected_permanently

    # ------------------------------------------------------------------
    # 実現損益の記録・日次上限判定
    # ------------------------------------------------------------------
    def record_realized_pnl(self, amount: float) -> Optional[str]:
        """
        決済確定時に呼び出す。日次実現損益を加算し、
        日次利益上限／日次最大損失に到達したかどうかを判定する。

        戻り値: None（未到達） / "PROFIT_TARGET" / "MAX_LOSS"
        到達した場合は trading_halted を True にし、以降の新規エントリーを止める。
        """
        state = self.daily_state
        state.realized_pnl += amount
        if amount >= 0:
            state.win_count += 1
        else:
            state.lose_count += 1

        if state.daily_limit_hit is not None:
            return state.daily_limit_hit

        if state.realized_pnl >= self.daily_profit_target:
            state.daily_limit_hit = "PROFIT_TARGET"
        elif state.realized_pnl <= -abs(self.daily_max_loss):
            state.daily_limit_hit = "MAX_LOSS"

        if state.daily_limit_hit is not None:
            state.stopped_at = datetime.now()
            self._trading_halted = True
            self._halt_reason = (
                OrderReason.DAILY_PROFIT_TARGET
                if state.daily_limit_hit == "PROFIT_TARGET"
                else OrderReason.DAILY_MAX_LOSS
            )

        return state.daily_limit_hit

    def should_force_close_all(self, now: datetime) -> Optional[OrderReason]:
        """
        現在、保有ポジション全件を強制決済すべき状態かどうかを判定する。
        呼び出し元（トレーディングループ）はこれを毎ティック確認し、
        Noneでなければ全ポジションを成行決済すること。
        """
        if self._push_disconnected_permanently:
            return OrderReason.PUSH_DISCONNECT
        if self.daily_state.daily_limit_hit == "PROFIT_TARGET":
            return OrderReason.DAILY_PROFIT_TARGET
        if self.daily_state.daily_limit_hit == "MAX_LOSS":
            return OrderReason.DAILY_MAX_LOSS
        if self.is_force_close_time(now):
            if not self._trading_halted:
                # 大引け時刻を過ぎたら、決済だけでなく新規エントリーも停止する
                # （デイトレード前提のため、引け後に新規で買うのは想定外の挙動になる）
                self._trading_halted = True
                self._halt_reason = OrderReason.FORCE_CLOSE_TIME
            return OrderReason.FORCE_CLOSE_TIME
        return None
