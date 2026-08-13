"""
gui.TradingController の動作確認用テスト。
実際のkabuステーションAPI・WebSocket接続は行わず、SimulatedBrokerClientを使い、
PushClientの代わりに controller._on_tick() を直接呼び出してロジックを検証する。

QT_QPA_PLATFORM=offscreen python tests/test_trading_controller.py で実行する。
"""

import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from indicators import PriceTick  # noqa: E402
from gui.trading_controller import TradingController  # noqa: E402
from signals import WatchlistEntry  # noqa: E402
from trading import SimulatedBrokerClient  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def _make_settings(**overrides):
    settings = {
        "shares_per_symbol": 100,
        "stop_loss_pct": 0.97,
        "daily_profit_target": 20000,
        "daily_max_loss": 20000,
        "force_close_time": "15:00",
        "push_reconnect_max_retry": 3,
        "bar_interval_minutes": 1,
        "indicators": {"rsi3": {"type": "rsi", "period": 3}},
        "entry_rule": {"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        "exit_rule": {"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">", "value": 70}]},
    }
    settings.update(overrides)
    return settings


def test_controller_constructs_without_ws_url():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    assert controller.push_client is None
    assert controller.symbol_ranks == {"9432": 1}
    print("test_controller_constructs_without_ws_url: OK")


def test_controller_tick_triggers_entry_and_emits_signals():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    received_ticks = []
    received_orders = []
    received_signals = []
    controller.tick_updated.connect(lambda *args: received_ticks.append(args))
    controller.order_event.connect(lambda r: received_orders.append(r))
    controller.signal_flagged.connect(lambda s, t: received_signals.append((s, t)))

    base_dt = datetime(2026, 7, 28, 9, 0)
    broker.set_current_price("9432", 100.0)
    prices = [100 - i for i in range(6)]  # 継続的な下落でRSIを下げる
    for i, p in enumerate(prices):
        broker.set_current_price("9432", float(p))
        tick = PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000)
        controller._on_tick(tick)
        controller.process_pending_orders_sync()  # 実運用ではワーカースレッドが都度処理する想定

    assert len(received_ticks) >= len(prices)  # バー確定時は_on_tickと処理後の両方でtick_updatedが発行される
    assert any(r.symbol == "9432" for r in received_orders), "エントリーのOrderResultが発行されるはず"
    assert any(t == "ENTRY" for _s, t in received_signals)
    assert controller.position_manager.has_position("9432") is True
    print("test_controller_tick_triggers_entry_and_emits_signals: OK", len(received_orders))


def test_controller_trailing_stop_triggers_exit():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    settings = _make_settings(
        indicators={"rsi3": {"type": "rsi", "period": 3}, "ar": {"type": "opening_range_ar", "bar_count": 2}},
        exit_rule={"operator": "AND", "conditions": []},  # 決済はトレールのみに任せる
        trailing_multiplier=2.0,
    )
    controller = TradingController(broker=broker, settings=settings, watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    received_orders = []
    controller.order_event.connect(lambda r: received_orders.append(r))

    base_dt = datetime(2026, 7, 28, 9, 0)
    # 0,1本目：AR算出用の下地。2,3本目：下落でRSIを下げエントリー成立。
    # 4本目：急騰し最高値を更新。5本目：急落しトレール決済が発動するはず。
    prices = [100, 99, 98, 95, 90, 110, 100, 90]
    for i, p in enumerate(prices):
        broker.set_current_price("9432", float(p))
        controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000))
        controller.process_pending_orders_sync()  # 実運用ではワーカースレッドが都度処理する想定

    assert controller.position_manager.has_position("9432") is False
    from trading import OrderReason
    assert any(r.reason == OrderReason.TRAILING_STOP for r in received_orders)
    print("test_controller_trailing_stop_triggers_exit: OK")


