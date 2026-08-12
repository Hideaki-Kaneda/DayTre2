"""
基本設計書5節「スレッド構成」に対応するQThreadワーカー。
"""

from typing import List

from PySide6.QtCore import QThread, Signal

from backtest import Bar, BacktestConfig, BacktestEngine, BacktestResult
from candle_strategy import CandleBacktestConfig, CandleBacktestEngine, CandleBacktestResult


class BacktestWorker(QThread):
    finished_ok = Signal(object)  # BacktestResult
    finished_error = Signal(str)

    def __init__(self, config: BacktestConfig, bars: List[Bar], parent=None):
        super().__init__(parent)
        self.config = config
        self.bars = bars

    def run(self) -> None:
        try:
            engine = BacktestEngine(self.config)
            result: BacktestResult = engine.run(self.bars)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))
            return
        self.finished_ok.emit(result)


class CandleBacktestWorker(QThread):
    """
    candle_strategy（ローソク足連続パターン戦略）専用のバックテストワーカー。
    既存のBacktestWorkerとは別クラスとして分離している
    （エンジン自体もCandleBacktestEngineで完全に独立しているため）。
    """

    finished_ok = Signal(object)  # CandleBacktestResult
    finished_error = Signal(str)

    def __init__(self, config: CandleBacktestConfig, bars: List[Bar], parent=None):
        super().__init__(parent)
        self.config = config
        self.bars = bars

    def run(self) -> None:
        try:
            engine = CandleBacktestEngine(self.config)
            result: CandleBacktestResult = engine.run(self.bars)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))
            return
        self.finished_ok.emit(result)


class StartupWorker(QThread):
    """
    TradingController.start() はトークン認証・銘柄登録などネットワークI/Oを
    含み、完了までに数百ms〜数秒かかる可能性があるため、GUIスレッドを
    ブロックしないようQThread上で実行する。
    """

    finished_ok = Signal()
    finished_error = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller

    def run(self) -> None:
        try:
            self.controller.start()
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))
            return
        self.finished_ok.emit()
