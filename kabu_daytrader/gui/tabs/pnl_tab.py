"""
損益タブ（基本設計書7.5節）。
"""

from PySide6.QtWidgets import QFormLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from trading.risk_manager import DailyPnlState


class PnlTab(QWidget):
    def __init__(self, daily_profit_target: float, daily_max_loss: float, parent=None):
        super().__init__(parent)
        self.daily_profit_target = daily_profit_target
        self.daily_max_loss = daily_max_loss

        self.realized_pnl_label = QLabel("¥0")
        self.realized_pnl_label.setStyleSheet("font-size: 28px; font-weight: bold;")

        self.win_lose_label = QLabel("勝ち: 0 / 負け: 0")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(-100, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("目標に対する進捗")

        self.status_label = QLabel("通常稼働中")

        form = QFormLayout()
        form.addRow("本日の実現損益", self.realized_pnl_label)
        form.addRow("勝敗", self.win_lose_label)
        form.addRow("目標/上限までの進捗", self.progress_bar)
        form.addRow("状態", self.status_label)

        layout = QVBoxLayout()
        layout.addLayout(form)
        self.setLayout(layout)

    def update_daily_state(self, state: DailyPnlState) -> None:
        self.realized_pnl_label.setText(f"¥{state.realized_pnl:+,.0f}")
        self.win_lose_label.setText(f"勝ち: {state.win_count} / 負け: {state.lose_count}")

        if state.realized_pnl >= 0:
            ratio = min(state.realized_pnl / self.daily_profit_target, 1.0) if self.daily_profit_target else 0
            self.progress_bar.setValue(int(ratio * 100))
        else:
            ratio = min(abs(state.realized_pnl) / self.daily_max_loss, 1.0) if self.daily_max_loss else 0
            self.progress_bar.setValue(int(-ratio * 100))

        if state.daily_limit_hit == "PROFIT_TARGET":
            self.status_label.setText("日次利益上限に到達（新規エントリー停止・全決済済み）")
        elif state.daily_limit_hit == "MAX_LOSS":
            self.status_label.setText("日次最大損失に到達（新規エントリー停止・全決済済み）")
        else:
            self.status_label.setText("通常稼働中")
