"""
TradingController：PushClient → SignalEngine → OrderExecutor → GUI表示更新
を1本につなぐ、本番監視ループの中核。

設計方針:
  - 取引ロジック（SignalEngine/OrderExecutor/RiskManager等）はQtに依存しない
    trading/signals/api パッケージのクラスをそのまま使う
    （バックテストと同じクラス群を再利用し、本番/バックテストのロジック乖離を防ぐ）
  - PushClientのon_tickコールバックは別スレッド（WebSocket受信スレッド）から
    呼ばれるため、共有状態（SignalEngine/PositionManager等）へのアクセスは
    threading.Lock で保護する
  - GUIタブの更新は、このクラスが発行するQt Signalを経由して行う。
    Qtは別スレッドからemitされたSignalを自動的にGUIスレッドのイベントキューへ
    キューイングするため、_on_tick内で直接ウィジェットを触らなくても安全に反映できる
  - **発注処理（OrderExecutor経由のtry_entry/try_exit/トレール判定）は専用の
    ワーカースレッド（_order_worker_loop）で実行する**。kabuステーションAPIの
    sendorderは約定確認のポーリング（最大約4.5秒）を伴うため、これをPUSH受信
    スレッド上で同期的に行うと、その間他銘柄のTick処理が止まってしまう。
    バー確定時のTick処理（指標更新・キュー投入）はPUSH受信スレッドのまま高速に
    行い、実際の発注判定・実行はキュー（queue.Queue）経由でワーカースレッドに
    委譲することで、1銘柄の発注待ちが他銘柄の監視を妨げないようにしている。
    同一銘柄について発注処理中（キュー投入済み・未完了）の間は、その銘柄の
    新しいバーが確定してもキューへの再投入は行わない（_pending_symbolsで管理）。
"""

import logging
import queue
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from api import PushClient
from indicators import PriceTick
from signals import SignalEngine, WatchlistEntry
from trading import (
    BrokerClient,
    ClosedPositionResult,
    LiveBarAggregator,
    OrderExecutor,
    OrderReason,
    OrderResult,
    PositionManager,
    RiskManager,
)
from trading.risk_manager import DailyPnlState

logger = logging.getLogger(__name__)


def _find_ar_indicator_key(indicator_config: Dict[str, Dict[str, Any]]) -> Optional[str]:
    for key, spec in indicator_config.items():
        if spec.get("type") == "opening_range_ar":
            return key
    return None


