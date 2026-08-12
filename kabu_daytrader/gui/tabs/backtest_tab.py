"""
バックテストタブ（基本設計書7.6節）。
J-Quants分足CSV、またはPostgreSQL（equities_bars_minuteテーブル。別バッチ
minute_data_import と共有）から分足データを読み込み、実際に
backtest.BacktestEngine を実行して結果（損益推移グラフ・勝率・平均利益/損失・
最大ドローダウン）を表示する。UIをブロックしないよう BacktestWorker(QThread) 上で実行する。
"""

import json
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import pyqtgraph as pg
except ImportError:  # pragma: no cover
    pg = None

from backtest import BacktestConfig, BacktestResult, load_bars_from_csv, resample_bars
from ..workers import BacktestWorker

RESULT_COLUMNS = ["項目", "値"]
BAR_INTERVAL_OPTIONS = [
    ("1分足（そのまま）", 1),
    ("3分足に変換", 3),
    ("5分足に変換", 5),
    ("15分足に変換", 15),
]
DATA_SOURCE_CSV = 0
DATA_SOURCE_POSTGRES = 1


class BacktestTab(QWidget):
    def __init__(self, get_current_rule_settings, parent=None):
        """
        get_current_rule_settings: () -> Dict[str, Any] を返す callable。
        設定タブで編集中の indicators/entry_rule/exit_rule をそのままバックテストにも
        使えるようにする（本番と条件を揃えるため、この画面では別入力にしない）。
        """
        super().__init__(parent)
        self.get_current_rule_settings = get_current_rule_settings
        self._worker: Optional[BacktestWorker] = None

        self.data_source_combo = QComboBox()
        self.data_source_combo.addItems(["CSVファイル", "PostgreSQL（equities_bars_minute）"])
        self.data_source_combo.currentIndexChanged.connect(self._on_data_source_changed)

        # --- CSVファイル入力 ---
        self.csv_path_edit = QLineEdit()
        self.csv_path_edit.setPlaceholderText("J-Quants分足CSVのパス")
        browse_button = QPushButton("参照...")
        browse_button.clicked.connect(self._on_browse_clicked)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.csv_path_edit)
        path_layout.addWidget(browse_button)
        csv_source_widget = QWidget()
        csv_source_widget.setLayout(path_layout)

        # --- PostgreSQL入力 ---
        self.pg_config_path_edit = QLineEdit()
        self.pg_config_path_edit.setPlaceholderText(r"config.iniのパス（例: C:\...\minute_data_import\config.ini）")
        self.pg_code_edit = QLineEdit()
        self.pg_code_edit.setPlaceholderText("銘柄コード（4桁。例: 6613。カンマ区切りで複数可）")
        self.pg_start_edit = QDateTimeEdit()
        self.pg_start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.pg_start_edit.setDateTime(QDateTime.currentDateTime().addDays(-7))
        self.pg_end_edit = QDateTimeEdit()
        self.pg_end_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.pg_end_edit.setDateTime(QDateTime.currentDateTime())

        pg_source_layout = QFormLayout()
        pg_source_layout.addRow("config.iniパス", self.pg_config_path_edit)
        pg_source_layout.addRow("銘柄コード", self.pg_code_edit)
        pg_source_layout.addRow("開始日時", self.pg_start_edit)
        pg_source_layout.addRow("終了日時", self.pg_end_edit)
        pg_source_widget = QWidget()
        pg_source_widget.setLayout(pg_source_layout)

        self.data_source_stack = QStackedWidget()
        self.data_source_stack.addWidget(csv_source_widget)   # index 0 = CSV
        self.data_source_stack.addWidget(pg_source_widget)    # index 1 = PostgreSQL

        self.bar_interval_combo = QComboBox()
        for label, _minutes in BAR_INTERVAL_OPTIONS:
            self.bar_interval_combo.addItem(label)
        self.bar_interval_combo.setToolTip(
            "CSVは1分足を想定。3分足・5分足等でシミュレーションしたい場合、\n"
            "ここで選ぶと同一区間内の1分足をOHLCVとして集約してから実行する\n"
            "（始値=区間最初、高値=区間最大、安値=区間最小、終値=区間最後、出来高=合計）。"
        )

        self.initial_buying_power = QSpinBox()
        self.initial_buying_power.setRange(10_000, 1_000_000_000)
        self.initial_buying_power.setValue(1_000_000)
        self.initial_buying_power.setSingleStep(10_000)

        self.shares_per_symbol = QSpinBox()
        self.shares_per_symbol.setRange(100, 1_000_000)
        self.shares_per_symbol.setValue(100)
        self.shares_per_symbol.setSingleStep(100)

        self.trailing_multiplier = QDoubleSpinBox()
        self.trailing_multiplier.setRange(0.5, 10.0)
        self.trailing_multiplier.setDecimals(2)
        self.trailing_multiplier.setValue(2.0)
        self.trailing_multiplier.setToolTip("トレール決済の基準幅 = AR × この倍率（既定2.0）。設定タブと同じ考え方。")

        self.reentry_cooldown_bars = QSpinBox()
        self.reentry_cooldown_bars.setRange(0, 30)
        self.reentry_cooldown_bars.setValue(3)
        self.reentry_cooldown_bars.setToolTip("決済後、同一銘柄への再エントリーを禁止するバー本数（既定3本）。設定タブと同じ考え方。")

        self.daily_profit_target = QSpinBox()
        self.daily_profit_target.setRange(0, 10_000_000)
        self.daily_profit_target.setValue(20000)

        self.daily_max_loss = QSpinBox()
        self.daily_max_loss.setRange(0, 10_000_000)
        self.daily_max_loss.setValue(20000)

        self.force_close_time = QTimeEdit()
        self.force_close_time.setTime(self.force_close_time.time().fromString("15:00", "HH:mm"))

        self.entry_start_time = QTimeEdit()
        self.entry_start_time.setTime(self.entry_start_time.time().fromString("09:00", "HH:mm"))

        form_group = QGroupBox("バックテスト条件")
        form = QFormLayout()
        form.addRow("データソース", self.data_source_combo)
        form.addRow(self.data_source_stack)
        form.addRow("シミュレーション足間隔", self.bar_interval_combo)
        form.addRow("初期資金", self.initial_buying_power)
        form.addRow("1銘柄あたり株数", self.shares_per_symbol)
        form.addRow("トレール倍率（AR×倍率）", self.trailing_multiplier)
        form.addRow("再エントリー禁止バー数", self.reentry_cooldown_bars)
        form.addRow("日次利益上限", self.daily_profit_target)
        form.addRow("日次最大損失", self.daily_max_loss)
        form.addRow("新規エントリー開始時刻", self.entry_start_time)
        form.addRow("強制引け決済時刻", self.force_close_time)
        form_group.setLayout(form)

        self.run_button = QPushButton("バックテスト実行")
        self.run_button.clicked.connect(self._on_run_clicked)
        self.status_label = QLabel("")
        self.log_path_label = QLabel("")

        self.result_table = QTableWidget(0, len(RESULT_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLUMNS)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        if pg is not None:
            self.equity_plot = pg.PlotWidget(
                title="損益推移（累積実現損益）",
                axisItems={"bottom": pg.DateAxisItem()},
            )
            self.equity_plot.showGrid(x=True, y=True)
            self.equity_plot.setLabel("left", "累積実現損益（円）")
            self.equity_plot.setLabel("bottom", "日時")
        else:  # pragma: no cover
            self.equity_plot = QLabel("pyqtgraphが利用できないため、グラフは表示できません")

        layout = QVBoxLayout()
        layout.addWidget(form_group)
        layout.addWidget(self.run_button)
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_path_label)
        layout.addWidget(self.result_table)
        layout.addWidget(self.equity_plot)
        self.setLayout(layout)

    def _on_browse_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "分足CSVを選択", "", "CSV Files (*.csv)")
        if path:
            self.csv_path_edit.setText(path)

    def _on_data_source_changed(self, index: int) -> None:
        self.data_source_stack.setCurrentIndex(index)

    def _load_bars(self):
        """
        選択中のデータソース（CSV / PostgreSQL）に応じて分足データを読み込む。
        読み込みに失敗した場合は例外を送出する（呼び出し元でメッセージボックス表示すること）。
        """
        if self.data_source_combo.currentIndex() == DATA_SOURCE_CSV:
            csv_path = self.csv_path_edit.text().strip()
            if not csv_path or not Path(csv_path).exists():
                raise ValueError("有効なCSVファイルを指定してください")
            return load_bars_from_csv(csv_path)

        # PostgreSQL
        from storage import pg_load_bars

        config_path = self.pg_config_path_edit.text().strip()
        if not config_path or not Path(config_path).exists():
            raise ValueError("有効なconfig.iniのパスを指定してください")

        codes = [c.strip() for c in self.pg_code_edit.text().split(",") if c.strip()]
        if not codes:
            raise ValueError("銘柄コードを指定してください（4桁、カンマ区切りで複数可）")

        start = self.pg_start_edit.dateTime().toPython()
        end = self.pg_end_edit.dateTime().toPython()
        if start >= end:
            raise ValueError("開始日時は終了日時より前にしてください")

        bars = pg_load_bars(config_path, codes=codes, start=start, end=end)
        if not bars:
            raise ValueError(
                "指定した条件でデータが取得できませんでした"
                "（銘柄コード・期間・config.iniの接続情報を確認してください）"
            )
        return bars

    def _on_run_clicked(self) -> None:
        try:
            rule_settings = self.get_current_rule_settings()
            bars = self._load_bars()
            interval_minutes = BAR_INTERVAL_OPTIONS[self.bar_interval_combo.currentIndex()][1]
            if interval_minutes > 1:
                bars = resample_bars(bars, interval_minutes)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "読み込みエラー", str(e))
            return

        qtime = self.force_close_time.time()
        entry_qtime = self.entry_start_time.time()
        config = BacktestConfig(
            indicator_config=rule_settings.get("indicators", {}),
            entry_rule=rule_settings.get("entry_rule", {}),
            exit_rule=rule_settings.get("exit_rule", {}),
            initial_buying_power=float(self.initial_buying_power.value()),
            shares_per_symbol=self.shares_per_symbol.value(),
            trailing_multiplier=self.trailing_multiplier.value(),
            reentry_cooldown_bars=self.reentry_cooldown_bars.value(),
            daily_profit_target=float(self.daily_profit_target.value()),
            daily_max_loss=float(self.daily_max_loss.value()),
            force_close_time=dtime(qtime.hour(), qtime.minute()),
            entry_start_time=dtime(entry_qtime.hour(), entry_qtime.minute()),
        )

        self.run_button.setEnabled(False)
        self.status_label.setText(f"実行中...（{len(bars)}件の分足データ）")

        self._worker = BacktestWorker(config, bars)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.start()

    def _on_finished_ok(self, result: BacktestResult) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("完了")
        self._render_result(result)
        self._save_backtest_run(result)
        self._save_backtest_log(result)

    def _save_backtest_run(self, result: BacktestResult) -> None:
        try:
            from storage import BacktestRunRepository, connect

            conn = connect()
            repo = BacktestRunRepository(conn)
            period_from = result.equity_curve[0].timestamp if result.equity_curve else datetime.now()
            period_to = result.equity_curve[-1].timestamp if result.equity_curve else datetime.now()
            repo.save(
                result, period_from=period_from, period_to=period_to,
                params={
                    "shares_per_symbol": self.shares_per_symbol.value(),
                    "trailing_multiplier": self.trailing_multiplier.value(),
                    "daily_profit_target": self.daily_profit_target.value(),
                    "daily_max_loss": self.daily_max_loss.value(),
                },
            )
        except Exception as e:  # noqa: BLE001
            # バックテスト結果の保存に失敗しても、画面表示自体は済んでいるので
            # 取引を止める必要はない。ログにのみ記録する。
            import logging
            logging.getLogger(__name__).warning("バックテスト結果のDB保存に失敗しました: %s", e)

    def _save_backtest_log(self, result: BacktestResult) -> None:
        """
        エントリー・決済（利確/損切/強制決済等）の詳細ログをCSVファイルへ出力する。
        logs/backtest_log_<実行日時>.csv に保存し、ステータス欄に保存先を表示する。
        """
        try:
            from backtest import write_backtest_log_csv

            logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
            filename = f"backtest_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            log_path = logs_dir / filename
            write_backtest_log_csv(result.log_entries, log_path)
            self.log_path_label.setText(f"ログ出力先: {log_path}")
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("バックテストログの出力に失敗しました: %s", e)
            self.log_path_label.setText("ログ出力に失敗しました")

    def _on_finished_error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("エラー")
        QMessageBox.critical(self, "バックテスト実行エラー", message)

    def _render_result(self, result: BacktestResult) -> None:
        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if result.equity_curve:
            period_from = result.equity_curve[0].timestamp.strftime("%Y-%m-%d %H:%M")
            period_to = result.equity_curve[-1].timestamp.strftime("%Y-%m-%d %H:%M")
            period_text = f"{period_from} 〜 {period_to}"
        else:
            period_text = "-"

        rows = [
            ("実行完了日時", completed_at),
            ("対象データ期間", period_text),
            ("取引回数", str(result.trade_count)),
            ("合計損益", f"¥{result.total_pnl:+,.0f}"),
            ("勝率", f"{result.win_rate * 100:.1f}%" if result.win_rate is not None else "-"),
            ("平均利益", f"¥{result.average_win:+,.0f}" if result.average_win is not None else "-"),
            ("平均損失", f"¥{result.average_loss:+,.0f}" if result.average_loss is not None else "-"),
            ("最大ドローダウン", f"¥{result.max_drawdown:,.0f}"),
        ]
        self.result_table.setRowCount(len(rows))
        for row, (label, value) in enumerate(rows):
            self.result_table.setItem(row, 0, QTableWidgetItem(label))
            self.result_table.setItem(row, 1, QTableWidgetItem(value))

        if pg is not None and result.equity_curve:
            xs = [p.timestamp.timestamp() for p in result.equity_curve]
            ys = [p.cumulative_pnl for p in result.equity_curve]
            self.equity_plot.clear()
            self.equity_plot.plot(xs, ys, pen=pg.mkPen(width=2))
