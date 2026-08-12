"""
ログ・アラートタブ（基本設計書7.7節）。
重大イベント（ERROR以上）は赤字で強調表示する。
"""

import logging

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget


class LogTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setMaximumBlockCount(5000)  # 無制限蓄積によるメモリ肥大を防ぐ

        layout = QVBoxLayout()
        layout.addWidget(self.text_edit)
        self.setLayout(layout)

    def append_log(self, message: str, levelno: int) -> None:
        color = "black"
        if levelno >= logging.ERROR:
            color = "red"
        elif levelno >= logging.WARNING:
            color = "darkorange"

        self.text_edit.appendHtml(f'<span style="color:{color};">{self._escape(message)}</span>')

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