class TradingController(QObject):
    tick_updated = Signal(str, dict, float, bool)  # symbol, snapshot, price, has_position
    signal_flagged = Signal(str, str)  # symbol, "ENTRY"/"EXIT"
    order_event = Signal(object)  # OrderResult
    connection_status_changed = Signal(bool)
    pnl_updated = Signal(object)  # DailyPnlState

    def __init__(
        self,
        broker: BrokerClient,
        settings: Dict[str, Any],
        watchlist: List[WatchlistEntry],
        ws_url: Optional[str] = None,
        db_path: Optional[Path | str] = None,
        pg_config_path: Optional[Path | str] = None,
        parent=None,
    ):
        """
        broker: trading.BrokerClient を満たすオブジェクト
                （本番は api.RestClient、テスト/デモは trading.SimulatedBrokerClient）
        settings: config.default_config.json 相当の設定dict
                  （indicators/entry_rule/exit_rule/各種閾値を含む）
        watchlist: signals.WatchlistEntry のリスト（スクリーナー順位順）。
                   前日終値・前日RSI・前日ボリンジャーバンド等の要約値を
                   保持していれば、寄り付き前の指標ウォームアップに使う。
        ws_url: PUSH APIのWebSocket URL。Noneの場合は start() 時にPushClientを起動しない
                （テストや、まだAPI接続を組み込みたくない場合向け）
        db_path: 指定するとSQLiteへ注文・ポジション・シグナルログ・日次損益を
                 永続化する。Noneの場合は永続化しない（既存のテスト・デモ用途向け）。
        pg_config_path: 指定すると、1分ごとに確定した4本値・出来高をPostgreSQLの
                 equities_bars_minuteテーブルへ書き込む（別バッチのminute_data_import
                 と同じテーブルを共有）。config.ini（[postgresql]セクション）のパスを渡す。
                 Noneの場合は書き込まない。
        """
        super().__init__(parent)
        self.broker = broker
        self.settings = settings
        self.watchlist = watchlist
        self.symbol_ranks: Dict[str, int] = {e.symbol: e.rank or (i + 1) for i, e in enumerate(watchlist)}
        self.symbol_names: Dict[str, str] = {e.symbol: e.name for e in watchlist}
        self._watchlist_by_symbol: Dict[str, WatchlistEntry] = {e.symbol: e for e in watchlist}

        self.signal_engine = SignalEngine(
            indicator_config=settings.get("indicators", {}),
            entry_rule=settings.get("entry_rule", {}),
            exit_rule=settings.get("exit_rule", {}),
        )
        self.position_manager = PositionManager()

        force_close_str = settings.get("force_close_time", "15:00")
        h, m = (int(x) for x in force_close_str.split(":"))
        entry_start_str = settings.get("entry_start_time", "09:00")
        eh, em = (int(x) for x in entry_start_str.split(":"))
        self.risk_manager = RiskManager(
            stop_loss_pct=settings.get("stop_loss_pct", 0.97),
            daily_profit_target=settings.get("daily_profit_target", 20000),
            daily_max_loss=settings.get("daily_max_loss", 20000),
            force_close_time=dtime(h, m),
            entry_start_time=dtime(eh, em),
            trailing_multiplier=settings.get("trailing_multiplier", 2.0),
            reentry_cooldown_bars=settings.get("reentry_cooldown_bars", 3),
            push_reconnect_max_retry=settings.get("push_reconnect_max_retry", 3),
        )
        self.order_executor = OrderExecutor(
            broker=broker,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            shares_per_symbol=settings.get("shares_per_symbol", 100),
            on_position_closed=self._on_position_closed,
        )

        self.bar_interval_minutes: int = settings.get("bar_interval_minutes", 3)
        self.bar_aggregator = LiveBarAggregator(interval_minutes=self.bar_interval_minutes)
        self.ar_indicator_key: Optional[str] = _find_ar_indicator_key(settings.get("indicators", {}))
        """
        indicators設定内でopening_range_ar型を使っているキー名（自動検出）。
        Noneの場合、トレール決済のAR値は常にNoneとなり発動しない
        （ARを使わない運用も可能なようにNoneを許容する設計）。
        """

        self.pg_bar_writer = None
        self._pg_bar_aggregator: Optional[LiveBarAggregator] = None
        if pg_config_path is not None:
            from storage import LiveBarWriter

            self.pg_bar_writer = LiveBarWriter(pg_config_path)
            # 指標計算用のbar_aggregator（既定3分等）とは別に、DB保存は常に1分足で行う
            # （「本番時は1分ごとに取得した4本値と売買高をDBに登録する」という要件のため）。
            self._pg_bar_aggregator = LiveBarAggregator(interval_minutes=1)

        self._lock = threading.Lock()
        self._last_prices: Dict[str, float] = {}

        # 発注処理を専用ワーカースレッドへ委譲するためのキューと管理状態
        self._order_queue: "queue.Queue" = queue.Queue()
        self._pending_symbols: set = set()
        """発注処理中（キュー投入済み〜ワーカーでの処理完了前）の銘柄集合。
        _lockで保護する。同一銘柄への重複キュー投入を防ぐために使う。"""
        self._order_worker_thread: Optional[threading.Thread] = None
        self._order_worker_stop = threading.Event()

        self._repos = None
        self._db_conn = None
        if db_path is not None:
            from storage import (
                ClosedTradeRepository,
                DailyPnlRepository,
                OrderRepository,
                PositionRepository,
                SignalLogRepository,
                connect,
            )

            conn = connect(db_path)
            self._db_conn = conn
            self._repos = {
                "orders": OrderRepository(conn),
                "positions": PositionRepository(conn),
                "closed_trades": ClosedTradeRepository(conn),
                "signal_log": SignalLogRepository(conn),
                "daily_pnl": DailyPnlRepository(conn),
            }
            logger.info("DB永続化を有効化しました: %s", db_path)

        self.push_client: Optional[PushClient] = None
        if ws_url is not None:
            self.push_client = PushClient(
                ws_url=ws_url,
                on_tick=self._on_tick,
                on_permanent_disconnect=self._on_permanent_disconnect,
                max_retry=self.risk_manager.push_reconnect_max_retry,
            )

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    def start(self) -> None:
        """
        認証・銘柄登録・PUSH接続開始を行う。ネットワークI/Oを含むため
        GUIスレッドから直接呼ばず、gui.workers.StartupWorker(QThread)経由で
        呼び出すこと。
        """
        if hasattr(self.broker, "authenticate"):
            self.broker.authenticate()
        if hasattr(self.broker, "unregister_all_symbols"):
            # register_symbols()は既存登録に追加されるだけで上書きしないため、
            # 前回セッションの銘柄が残っていると新しい監視リストに混ざって
            # PUSH配信され続けてしまう。必ず先に全解除してから登録する。
            self.broker.unregister_all_symbols()
        if hasattr(self.broker, "register_symbols"):
            self.broker.register_symbols([(sym, 1) for sym in self.symbol_ranks])

        self._warmup_indicators()

        self.risk_manager.start_new_trading_day(date.today())

        if self.pg_bar_writer is not None:
            self.pg_bar_writer.open()
            logger.info("PostgreSQLへの1分足書き込みを有効化しました")

        self._order_worker_stop.clear()
        self._order_worker_thread = threading.Thread(
            target=self._order_worker_loop, name="OrderWorker", daemon=True
        )
        self._order_worker_thread.start()

        if self.push_client is not None:
            self.push_client.start()

        self.connection_status_changed.emit(True)
        logger.info("本番監視ループを開始しました（監視銘柄数: %d）", len(self.watchlist))

    def _warmup_indicators(self) -> None:
        """
        各監視銘柄について、銘柄リストCSVで用意した前日の要約値
        （前日終値・前日RSI・前日ボリンジャーバンド等）を使って指標をウォームアップする。
        kabuステーションAPI経由での取得は行わない（分足の過去データを取得する
        エンドポイントがなく、単一の前日終値だけでは精度が低いため、
        CSVで別途用意した要約値を使う方式にした）。

        要約値が一部しかない銘柄はSignalEngine.warmup_symbol_from_summary()側で
        可能な範囲でフォールバックする。前日終値すら無い銘柄はウォームアップを
        スキップし、実際のTickが溜まるまで通常どおり待つ（致命的エラーにはしない）。
        """
        seed_timestamp = datetime.combine(date.today() - timedelta(days=1), dtime(15, 0))
        for symbol, entry in self._watchlist_by_symbol.items():
            if entry.prev_close is None and entry.prev_rsi is None and entry.prev_bb_middle is None:
                logger.warning("前日の要約値が無いためウォームアップをスキップ: %s", symbol)
                continue

            results = self.signal_engine.warmup_symbol_from_summary(
                symbol,
                timestamp=seed_timestamp,
                prev_close=entry.prev_close,
                prev_open=entry.prev_open,
                prev_high=entry.prev_high,
                prev_low=entry.prev_low,
                prev_rsi=entry.prev_rsi,
                prev_bb_upper=entry.prev_bb_upper,
                prev_bb_middle=entry.prev_bb_middle,
                prev_bb_lower=entry.prev_bb_lower,
            )
            failed = [key for key, ok in results.items() if not ok]
            if failed:
                logger.warning("ウォームアップ未達の指標があります: %s -> %s", symbol, failed)
            else:
                logger.info("ウォームアップ完了: %s", symbol)

    def stop(self) -> None:
        if self.push_client is not None:
            self.push_client.stop()

        # 発注ワーカースレッドの停止。キューに滞留しているタスクは処理せず
        # 破棄する（停止操作をした以上、これ以降の新規発注は行わないという判断）。
        self._order_worker_stop.set()
        self._order_queue.put(None)  # ワーカーのqueue.get()ブロックを解除するための番兵
        if self._order_worker_thread is not None:
            self._order_worker_thread.join(timeout=5.0)
            if self._order_worker_thread.is_alive():
                logger.warning("発注ワーカースレッドが5秒以内に停止しませんでした")
            self._order_worker_thread = None

        self.connection_status_changed.emit(False)
        logger.info("本番監視ループを停止しました")

    def close(self) -> None:
        """
        DB接続を明示的にクローズする。特にWindowsでは、sqlite3の接続を
        開いたままだと一時ファイル・DBファイルの削除やアプリ終了時に
        「別のプロセスが使用中です」エラーの原因になるため、
        アプリケーション終了時（および単体テストの後片付け）には必ず呼ぶこと。
        """
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None
        if self.pg_bar_writer is not None:
            self.pg_bar_writer.close()

    # ------------------------------------------------------------------
    # Tick処理（PushClientの受信スレッドから呼ばれる）
    # ------------------------------------------------------------------
    def _on_tick(self, tick: PriceTick) -> None:
        if tick.symbol not in self.symbol_ranks:
            # 現在の監視銘柄リストに含まれない銘柄のTickは無視する。
            # kabuステーション側の銘柄登録は追加方式のため、前回セッションの
            # 登録が残っていた場合の保険として、アプリ側でも二重にガードする。
            return

        with self._lock:
            self._last_prices[tick.symbol] = tick.price

            # 生Tickをbar_interval_minutes単位のバーへ集約する。
            # バーが確定した（区間が変わった）タイミングでのみ指標更新・
            # シグナル判定・トレール決済判定を行う。AR（当日の値幅目安）は
            # 確定したバーのTRを前提とした指標であり、生Tick単位では
            # 意味のある計算ができないため。
            completed_bar = self.bar_aggregator.add_tick(tick)

            if self._pg_bar_aggregator is not None:
                completed_1min_bar = self._pg_bar_aggregator.add_tick(tick)
                if completed_1min_bar is not None and self.pg_bar_writer is not None:
                    self._write_bar_to_postgres(completed_1min_bar)

            if completed_bar is not None:
                # 指標更新はここまで（in-memoryの計算のみで高速なため、
                # PUSH受信スレッド上で同期的に行って問題ない）。
                self.signal_engine.update(completed_bar)
                self.risk_manager.tick_reentry_cooldown(tick.symbol)

                # 実際の発注判定・実行（ブローカーへのREST呼び出しを伴い、
                # 約定確認のポーリングで数秒かかることがある）は専用の
                # 発注ワーカースレッドへ委譲する。同一銘柄について発注処理中
                # （キュー投入済み〜処理完了前）であれば、二重投入を避けるため
                # 今回のバーはスキップする（次に確定するバーで改めて判定される）。
                if tick.symbol not in self._pending_symbols:
                    self._pending_symbols.add(tick.symbol)
                    self._order_queue.put(("BAR", tick.symbol, completed_bar))

            snapshot = self.signal_engine.get_context(tick.symbol).snapshot()
            has_position = self.position_manager.has_position(tick.symbol)

        # 監視状況タブの現在値表示は、バー確定を待たず生Tickのたびに更新する
        # （指標値はバー確定時のスナップショットのまま、価格だけ最新になる。
        #  ポジション有無も、発注ワーカーでの処理が完了するまでは直前の状態のまま）
        self.tick_updated.emit(tick.symbol, snapshot, tick.price, has_position)

    def process_pending_orders_sync(self) -> None:
        """
        キューに溜まっているタスクを、ワーカースレッドを使わず呼び出し元スレッドで
        同期的に処理する。主に単体テスト用（実運用ではstart()でワーカースレッドを
        起動するため、通常この呼び出しは不要）。
        """
        while True:
            try:
                task = self._order_queue.get_nowait()
            except queue.Empty:
                break
            if task is None:
                continue
            kind = task[0]
            try:
                if kind == "BAR":
                    _, symbol, completed_bar = task
                    self._process_symbol_order(symbol, completed_bar)
                elif kind == "FORCE_LIQUIDATE":
                    self._process_force_liquidation()
            finally:
                if kind == "BAR":
                    with self._lock:
                        self._pending_symbols.discard(task[1])

    def _order_worker_loop(self) -> None:
        """
        発注ワーカースレッド本体。キューから (symbol, completed_bar) を取り出し、
        エントリー/決済/トレール決済の判定と、実際のブローカーへの発注実行を行う。
        ここでのOrderExecutor呼び出しはREST APIの約定確認ポーリングを含むため
        数秒かかることがあるが、このスレッドをブロックするだけで、PUSH受信スレッド
        （_on_tick）や他銘柄の監視には影響しない。
        """
        while not self._order_worker_stop.is_set():
            task = self._order_queue.get()
            if task is None:  # stop()からの番兵
                break

            kind = task[0]
            try:
                if kind == "BAR":
                    _, symbol, completed_bar = task
                    self._process_symbol_order(symbol, completed_bar)
                elif kind == "FORCE_LIQUIDATE":
                    self._process_force_liquidation()
                else:  # pragma: no cover
                    logger.warning("未知のタスク種別を無視しました: %s", kind)
            except Exception:  # noqa: BLE001
                logger.exception("発注ワーカーで予期しない例外が発生しました: task=%s", task)
            finally:
                if kind == "BAR":
                    with self._lock:
                        self._pending_symbols.discard(task[1])

    def _process_symbol_order(self, symbol: str, completed_bar: PriceTick) -> None:
        """
        1銘柄・1確定バー分のエントリー/決済/トレール判定と発注実行を行う
        （発注ワーカースレッドから呼ばれる）。
        """
        order_result: Optional[OrderResult] = None
        signal_fired: Optional[str] = None

        with self._lock:
            has_position_before = self.position_manager.has_position(symbol)

            if has_position_before:
                self.position_manager.update_high_water_mark(symbol, completed_bar.price)
                ar_value = None
                if self.ar_indicator_key is not None:
                    ar_value = (
                        self.signal_engine.get_context(symbol)
                        .snapshot()
                        .get(self.ar_indicator_key, {})
                        .get("ar")
                    )
                order_result = self.order_executor.check_and_apply_trailing_stop(
                    symbol, completed_bar.price, ar_value
                )
                if order_result is None:
                    exit_event = self.signal_engine.evaluate_exit(symbol)
                    if exit_event is not None:
                        signal_fired = "EXIT"
                        self._persist_signal(exit_event)
                        order_result = self.order_executor.try_exit(
                            symbol, completed_bar.price, OrderReason.SIGNAL
                        )
            else:
                entry_event = self.signal_engine.evaluate_entry(symbol)
                if entry_event is not None:
                    signal_fired = "ENTRY"
                    self._persist_signal(entry_event)
                    # ライブ監視では各銘柄のバーが逐次確定するため、都度try_entryで
                    # 余力チェックしながら発注する（バックテストのようなタイムスタンプ単位の
                    # 一括ランク付けはできないが、try_entry自体が余力不足時は
                    # 発注しないため、実質的に「余力がなくなるまで順次エントリー」という
                    # 要件は満たされる）
                    order_result = self.order_executor.try_entry(
                        symbol, completed_bar.price, now=completed_bar.timestamp
                    )

            if order_result is not None:
                self._persist_order(order_result)
                position = self.position_manager.get_position(symbol)
                if position is not None:
                    self._persist_open_position(position)
                elif self._repos is not None:
                    self._repos["positions"].remove(symbol)

            has_position_after = self.position_manager.has_position(symbol)
            snapshot = self.signal_engine.get_context(symbol).snapshot()
            daily_state = self.risk_manager.daily_state
            self._persist_daily_pnl(daily_state)

        # ワーカースレッドから発行するQt Signalも、GUIスレッドのイベントキューへ
        # 自動的にキューイングされるため安全（Qtのスレッドセーフティに準拠）。
        self.tick_updated.emit(symbol, snapshot, completed_bar.price, has_position_after)
        if signal_fired is not None:
            self.signal_flagged.emit(symbol, signal_fired)
        if order_result is not None:
            self.order_event.emit(order_result)
        self.pnl_updated.emit(daily_state)

    def _on_position_closed(self, closed: ClosedPositionResult) -> None:
        logger.info(
            "ポジション決済: %s qty=%d entry=%.1f exit=%.1f pnl=%+.0f reason=%s",
            closed.symbol, closed.qty, closed.entry_price, closed.exit_price,
            closed.realized_pnl, closed.reason.value,
        )
        if self._repos is not None:
            self._repos["closed_trades"].save(closed)

    # ------------------------------------------------------------------
    # DB永続化ヘルパー（db_path未指定時は何もしない）
    # ------------------------------------------------------------------
    def _persist_signal(self, event) -> None:
        if self._repos is not None:
            self._repos["signal_log"].save(event)

    def _persist_order(self, result: OrderResult) -> None:
        if self._repos is not None:
            self._repos["orders"].save(result)

    def _persist_open_position(self, position) -> None:
        if self._repos is not None:
            self._repos["positions"].upsert_open(position)

    def _persist_daily_pnl(self, state: DailyPnlState) -> None:
        if self._repos is not None:
            self._repos["daily_pnl"].save(state)

    def _write_bar_to_postgres(self, bar_tick: PriceTick) -> None:
        """
        確定した1分足をPostgreSQL（equities_bars_minute）へ書き込む。
        別バッチ（minute_data_import）と同じテーブルを共有するための機能。
        書き込み失敗時は取引を止めず、ログに記録するだけに留める
        （相場監視という主目的を、補助的なDB書き込みの失敗で止めないため）。
        """
        try:
            self.pg_bar_writer.write_bar(
                kabu_code=bar_tick.symbol,
                dt=bar_tick.timestamp,
                open_=bar_tick.open,
                high=bar_tick.high,
                low=bar_tick.low,
                close=bar_tick.price,
                volume=bar_tick.volume or 0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("PostgreSQLへの1分足書き込みに失敗しました: %s (%s)", bar_tick.symbol, e)

    def _on_permanent_disconnect(self) -> None:
        logger.error("PUSH API再接続に失敗したため、保有ポジションを強制決済します")
        self.risk_manager.notify_push_disconnected_permanently()
        self.connection_status_changed.emit(False)
        self.check_forced_liquidation()

    # ------------------------------------------------------------------
    # 定期チェック（GUI側のQTimerから呼び出す）
    # ------------------------------------------------------------------
    def check_forced_liquidation(self) -> None:
        """
        MainWindowのQTimer（GUIスレッド）から定期的に呼ばれる想定。
        実際の強制決済処理はブローカーへのREST呼び出し（約定確認ポーリング含む）を
        伴い数秒かかることがあるため、GUIスレッドをブロックしないよう
        発注ワーカースレッドへキュー経由で委譲し、即座に返る。
        """
        self._order_queue.put(("FORCE_LIQUIDATE",))

    def _process_force_liquidation(self) -> None:
        """強制決済の実処理（発注ワーカースレッドから呼ばれる）。"""
        with self._lock:
            results = self.order_executor.check_forced_liquidation(
                datetime.now(), price_lookup=lambda s: self._last_prices.get(s, 0.0)
            )
            for result in results:
                self._persist_order(result)
                if self._repos is not None:
                    self._repos["positions"].remove(result.symbol)
            daily_state = self.risk_manager.daily_state
            self._persist_daily_pnl(daily_state)

        for result in results:
            self.order_event.emit(result)
        if results:
            self.pnl_updated.emit(daily_state)
