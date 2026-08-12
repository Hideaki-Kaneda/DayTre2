"""
監視状況タブ（基本設計書7.3節）。

監視銘柄一覧（現在値・主要指標値・シグナル状態・ポジション有無）を表示する。
実データはPushWorker等から update_symbol() 経由で流し込む想定。

銘柄リストCSVフォーマット（1行目はヘッダー、列名で認識するため順序は自由）:
    銘柄コード,銘柄名,前日終値,前日始値,前日高値,前日安値,前日出来高,前日RSI14,前日BB上限,前日BBミドル,前日BB下限

「銘柄コード」列のみ必須。それ以外の列は省略可能（省略した項目はNoneのまま扱われ、
指標のウォームアップ精度が下がるだけで動作は継続する）。
前日RSI14・前日BB上限/ミドル/下限は、設定タブのindicatorsで使っている
RSI・ボリンジャーバンドの期間設定（既定: RSI period=14, BB period=20, num_std=2）と
揃えて計算しておくこと（期間が異なると再現精度が落ちる）。
"""

import csv
from typing import Any, Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from signals import WatchlistEntry

COLUMNS = ["銘柄コード", "銘柄名", "現在値", "RSI", "VWAP乖離%", "BB位置", "シグナル", "ポジション"]

# CSVヘッダー名 → WatchlistEntryのフィールド名 の対応
_HEADER_MAP = {
    "銘柄コード": "symbol",
    "銘柄名": "name",
    "前日終値": "prev_close",
    "前日始値": "prev_open",
    "前日高値": "prev_high",
    "前日安値": "prev_low",
    "前日出来高": "prev_volume",
    "前日RSI14": "prev_rsi",
    "前日BB上限": "prev_bb_upper",
    "前日BBミドル": "prev_bb_middle",
    "前日BB下限": "prev_bb_lower",
}
_FLOAT_FIELDS = {
    "prev_close", "prev_open", "prev_high", "prev_low", "prev_volume",
    "prev_rsi", "prev_bb_upper", "prev_bb_middle", "prev_bb_lower",
}


class MonitorTab(QWidget):
    watchlist_loaded = Signal(list)  # List[WatchlistEntry]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._row_index_by_symbol: Dict[str, int] = {}
        self._watchlist: List[WatchlistEntry] = []

        self.load_button = QPushButton("銘柄リストCSVを読込...")
        self.load_button.clicked.connect(self._on_load_clicked)
        self.watchlist_status_label = QLabel("銘柄リスト未読込")

        toolbar_layout = QHBoxLayout()
        toolbar_layout.addWidget(self.load_button)
        toolbar_layout.addWidget(self.watchlist_status_label)
        toolbar_layout.addStretch(1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout = QVBoxLayout()
        layout.addLayout(toolbar_layout)
        layout.addWidget(self.table)
        self.setLayout(layout)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "銘柄リストCSVを選択（銘柄コード,銘柄名,前日終値,前日始値,前日高値,前日安値,"
                  "前日出来高,前日RSI14,前日BB上限,前日BBミドル,前日BB下限）",
            "", "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            entries = self._load_watchlist_csv(path)
        except ValueError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "CSV読込エラー", str(e))
            return

        self.set_watchlist(entries)
        self.watchlist_status_label.setText(f"{len(entries)}銘柄を読込済み（{path}）")
        self.watchlist_loaded.emit(entries)

    @staticmethod
    def _load_watchlist_csv(path: str) -> List[WatchlistEntry]:
        entries: List[WatchlistEntry] = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "銘柄コード" not in reader.fieldnames:
                raise ValueError(
                    "CSVのヘッダー行に「銘柄コード」列が見つかりません。"
                    "1行目に列名（銘柄コード,銘柄名,前日終値,...）を入れてください。"
                )

            for rank, row in enumerate(reader, start=1):
                kwargs: Dict[str, Any] = {"rank": rank}
                for header_name, field_name in _HEADER_MAP.items():
                    raw = row.get(header_name)
                    if raw is None:
                        continue
                    raw = raw.strip()
                    if not raw:
                        continue
                    if field_name in _FLOAT_FIELDS:
                        try:
                            kwargs[field_name] = float(raw)
                        except ValueError:
                            continue  # 数値変換できない値は無視（Noneのまま）
                    else:
                        kwargs[field_name] = raw

                if not kwargs.get("symbol"):
                    continue
                kwargs.setdefault("name", "")
                entries.append(WatchlistEntry(**kwargs))
        return entries

    def get_watchlist(self) -> List[WatchlistEntry]:
        return list(self._watchlist)

    def set_watchlist(self, entries: List[WatchlistEntry]) -> None:
        self._watchlist = list(entries)
        self.table.setRowCount(len(entries))
        self._row_index_by_symbol.clear()
        for row, entry in enumerate(entries):
            self._row_index_by_symbol[entry.symbol] = row
            self.table.setItem(row, 0, QTableWidgetItem(entry.symbol))
            self.table.setItem(row, 1, QTableWidgetItem(entry.name))
            for col in range(2, len(COLUMNS)):
                self.table.setItem(row, col, QTableWidgetItem("-"))

    def update_symbol(self, symbol: str, snapshot: Dict[str, Any], price: float, has_position: bool) -> None:
        """
        snapshot: SignalEngine.get_context(symbol).snapshot() の出力
                  （例: {"rsi14": {"rsi14": 28.3}, "vwap": {"deviation_pct": -1.2}, "bb": {"position": "WITHIN"}}）
        """
        row = self._row_index_by_symbol.get(symbol)
        if row is None:
            return

        self.table.setItem(row, 2, QTableWidgetItem(f"{price:.1f}"))

        rsi = self._extract(snapshot, "rsi14", "rsi14")
        self.table.setItem(row, 3, QTableWidgetItem(self._fmt(rsi)))

        vwap_dev = self._extract(snapshot, "vwap", "deviation_pct")
        self.table.setItem(row, 4, QTableWidgetItem(self._fmt(vwap_dev)))

        bb_position = self._extract(snapshot, "bb", "position")
        self.table.setItem(row, 5, QTableWidgetItem(str(bb_position) if bb_position else "-"))

        self.table.setItem(row, 7, QTableWidgetItem("保有中" if has_position else "-"))

    def set_signal_flag(self, symbol: str, signal_type: str) -> None:
        row = self._row_index_by_symbol.get(symbol)
        if row is None:
            return
        item = QTableWidgetItem(signal_type)
        if signal_type == "ENTRY":
            item.setForeground(Qt.GlobalColor.darkGreen)
        elif signal_type == "EXIT":
            item.setForeground(Qt.GlobalColor.darkRed)
        self.table.setItem(row, 6, item)

    @staticmethod
    def _extract(snapshot: Dict[str, Any], indicator_key: str, field: str):
        d = snapshot.get(indicator_key)
        if not d:
            return None
        return d.get(field)

    @staticmethod
    def _fmt(value) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)
