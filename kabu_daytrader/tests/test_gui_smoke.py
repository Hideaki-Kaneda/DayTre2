"""
gui/ パッケージのスモークテスト。実際の画面描画はできないため
（オフスクリーン環境）、ウィジェットの生成・主要メソッド呼び出しが
例外なく完走することを確認する。

QT_QPA_PLATFORM=offscreen python tests/test_gui_smoke.py で実行する。
"""

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from config.settings import ConfigManager  # noqa: E402
from gui import MainWindow  # noqa: E402
from signals import WatchlistEntry  # noqa: E402
from trading.models import OrderReason, OrderResult, OrderSide, OrderStatus  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

# QMessageBox.warning/criticalはモーダルダイアログでexec()し、
# 実際のユーザー操作がないヘッドレステストでは永久にブロックしてしまうため、
# テスト実行中はダイアログを開かずログだけ出すダミーに差し替える。
_message_box_patcher_warning = patch.object(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
_message_box_patcher_critical = patch.object(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
_message_box_patcher_warning.start()
_message_box_patcher_critical.start()


def test_main_window_constructs():
    window = MainWindow()
    assert window.windowTitle() == "Kabu Auto Day-Trader"
    assert window.tabs.count() == 7
    print("test_main_window_constructs: OK")


def test_settings_tab_round_trip():
    window = MainWindow()
    original = window.settings_tab.collect_settings()
    assert original["watchlist_size"] == 30
    assert "entry_rule" in original and "exit_rule" in original
    assert original["entry_start_time"] == "09:12"  # config/default_config.jsonの既定値

    window.settings_tab.watchlist_size.setValue(10)
    from PySide6.QtCore import QTime
    window.settings_tab.entry_start_time.setTime(QTime(9, 20))
    updated = window.settings_tab.collect_settings()
    assert updated["watchlist_size"] == 10
    assert updated["entry_start_time"] == "09:20"
    print("test_settings_tab_round_trip: OK")


def test_settings_tab_invalid_json_does_not_crash():
    window = MainWindow()
    window.settings_tab.rule_json_edit.setPlainText("{not valid json")
    # _on_save_clicked内でエラーダイアログを出すだけで例外を投げないことを確認
    window.settings_tab._on_save_clicked()
    print("test_settings_tab_invalid_json_does_not_crash: OK")


def test_monitor_tab_updates():
    window = MainWindow()
    window.monitor_tab.set_watchlist([
        WatchlistEntry(symbol="9432", name="NTT"),
        WatchlistEntry(symbol="7203", name="トヨタ自動車"),
    ])
    window.monitor_tab.update_symbol(
        "9432",
        snapshot={"rsi14": {"rsi14": 28.5}, "vwap": {"deviation_pct": -1.2}, "bb": {"position": "BELOW_LOWER"}},
        price=150.5,
        has_position=True,
    )
    window.monitor_tab.set_signal_flag("9432", "ENTRY")
    assert window.monitor_tab.table.rowCount() == 2
    assert window.monitor_tab.table.item(0, 2).text() == "150.5"
    print("test_monitor_tab_updates: OK")


def test_orders_tab_and_notify_order_event_switches_tab():
    window = MainWindow()
    window.tabs.setCurrentWidget(window.settings_tab)
    assert window.tabs.currentWidget() is window.settings_tab

    result = OrderResult(
        order_id="ORD-1", symbol="9432", side=OrderSide.BUY, qty=100,
        status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
        requested_at=datetime.now(), filled_at=datetime.now(), filled_price=150.0,
    )
    window.notify_order_event(result)

    assert window.tabs.currentWidget() is window.orders_tab, "発注イベントで注文/決済タブへ自動切替されるはず"
    assert window.orders_tab.order_table.rowCount() == 1
    print("test_orders_tab_and_notify_order_event_switches_tab: OK")


def test_pnl_tab_updates():
    from trading.risk_manager import DailyPnlState
    import datetime as dt

    window = MainWindow()
    state = DailyPnlState(trade_date=dt.date.today(), realized_pnl=5000, win_count=3, lose_count=1)
    window.pnl_tab.update_daily_state(state)
    assert "5,000" in window.pnl_tab.realized_pnl_label.text() or "+5,000" in window.pnl_tab.realized_pnl_label.text()
    print("test_pnl_tab_updates: OK", window.pnl_tab.realized_pnl_label.text())


def test_log_tab_receives_logging_records():
    import logging

    window = MainWindow()
    logger = logging.getLogger("kabu_daytrader.test")
    logger.warning("テスト用の警告メッセージ")
    app.processEvents()
    assert "テスト用の警告メッセージ" in window.log_tab.text_edit.toPlainText()
    print("test_log_tab_receives_logging_records: OK")


def test_backtest_tab_end_to_end_with_real_engine():
    window = MainWindow()

    with tempfile.TemporaryDirectory() as td:
        csv_path = Path(td) / "bars.csv"
        base_dt = datetime(2026, 7, 28, 9, 0)
        decline = [1000 - 20 * i for i in range(15)]
        bottom = decline[-1]
        recovery = [bottom + 20 * i for i in range(1, 40)]
        prices = decline + recovery

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"])
            for i, p in enumerate(prices):
                ts = base_dt + timedelta(minutes=i)
                writer.writerow([ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"), "94320", p, p, p, p, 1000, p * 1000])

        # 設定タブのentry_rule/exit_ruleをこのシナリオに合わせて簡略化
        window.settings_tab.rule_json_edit.setPlainText(
            '{"indicators": {"rsi14": {"type": "rsi", "period": 14}},'
            ' "entry_rule": {"operator": "AND", "conditions": '
            '[{"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30}]},'
            ' "exit_rule": {"operator": "AND", "conditions": '
            '[{"indicator": "rsi14", "field": "rsi14", "op": ">", "value": 70}]}}'
        )

        window.backtest_tab.csv_path_edit.setText(str(csv_path))
        window.backtest_tab._on_run_clicked()

        worker = window.backtest_tab._worker
        assert worker is not None
        worker.wait(10_000)  # 最大10秒待機
        app.processEvents()

        assert window.backtest_tab.status_label.text() == "完了"
        assert window.backtest_tab.result_table.rowCount() == 8
        row_labels = [window.backtest_tab.result_table.item(r, 0).text() for r in range(8)]
        assert "実行完了日時" in row_labels
        assert "対象データ期間" in row_labels
        assert window.backtest_tab.log_path_label.text().startswith("ログ出力先:")
    print("test_backtest_tab_end_to_end_with_real_engine: OK")


def test_backtest_tab_resamples_to_3min_bars():
    window = MainWindow()

    with tempfile.TemporaryDirectory() as td:
        csv_path = Path(td) / "bars_1min.csv"
        base_dt = datetime(2026, 7, 28, 9, 0)
        decline = [1000 - 20 * i for i in range(15)]
        bottom = decline[-1]
        recovery = [bottom + 20 * i for i in range(1, 40)]
        prices = decline + recovery  # 54本の1分足

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"])
            for i, p in enumerate(prices):
                ts = base_dt + timedelta(minutes=i)
                writer.writerow([ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"), "94320", p, p, p, p, 1000, p * 1000])

        window.settings_tab.rule_json_edit.setPlainText(
            '{"indicators": {"rsi14": {"type": "rsi", "period": 14}},'
            ' "entry_rule": {"operator": "AND", "conditions": '
            '[{"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30}]},'
            ' "exit_rule": {"operator": "AND", "conditions": '
            '[{"indicator": "rsi14", "field": "rsi14", "op": ">", "value": 70}]}}'
        )

        window.backtest_tab.csv_path_edit.setText(str(csv_path))
        window.backtest_tab.bar_interval_combo.setCurrentIndex(1)  # "3分足に変換"
        window.backtest_tab._on_run_clicked()

        worker = window.backtest_tab._worker
        assert worker is not None
        worker.wait(10_000)
        app.processEvents()

        assert window.backtest_tab.status_label.text() == "完了"
        # 54本の1分足 → 3分足なら18本になっているはず（workerに渡されたbarsで直接検証）
        assert len(worker.bars) == 18, len(worker.bars)
    print("test_backtest_tab_resamples_to_3min_bars: OK")


def test_backtest_tab_postgres_source_end_to_end_with_mocked_db():
    from unittest.mock import patch
    from backtest.models import Bar

    window = MainWindow()

    # 設定タブのルールをRSI単純条件に簡略化（前回テストと同様の意図）
    window.settings_tab.rule_json_edit.setPlainText(
        '{"indicators": {"rsi14": {"type": "rsi", "period": 14}},'
        ' "entry_rule": {"operator": "AND", "conditions": '
        '[{"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30}]},'
        ' "exit_rule": {"operator": "AND", "conditions": '
        '[{"indicator": "rsi14", "field": "rsi14", "op": ">", "value": 70}]}}'
    )

    base_dt = datetime(2026, 7, 28, 9, 0)
    decline = [1000 - 20 * i for i in range(15)]
    bottom = decline[-1]
    recovery = [bottom + 20 * i for i in range(1, 40)]
    prices = decline + recovery
    fake_bars = [Bar("9432", base_dt + timedelta(minutes=i), p, p, p, p, 1000) for i, p in enumerate(prices)]

    window.backtest_tab.data_source_combo.setCurrentIndex(1)  # PostgreSQL
    assert window.backtest_tab.data_source_stack.currentIndex() == 1

    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.ini"
        config_path.write_text(
            "[postgresql]\nhost=localhost\nport=5433\ndbname=quants\nuser=postgres\npassword=dummy\n",
            encoding="utf-8",
        )
        window.backtest_tab.pg_config_path_edit.setText(str(config_path))
        window.backtest_tab.pg_code_edit.setText("9432")

        with patch("storage.pg_load_bars", return_value=fake_bars) as mock_load:
            window.backtest_tab._on_run_clicked()
            mock_load.assert_called_once()
            call_kwargs = mock_load.call_args.kwargs
            assert call_kwargs["codes"] == ["9432"]

        worker = window.backtest_tab._worker
        assert worker is not None
        worker.wait(10_000)
        app.processEvents()

        assert window.backtest_tab.status_label.text() == "完了"
    print("test_backtest_tab_postgres_source_end_to_end_with_mocked_db: OK")


def test_backtest_tab_postgres_source_requires_config_path():
    window = MainWindow()
    window.backtest_tab.data_source_combo.setCurrentIndex(1)  # PostgreSQL
    window.backtest_tab.pg_config_path_edit.setText("")  # 未入力
    window.backtest_tab.pg_code_edit.setText("9432")

    # QMessageBox.criticalはモック化済みなので例外なく完走することだけを確認
    window.backtest_tab._on_run_clicked()
    assert window.backtest_tab._worker is None  # 読み込みエラーでワーカーは起動しないはず
    print("test_backtest_tab_postgres_source_requires_config_path: OK")


def test_candle_backtest_tab_end_to_end_with_real_engine():
    window = MainWindow()

    with tempfile.TemporaryDirectory() as td:
        csv_path = Path(td) / "bars_1min.csv"
        base_dt = datetime(2026, 7, 28, 9, 0)
        # 3分足換算で3本連続陽線が成立するよう、1分足レベルで単調増加させる
        prices = [100 + i for i in range(30)]

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"])
            for i, p in enumerate(prices):
                ts = base_dt + timedelta(minutes=i)
                writer.writerow([ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"), "94320", p, p, p, p, 1000, p * 1000])

        window.candle_backtest_tab.csv_path_edit.setText(str(csv_path))
        window.candle_backtest_tab.consecutive_bars_n.setValue(3)
        window.candle_backtest_tab._on_run_clicked()

        worker = window.candle_backtest_tab._worker
        assert worker is not None
        worker.wait(10_000)
        app.processEvents()

        assert window.candle_backtest_tab.status_label.text() == "完了"
        assert window.candle_backtest_tab.result_table.rowCount() == 8
        assert window.candle_backtest_tab.log_path_label.text().startswith("ログ出力先:")
    print("test_candle_backtest_tab_end_to_end_with_real_engine: OK")


if __name__ == "__main__":
    test_main_window_constructs()
    test_settings_tab_round_trip()
    test_settings_tab_invalid_json_does_not_crash()
    test_monitor_tab_updates()
    test_orders_tab_and_notify_order_event_switches_tab()
    test_pnl_tab_updates()
    test_log_tab_receives_logging_records()
    test_backtest_tab_end_to_end_with_real_engine()
    test_backtest_tab_resamples_to_3min_bars()
    test_backtest_tab_postgres_source_end_to_end_with_mocked_db()
    test_backtest_tab_postgres_source_requires_config_path()
    test_candle_backtest_tab_end_to_end_with_real_engine()
    print("\nすべてのテストに成功しました。")