def test_controller_check_forced_liquidation_closes_all_positions():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(
        broker=broker,
        settings=_make_settings(daily_max_loss=1_000_000, daily_profit_target=1_000_000),
        watchlist=[WatchlistEntry(symbol="9432", name="NTT")],
    )
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    base_dt = datetime(2026, 7, 28, 9, 0)
    prices_in = [100, 99, 98, 97, 96]  # 最後の1本はバー確定用の追加Tick
    for i, p in enumerate(prices_in):
        broker.set_current_price("9432", float(p))
        controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000))
        controller.process_pending_orders_sync()
    assert controller.position_manager.has_position("9432") is True

    # PUSH切断確定をシミュレート
    received_orders = []
    controller.order_event.connect(lambda r: received_orders.append(r))
    controller._on_permanent_disconnect()
    controller.process_pending_orders_sync()  # check_forced_liquidation()はキュー投入のみのため同期フラッシュが必要

    assert controller.position_manager.has_position("9432") is False
    from trading import OrderReason
    assert any(r.reason == OrderReason.PUSH_DISCONNECT for r in received_orders)
    print("test_controller_check_forced_liquidation_closes_all_positions: OK")


def test_controller_persists_to_db_when_db_path_given():
    import tempfile

    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    with tempfile.TemporaryDirectory() as td:
        db_path = f"{td}/test.db"
        controller = TradingController(
            broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")], db_path=db_path,
        )
        controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

        base_dt = datetime(2026, 7, 28, 9, 0)
        prices = [100 - i for i in range(6)]
        for i, p in enumerate(prices):
            broker.set_current_price("9432", float(p))
            controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000))
            controller.process_pending_orders_sync()

        assert controller._repos is not None
        orders = controller._repos["orders"].list_recent()
        assert len(orders) >= 1
        positions = controller._repos["positions"].list_open()
        assert len(positions) == 1
        controller.close()  # Windowsで一時フォルダ削除時に「使用中」エラーになるのを防ぐ
        print("test_controller_persists_to_db_when_db_path_given: OK", len(orders))


def test_controller_db_access_from_different_thread_does_not_raise():
    """
    実際のPushClientはWebSocket受信スレッドからon_tickを呼ぶため、
    TradingControllerの生成スレッド（GUIスレッド）とは別スレッドから
    _on_tick経由でsqlite3接続にアクセスすることになる。
    check_same_thread=Falseにしていないと
    'SQLite objects created in a thread can only be used in that same thread'
    が発生するため、それを再現して検証する。
    """
    import tempfile
    import threading

    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    with tempfile.TemporaryDirectory() as td:
        db_path = f"{td}/test.db"
        # メインスレッド（このテスト関数の実行スレッド）でController生成
        controller = TradingController(
            broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")], db_path=db_path,
        )
        controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

        errors = []

        def worker():
            try:
                base_dt = datetime(2026, 7, 28, 9, 0)
                prices = [100 - i for i in range(6)]
                for i, p in enumerate(prices):
                    broker.set_current_price("9432", float(p))
                    controller._on_tick(
                        PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000)
                    )
                # 発注（＝sqlite書込）自体はこのスレッド内で同期実行し、
                # 元のテスト意図（別スレッドからのDBアクセス）を保つ
                controller.process_pending_orders_sync()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)

        assert not errors, f"別スレッドからのDBアクセスで例外が発生: {errors}"
        assert controller._repos["orders"].list_recent(), "別スレッドから保存された注文が読み取れるはず"
        controller.close()  # Windowsで一時フォルダ削除時に「使用中」エラーになるのを防ぐ
        print("test_controller_db_access_from_different_thread_does_not_raise: OK")


def test_controller_ignores_ticks_for_symbols_not_in_watchlist():
    """
    前回セッションの銘柄登録が残っていた場合の保険として、
    現在のwatchlistに含まれない銘柄のTickは無視されることを確認する。
    """
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    received_ticks = []
    controller.tick_updated.connect(lambda *args: received_ticks.append(args))

    base_dt = datetime(2026, 7, 28, 9, 0)
    # 監視対象外の銘柄"9999"のTickは無視されるはず
    broker.set_current_price("9999", 100.0)
    controller._on_tick(PriceTick(symbol="9999", timestamp=base_dt, price=100.0, volume=1000))
    assert len(received_ticks) == 0
    assert "9999" not in controller._last_prices

    # 監視対象の銘柄"9432"は通常通り処理される
    broker.set_current_price("9432", 100.0)
    controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt, price=100.0, volume=1000))
    assert len(received_ticks) == 1
    print("test_controller_ignores_ticks_for_symbols_not_in_watchlist: OK")


