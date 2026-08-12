"""
新戦略（ローソク足連続パターン）専用のバックテストタブ。

既存のBacktestTab（RSI等ベース戦略）とは意図的に別クラスとして分離している
（candle_strategyパッケージ自体が既存のtrading/signalsと独立しているため）。
CSV/PostgreSQLからの分足データ読込は既存のbacktest.data_loaderを共通基盤として
再利用するが、戦略の判定・発注ロジックはcandle_strategy.CandleBacktestEngineのみを使う。

【重要】このタブはバックテストのみを対象とする。実際のkabuステーションAPIでの
信用取引発注（新規売り＝空売り等）はCLAUDE.mdに記載の通りまだ未実装・未検証のため、
本番監視への統合は行っていない。
"""

import logging
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Optional

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

from backtest import load_bars_from_csv, resample_bars
from candle_strategy import CandleBacktestConfig, CandleBacktestResult, write_candle_log_csv
from ..workers import CandleBacktestWorker

RESULT_COLUMNS = ["項目", "値"]
BAR_INTERVAL_OPTIONS = [
    ("1分足（そのまま）", 1),
    ("3分足に変換（既定）", 3),
    ("5分足に変換", 5),
    ("15分足に変換", 15),
]
DATA_SOURCE_CSV = 0
DATA_SOURCE_POSTGRES = 1


class CandleBacktestTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[CandleBacktestWorker] = None

        note = QLabel(
            "新戦略（N本連続陽線／陰線でエントリー、信用取引での空売り対応）専用のバックテストです。\n"
            "既存戦略（設定タブのRSI等）とは完全に独立して動作します。\n"
            "※ライブ監視への統合は未実装（信用注文のkabuステーションAPI仕様が未検証のため）。"
        )
        note.setWordWrap(True)

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
        self.data_source_stack.addWidget(csv_source_widget)
        self.data_source_stack.addWidget(pg_source_widget)

        self.bar_interval_combo = QComboBox()
        for label, _minutes in BAR_INTERVAL_OPTIONS:
            self.bar_interval_combo.addItem(label)
        self.bar_interval_combo.setCurrentIndex(1)  # 既定3分足
        self.bar_interval_combo.setToolTip(
            "CSVは1分足を想定。同一区間内の1分足をOHLCVとして集約してからシミュレーションする。"
        )

        # --- 戦略パラメータ（ユーザー確定仕様の既定値） ---
        self.consecutive_bars_n = QSpinBox()
        self.consecutive_bars_n.setRange(1, 20)
        self.consecutive_bars_n.setValue(3)
        self.consecutive_bars_n.setToolTip("エントリー条件：この本数だけ連続で同色（陽線/陰線）のバーが確定したらエントリー。")

        self.reverse_bars_m = QSpinBox()
        self.reverse_bars_m.setRange(1, 20)
        self.reverse_bars_m.setValue(2)
        self.reverse_bars_m.setToolTip("決済条件（第3優先）：エントリー後、逆色バーが通算でこの本数に達したら決済。")

        self.take_profit_pct = QDoubleSpinBox()
        self.take_profit_pct.setRange(0.1, 50.0)
        self.take_profit_pct.setDecimals(2)
        self.take_profit_pct.setSuffix(" %")
        self.take_profit_pct.setValue(3.5)
        self.take_profit_pct.setToolTip("決済条件（第2優先）：建値からこの割合(%)以上有利に動いたら利確。")

        self.stop_loss_pct = QDoubleSpinBox()
        self.stop_loss_pct.setRange(0.1, 50.0)
        self.stop_loss_pct.setDecimals(2)
        self.stop_loss_pct.setSuffix(" %")
        self.stop_loss_pct.setValue(1.5)
        self.stop_loss_pct.setToolTip("決済条件（第1優先＝最優先）：建値からこの割合(%)以上不利に動いたら損切。")

        self.initial_buying_power = QSpinBox()
        self.initial_buying_power.setRange(10_000, 1_000_000_000)
        self.initial_buying_power.setValue(1_000_000)
        self.initial_buying_power.setSingleStep(10_000)

        self.shares_per_symbol = QSpinBox()
        self.shares_per_symbol.setRange(100, 1_000_000)
        self.shares_per_symbol.setValue(100)
        self.shares_per_symbol.setSingleStep(100)

        self.force_close_time = QTimeEdit()
        self.force_close_time.setTime(self.force_close_time.time().fromString("15:00", "HH:mm"))

        form_group = QGroupBox("バックテスト条件（新戦略：ローソク足連続パターン）")
        form = QFormLayout()
        form.addRow("データソース", self.data_source_combo)
        form.addRow(self.data_source_stack)
        form.addRow("シミュレーション足間隔", self.bar_interval_combo)
        form.addRow("連続本数 N（エントリー条件）", self.consecutive_bars_n)
        form.addRow("逆足本数 M（決済・第3優先）", self.reverse_bars_m)
        form.addRow("損切ライン（決済・第1優先）", self.stop_loss_pct)
        form.addRow("利確ライン（決済・第2優先）", self.take_profit_pct)
        form.addRow("初期資金", self.initial_buying_power)
        form.addRow("1銘柄あたり株数", self.shares_per_symbol)
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
                title="損益推移（累積実現損益）", axisItems={"bottom": pg.DateAxisItem()},
            )
            self.equity_plot.showGrid(x=True, y=True)
            self.equity_plot.setLabel("left", "累積実現損益（円）")
            self.equity_plot.setLabel("bottom", "日時")
        else:  # pragma: no cover
            self.equity_plot = QLabel("pyqtgraphが利用できないため、グラフは表示できません")

        layout = QVBoxLayout()
        layout.addWidget(note)
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
        if self.data_source_combo.currentIndex() == DATA_SOURCE_CSV:
            csv_path = self.csv_path_edit.text().strip()
            if not csv_path or not Path(csv_path).exists():
                raise ValueError("有効なCSVファイルを指定してください")
            return load_bars_from_csv(csv_path)

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
            bars = self._load_bars()
            interval_minutes = BAR_INTERVAL_OPTIONS[self.bar_interval_combo.currentIndex()][1]
            if interval_minutes > 1:
                bars = resample_bars(bars, interval_minutes)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "読み込みエラー", str(e))
            return

        qtime = self.force_close_time.time()
        config = CandleBacktestConfig(
            initial_buying_power=float(self.initial_buying_power.value()),
            shares_per_symbol=self.shares_per_symbol.value(),
            consecutive_bars_n=self.consecutive_bars_n.value(),
            reverse_bars_m=self.reverse_bars_m.value(),
            take_profit_pct=self.take_profit_pct.value() / 100.0,
            stop_loss_pct=self.stop_loss_pct.value() / 100.0,
            force_close_time=dtime(qtime.hour(), qtime.minute()),
        )

        self.run_button.setEnabled(False)
        self.status_label.setText(f"実行中...（{len(bars)}件の分足データ）")

        self._worker = CandleBacktestWorker(config, bars)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.start()

    def _on_finished_ok(self, result: CandleBacktestResult) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("完了")
        self._render_result(result)
        self._save_log(result)

    def _save_log(self, result: CandleBacktestResult) -> None:
        try:
            logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
            filename = f"candle_backtest_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            log_path = logs_dir / filename
            write_candle_log_csv(result.log_entries, log_path)
            self.log_path_label.setText(f"ログ出力先: {log_path}")
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("バックテストログの出力に失敗しました: %s", e)
            self.log_path_label.setText("ログ出力に失敗しました")

    def _on_finished_error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("エラー")
        QMessageBox.critical(self, "バックテスト実行エラー", message)

    def _render_result(self, result: CandleBacktestResult) -> None:
        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if result.equity_curve:
            period_from = result.equity_curve[0].timestamp.strftime("%Y-%m-%d %H:%M")
            period_to = result.equity_curve[-1].timestamp.strftime("%Y-%m-%d %H:%M")
            period_text = f"{period_from} 〜 {period_to}"
        else:
            period_text = "-"

        long_trades = [t for t in result.trades if t.direction.value == "LONG"]
        short_trades = [t for t in result.trades if t.direction.value == "SHORT"]

        rows = [
            ("実行完了日時", completed_at),
            ("対象データ期間", period_text),
            ("取引回数（買い建て/売り建て）", f"{result.trade_count}（{len(long_trades)}/{len(short_trades)}）"),
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
