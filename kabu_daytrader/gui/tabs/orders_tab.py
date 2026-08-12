"""
注文・決済タブ（基本設計書7.4節）。

発注・約定が発生すると、MainWindow側からこのタブへ自動的にタブ切替される
（setCurrentWidget()はMainWindowが行う。本タブ自体はデータ表示に専念する）。
"""

from datetime import datetime

from PySide6.QtWidgets import QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from trading.models import OrderResult, OrderStatus, Position

POSITION_COLUMNS = ["銘柄コード", "株数", "取得価格", "現在値", "含み損益"]
ORDER_COLUMNS = ["時刻", "銘柄コード", "区分", "理由", "株数", "約定価格", "状態"]


class OrdersTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.position_table = QTableWidget(0, len(POSITION_COLUMNS))
        self.position_table.setHorizontalHeaderLabels(POSITION_COLUMNS)
        self.position_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.position_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.order_table = QTableWidget(0, len(ORDER_COLUMNS))
        self.order_table.setHorizontalHeaderLabels(ORDER_COLUMNS)
        self.order_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.order_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("保有ポジション"))
        layout.addWidget(self.position_table)
        layout.addWidget(QLabel("注文・約定履歴"))
        layout.addWidget(self.order_table)
        self.setLayout(layout)

    def add_order_event(self, result: OrderResult) -> None:
        row = 0
        self.order_table.insertRow(row)
        ts = result.filled_at or result.requested_at
        self.order_table.setItem(row, 0, QTableWidgetItem(ts.strftime("%H:%M:%S")))
        self.order_table.setItem(row, 1, QTableWidgetItem(result.symbol))
        self.order_table.setItem(row, 2, QTableWidgetItem(result.side.value))
        self.order_table.setItem(row, 3, QTableWidgetItem(result.reason.value))
        self.order_table.setItem(row, 4, QTableWidgetItem(str(result.qty)))
        price_text = f"{result.filled_price:.1f}" if result.filled_price is not None else "-"
        self.order_table.setItem(row, 5, QTableWidgetItem(price_text))
        status_text = "約定" if result.status == OrderStatus.FILLED else result.status.value
        self.order_table.setItem(row, 6, QTableWidgetItem(status_text))

    def refresh_positions(self, positions: list[Position], current_prices: dict[str, float]) -> None:
        self.position_table.setRowCount(len(positions))
        for row, pos in enumerate(positions):
            current_price = current_prices.get(pos.symbol, pos.entry_price)
            unrealized = (current_price - pos.entry_price) * pos.qty
            self.position_table.setItem(row, 0, QTableWidgetItem(pos.symbol))
            self.position_table.setItem(row, 1, QTableWidgetItem(str(pos.qty)))
            self.position_table.setItem(row, 2, QTableWidgetItem(f"{pos.entry_price:.1f}"))
            self.position_table.setItem(row, 3, QTableWidgetItem(f"{current_price:.1f}"))
            item = QTableWidgetItem(f"{unrealized:+.0f}")
            self.position_table.setItem(row, 4, item)
