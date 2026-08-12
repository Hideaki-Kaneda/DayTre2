"""
MainWindow：基本設計書7.1節の全体レイアウトに対応。

設定／監視状況／注文・決済／損益／バックテスト／ログの6タブ構成に加え、
上部に監視開始/停止コントロールを持つ。

「監視開始」を押すと、監視状況タブで読み込んだ銘柄リストと設定タブの
シグナルルールをもとに TradingController を生成し、kabuステーションAPIへの
認証・銘柄登録・PUSH接続を StartupWorker(QThread) 上で実行する。
以降はTradingControllerが発行するQt Signal経由で各タブが更新され、
発注・約定が発生すると notify_order_event() が注文・決済タブへ自動的に
切り替える（基本設計書4.7節の要件）。
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api import AccountConfig, RestClient
from config.settings import ConfigManager
from trading import OrderResult

from .qt_log_handler import QtLogHandler
from .tabs import BacktestTab, CandleBacktestTab, LogTab, MonitorTab, OrdersTab, PnlTab, SettingsTab
from .trading_controller import TradingController
from .workers import StartupWorker

ENVIRONMENTS = {
    "本番 (18080)": {"rest_port": 18080, "ws_port": 18080},
    "検証用 (18081)": {"rest_port": 18081, "ws_port": 18081},
}


class MainWindow(QMainWindow):
    def __init__(self, config_manager: ConfigManager | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kabu Auto Day-Trader")
        self.resize(1100, 750)

        self.config_manager = config_manager or ConfigManager()
        settings = self.config_manager.data

        self.settings_tab = SettingsTab(settings)
        self.monitor_tab = MonitorTab()
        self.orders_tab = OrdersTab()
        self.pnl_tab = PnlTab(
            daily_profit_target=settings.get("daily_profit_target", 20000),
            daily_max_loss=settings.get("daily_max_loss", 20000),
        )
        self.backtest_tab = BacktestTab(get_current_rule_settings=self.settings_tab.collect_settings)
        self.candle_backtest_tab = CandleBacktestTab()
        self.log_tab = LogTab()

        self.tabs = QTabWidget()
        self.tabs.addTab(self.settings_tab, "設定")
        self.tabs.addTab(self.monitor_tab, "監視状況")
        self.tabs.addTab(self.orders_tab, "注文・決済")
        self.tabs.addTab(self.pnl_tab, "損益")
        self.tabs.addTab(self.backtest_tab, "バックテスト")
        self.tabs.addTab(self.candle_backtest_tab, "新戦略バックテスト")
        self.tabs.addTab(self.log_tab, "ログ")

        self._build_control_bar()

        central = QWidget()
        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(self.control_bar)
        central_layout.addWidget(self.tabs)
        central.setLayout(central_layout)
        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.connection_status_label = QLabel("PUSH接続: 未接続")
        self.status_bar.addPermanentWidget(self.connection_status_label)
        self.setStatusBar(self.status_bar)

        self.settings_tab.settings_saved.connect(self._on_settings_saved)

        self.controller: TradingController | None = None
        self._startup_worker: StartupWorker | None = None
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "kabu_daytrader.db"

        self._forced_liquidation_timer = QTimer(self)
        self._forced_liquidation_timer.setInterval(10_000)  # 10秒ごとに強制決済条件をチェック
        self._forced_liquidation_timer.timeout.connect(self._on_forced_liquidation_timer)

        self._setup_logging()

    # ------------------------------------------------------------------
    # 上部の監視開始/停止コントロール
    # ------------------------------------------------------------------
    def _build_control_bar(self) -> None:
        self.control_bar = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)

        self.environment_combo = QComboBox()
        self.environment_combo.addItems(list(ENVIRONMENTS.keys()))

        self.start_button = QPushButton("監視開始")
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        layout.addWidget(QLabel("接続環境:"))
        layout.addWidget(self.environment_combo)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addStretch(1)
        self.control_bar.setLayout(layout)

    # ------------------------------------------------------------------
    # ログ配線
    # ------------------------------------------------------------------
    def _setup_logging(self) -> None:
        self._qt_log_handler = QtLogHandler()
        self._qt_log_handler.setLevel(logging.INFO)
        self._qt_log_handler.log_emitted.connect(self.log_tab.append_log)
        logging.getLogger().addHandler(self._qt_log_handler)

    # ------------------------------------------------------------------
    # 設定タブ
    # ------------------------------------------------------------------
    def _on_settings_saved(self, settings: dict) -> None:
        self.config_manager.data.clear()
        self.config_manager.data.update(settings)
        self.config_manager.save()
        self.pnl_tab.daily_profit_target = settings.get("daily_profit_target", 20000)
        self.pnl_tab.daily_max_loss = settings.get("daily_max_loss", 20000)
        logging.getLogger(__name__).info("設定を保存しました")

    # ------------------------------------------------------------------
    # 監視開始・停止
    # ------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        watchlist = self.monitor_tab.get_watchlist()
        if not watchlist:
            QMessageBox.warning(self, "監視銘柄未設定", "監視状況タブから銘柄リストCSVを読み込んでください")
            return

        password, ok = QInputDialog.getText(
            self, "APIパスワード",
            "kabuステーションAPIパスワードを入力してください\n"
            "（環境変数 KABU_API_PASSWORD が設定されていればそれを初期値にします）",
            QLineEdit.EchoMode.Password,
            os.environ.get("KABU_API_PASSWORD", ""),
        )
        if not ok or not password:
            return

        try:
            settings = self.settings_tab.collect_settings()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "設定エラー", f"シグナルルールのJSONが不正です:\n{e}")
            return

        env = ENVIRONMENTS[self.environment_combo.currentText()]
        rest_base_url = f"http://localhost:{env['rest_port']}/kabusapi"
        ws_url = f"ws://localhost:{env['ws_port']}/kabusapi/websocket"

        broker = RestClient(base_url=rest_base_url, api_password=password, account_config=AccountConfig())
        self.controller = TradingController(
            broker=broker, settings=settings, watchlist=watchlist, ws_url=ws_url,
            db_path=self._db_path, pg_config_path=settings.get("pg_config_path") or None,
        )
        self._connect_controller_signals(self.controller)

        self.start_button.setEnabled(False)
        self._startup_worker = StartupWorker(self.controller)
        self._startup_worker.finished_ok.connect(self._on_startup_ok)
        self._startup_worker.finished_error.connect(self._on_startup_error)
        self._startup_worker.start()

    def _on_startup_ok(self) -> None:
        self.stop_button.setEnabled(True)
        self._forced_liquidation_timer.start()
        logging.getLogger(__name__).info("監視を開始しました")

    def _on_startup_error(self, message: str) -> None:
        self.start_button.setEnabled(True)
        QMessageBox.critical(self, "接続エラー", f"監視の開始に失敗しました:\n{message}")

    def _on_stop_clicked(self) -> None:
        if self.controller is not None:
            self.controller.stop()
            self.controller.close()
        self._forced_liquidation_timer.stop()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_forced_liquidation_timer(self) -> None:
        if self.controller is not None:
            self.controller.check_forced_liquidation()

    def _connect_controller_signals(self, controller: TradingController) -> None:
        controller.tick_updated.connect(self._on_tick_updated)
        controller.signal_flagged.connect(self.monitor_tab.set_signal_flag)
        controller.order_event.connect(self.notify_order_event)
        controller.connection_status_changed.connect(self.notify_connection_status)
        controller.pnl_updated.connect(self.pnl_tab.update_daily_state)

    def _on_tick_updated(self, symbol: str, snapshot: dict, price: float, has_position: bool) -> None:
        self.monitor_tab.update_symbol(symbol, snapshot, price, has_position)
        if self.controller is not None:
            self.orders_tab.refresh_positions(
                self.controller.position_manager.get_all_positions(),
                self.controller._last_prices,
            )

    # ------------------------------------------------------------------
    # 外部（本番監視ループ）から呼び出す想定の更新メソッド
    # ------------------------------------------------------------------
    def notify_order_event(self, result: OrderResult) -> None:
        """
        発注・約定イベントを受け取り、注文・決済タブへ表示を追加した上で
        そのタブへ自動的に切り替える（基本設計書4.7節の要件）。
        """
        self.orders_tab.add_order_event(result)
        self.tabs.setCurrentWidget(self.orders_tab)

    def notify_connection_status(self, connected: bool) -> None:
        self.connection_status_label.setText("PUSH接続: 接続中" if connected else "PUSH接続: 切断")
