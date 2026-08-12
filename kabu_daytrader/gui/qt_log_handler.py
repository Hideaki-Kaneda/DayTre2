"""
標準loggingのレコードをQtシグナルとして流すハンドラ。
基本設計書7.7節「ログ・アラートタブ」に対応。
別スレッド（PushWorker/OrderWorker等）からのログもQt Signal経由で
安全にGUIスレッドへ届けられる（Qt Signal/Slotはスレッドセーフ）。
"""

import logging

from PySide6.QtCore import QObject, Signal


class QtLogHandler(logging.Handler, QObject):
    log_emitted = Signal(str, int)  # (フォーマット済みメッセージ, levelno)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            msg = record.getMessage()
        self.log_emitted.emit(msg, record.levelno)