def test_controller_start_unregisters_before_registering():
    """
    start()実行時に、register_symbols()の前にunregister_all_symbols()が
    呼ばれることを確認する（前回セッションの銘柄が残らないようにするため）。
    """
    call_order = []

    class FakeBroker:
        def authenticate(self):
            call_order.append("authenticate")

        def unregister_all_symbols(self):
            call_order.append("unregister_all")

        def register_symbols(self, symbols):
            call_order.append("register")
            self.registered = symbols

        def get_buying_power(self):
            return 1_000_000.0

        def place_market_buy(self, symbol, qty):
            raise NotImplementedError

        def place_market_sell(self, symbol, qty):
            raise NotImplementedError

    broker = FakeBroker()
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT"), WatchlistEntry(symbol="7203", name="トヨタ")])
    controller.start()

    assert call_order == ["authenticate", "unregister_all", "register"], call_order
    assert broker.registered == [("9432", 1), ("7203", 1)]
    print("test_controller_start_unregisters_before_registering: OK")


def test_controller_start_warms_up_indicators_from_watchlist_entry_summary():
    """
    銘柄リストCSV（WatchlistEntry）に前日終値・前日RSI・前日BBを持たせておくと、
    start()時点でSignalEngineが即座にis_ready()になることを確認する
    （kabuステーションAPI側への前日終値問い合わせは行わない設計）。
    """
    call_order = []

    class FakeBroker:
        def authenticate(self):
            call_order.append("authenticate")

        def unregister_all_symbols(self):
            call_order.append("unregister_all")

        def register_symbols(self, symbols):
            call_order.append("register")

        def get_buying_power(self):
            return 1_000_000.0

        def place_market_buy(self, symbol, qty):
            raise NotImplementedError

        def place_market_sell(self, symbol, qty):
            raise NotImplementedError

    broker = FakeBroker()
    watchlist = [
        WatchlistEntry(
            symbol="9432", name="NTT", rank=1,
            prev_close=1000.0, prev_open=995.0, prev_high=1010.0, prev_low=990.0,
            prev_rsi=42.0, prev_bb_upper=1040.0, prev_bb_middle=1000.0, prev_bb_lower=960.0,
        ),
        WatchlistEntry(symbol="7203", name="トヨタ", rank=2),  # 要約値なし（フォールバック無しでスキップされるはず）
    ]
    settings = _make_settings(**{
        "indicators": {
            "rsi3": {"type": "rsi", "period": 3},
            "bb": {"type": "bollinger", "period": 4, "num_std": 2},
        },
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    })
    controller = TradingController(broker=broker, settings=settings, watchlist=watchlist)
    controller.start()

    ctx = controller.signal_engine.get_context("9432")
    assert ctx.all_ready() is True
    snap = ctx.snapshot()
    assert abs(snap["rsi3"]["rsi3"] - 42.0) < 0.01, snap
    assert abs(snap["bb"]["middle"] - 1000.0) < 0.01, snap

    # 要約値の無い7203はウォームアップされず、is_ready()はFalseのまま
    ctx2 = controller.signal_engine.get_context("7203")
    assert ctx2.all_ready() is False
    print("test_controller_start_warms_up_indicators_from_watchlist_entry_summary: OK", snap)


def test_controller_writes_1min_bars_to_postgres_on_bar_completion():
    import tempfile
    from unittest.mock import MagicMock

    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    settings = _make_settings(bar_interval_minutes=3)  # 指標計算は3分足、DB書き込みは常に1分足のはず

    with tempfile.TemporaryDirectory() as td:
        config_path = f"{td}/config.ini"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("[postgresql]\nhost=localhost\nport=5433\ndbname=quants\nuser=postgres\npassword=dummy\n")

        controller = TradingController(
            broker=broker, settings=settings, watchlist=[WatchlistEntry(symbol="9432", name="NTT")],
            pg_config_path=config_path,
        )
        # 実際のPostgreSQL接続は行わず、LiveBarWriterをモックに差し替えて検証する
        mock_writer = MagicMock()
        controller.pg_bar_writer = mock_writer

        base_dt = datetime(2026, 7, 28, 9, 0)
        # 1分ごとに4本Tick投入 → 1分足が3回確定するはず（3分足の指標計算とは別に）
        for i, p in enumerate([100.0, 101.0, 99.0, 102.0]):
            broker.set_current_price("9432", p)
            controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=p, volume=1000))

        assert mock_writer.write_bar.call_count == 3  # 4本Tick投入で3本の1分足が確定する
        first_call_kwargs = mock_writer.write_bar.call_args_list[0].kwargs
        assert first_call_kwargs["kabu_code"] == "9432"
        assert first_call_kwargs["dt"] == base_dt
        print("test_controller_writes_1min_bars_to_postgres_on_bar_completion: OK", mock_writer.write_bar.call_count)


def test_controller_start_and_close_manage_postgres_connection():
    import tempfile
    from unittest.mock import MagicMock

    call_order = []

    class FakeBroker:
        def authenticate(self):
            call_order.append("authenticate")

        def unregister_all_symbols(self):
            pass

        def register_symbols(self, symbols):
            pass

        def get_buying_power(self):
            return 1_000_000.0

        def place_market_buy(self, symbol, qty):
            raise NotImplementedError

        def place_market_sell(self, symbol, qty):
            raise NotImplementedError

    with tempfile.TemporaryDirectory() as td:
        config_path = f"{td}/config.ini"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("[postgresql]\nhost=localhost\nport=5433\ndbname=quants\nuser=postgres\npassword=dummy\n")

        controller = TradingController(
            broker=FakeBroker(), settings=_make_settings(),
            watchlist=[WatchlistEntry(symbol="9432", name="NTT")], pg_config_path=config_path,
        )
        controller.pg_bar_writer = MagicMock()
        controller.start()
        controller.pg_bar_writer.open.assert_called_once()

        controller.close()
        controller.pg_bar_writer.close.assert_called_once()
    print("test_controller_start_and_close_manage_postgres_connection: OK")


def test_controller_start_runs_real_worker_thread_that_processes_orders_async():
    """
    start()で起動される発注ワーカースレッドが、実際に別スレッドとして
    キューを処理することを検証する（_on_tick自体は即座に返ることも確認）。
    """
    import time as time_module

    call_order = []

    class FakeBroker:
        def authenticate(self):
            call_order.append("authenticate")

        def unregister_all_symbols(self):
            pass

        def register_symbols(self, symbols):
            pass

        def get_buying_power(self):
            return 1_000_000.0

        def place_market_buy(self, symbol, qty):
            from trading.models import OrderReason, OrderResult, OrderSide, OrderStatus
            call_order.append("place_market_buy")
            return OrderResult(
                order_id="SIM-1", symbol=symbol, side=OrderSide.BUY, qty=qty,
                status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
                requested_at=datetime.now(), filled_at=datetime.now(), filled_price=100.0,
            )

        def place_market_sell(self, symbol, qty):
            raise NotImplementedError

    settings = _make_settings()
    controller = TradingController(broker=FakeBroker(), settings=settings, watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.start()
    try:
        assert controller._order_worker_thread is not None
        assert controller._order_worker_thread.is_alive()
        assert controller._order_worker_thread is not threading.current_thread()

        base_dt = datetime(2026, 7, 28, 9, 0)
        prices = [100 - i for i in range(6)]
        started = time_module.monotonic()
        for i, p in enumerate(prices):
            controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000))
        elapsed = time_module.monotonic() - started
        assert elapsed < 1.0, f"_on_tickはブロックせず即座に返るはず（実測 {elapsed:.3f}秒）"

        # ワーカースレッドが非同期に処理を終えるまで少し待つ
        for _ in range(50):
            if controller.position_manager.has_position("9432"):
                break
            time_module.sleep(0.05)
        assert controller.position_manager.has_position("9432") is True
        assert "place_market_buy" in call_order
    finally:
        controller.stop()
        assert controller._order_worker_thread is None
    print("test_controller_start_runs_real_worker_thread_that_processes_orders_async: OK")


def test_controller_check_forced_liquidation_does_not_block_caller():
    """
    check_forced_liquidation()はキュー投入のみで即座に返る
    （実際の決済処理はブローカー呼び出しを含み時間がかかりうるため、
    呼び出し元・GUIスレッドをブロックしてはいけない）。
    """
    import time as time_module

    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    started = time_module.monotonic()
    controller.check_forced_liquidation()
    elapsed = time_module.monotonic() - started
    assert elapsed < 0.1, f"check_forced_liquidation()は即座に返るはず（実測 {elapsed:.3f}秒）"

    # ワーカースレッドを起動していないため、投入したタスクはキューに残ったままのはず
    assert controller._order_queue.qsize() >= 1
    print("test_controller_check_forced_liquidation_does_not_block_caller: OK")


def test_controller_pending_symbols_prevents_duplicate_queueing():
    """
    同一銘柄について、先に投入したタスクが処理される前に次のバーが確定しても、
    _pending_symbolsにより重複してキューへ投入されないことを確認する。
    """
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    controller = TradingController(broker=broker, settings=_make_settings(), watchlist=[WatchlistEntry(symbol="9432", name="NTT")])
    controller.risk_manager.start_new_trading_day(datetime(2026, 7, 28).date())

    base_dt = datetime(2026, 7, 28, 9, 0)
    # ワーカースレッドを起動していない（＝キューが誰にも処理されない）状態で
    # 複数バー分のTickを送っても、同一銘柄は1回しかキューに積まれないはず
    for i, p in enumerate([100, 99, 98, 97]):
        broker.set_current_price("9432", float(p))
        controller._on_tick(PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=i), price=float(p), volume=1000))

    assert controller._order_queue.qsize() == 1
    assert "9432" in controller._pending_symbols
    print("test_controller_pending_symbols_prevents_duplicate_queueing: OK")


def test_controller_start_warms_up_from_db_when_available():
    """
    DBに実際の1分足データがある場合、CSV要約値より優先してそちらでウォームアップし、
    かつCSV由来のフォールバックで上書きされないことを確認する（unittest.mockでDBをモック化）。
    """
    import tempfile
    from unittest.mock import patch
    from backtest.models import Bar

    base_dt = datetime(2026, 7, 27, 9, 0)
    fake_bars = [
        Bar("9432", base_dt + timedelta(minutes=i), 1000 + i, 1001 + i, 999 + i, 1000 + i, 1000)
        for i in range(20)
    ]

    with tempfile.TemporaryDirectory() as td:
        config_path = f"{td}/config.ini"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("[postgresql]\nhost=localhost\nport=5433\ndbname=quants\nuser=postgres\npassword=dummy\n")

        broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
        settings = _make_settings(
            indicators={"sma3": {"type": "sma", "period": 3}},
            bar_interval_minutes=1,
        )
        # CSV要約値は実データと全く異なる値にしておき、上書きされていないことを確認する
        watchlist = [WatchlistEntry(symbol="9432", name="NTT", prev_close=99999.0)]
        controller = TradingController(broker=broker, settings=settings, watchlist=watchlist, pg_config_path=config_path)

        with patch("storage.pg_load_bars", return_value=fake_bars) as mock_load:
            controller._warmup_indicators()
            mock_load.assert_called_once()
            assert mock_load.call_args.kwargs["codes"] == ["9432"]

        snap = controller.signal_engine.get_context("9432").snapshot()
        # 実データ由来の値（直近3本の単純平均）になっているはず。CSVの99999は使われていない
        assert snap["sma3"]["sma3"] == (1017 + 1018 + 1019) / 3
    print("test_controller_start_warms_up_from_db_when_available: OK", snap)


if __name__ == "__main__":
    test_controller_constructs_without_ws_url()
    test_controller_tick_triggers_entry_and_emits_signals()
    test_controller_trailing_stop_triggers_exit()
    test_controller_check_forced_liquidation_closes_all_positions()
    test_controller_persists_to_db_when_db_path_given()
    test_controller_db_access_from_different_thread_does_not_raise()
    test_controller_ignores_ticks_for_symbols_not_in_watchlist()
    test_controller_start_unregisters_before_registering()
    test_controller_start_warms_up_indicators_from_watchlist_entry_summary()
    test_controller_writes_1min_bars_to_postgres_on_bar_completion()
    test_controller_start_and_close_manage_postgres_connection()
    test_controller_start_runs_real_worker_thread_that_processes_orders_async()
    test_controller_check_forced_liquidation_does_not_block_caller()
    test_controller_pending_symbols_prevents_duplicate_queueing()
    test_controller_start_warms_up_from_db_when_available()
    print("\nすべてのテストに成功しました。")
