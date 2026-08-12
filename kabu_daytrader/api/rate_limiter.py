"""
kabuステーションREST APIのレート制限（発注系5件/秒、情報・登録系10件/秒）を
順守するためのシンプルなスライディングウィンドウ方式のレートリミッタ。
"""

import threading
import time
from collections import deque
from typing import Callable, Deque


class RateLimiter:
    """
    直近 window_seconds 秒間の呼び出し回数が max_calls を超えないよう、
    必要に応じて acquire() 内でブロッキング待機する。

    time_func / sleep_func はテスト時に差し替え可能にしてある
    （実時間を待たずにレート制限ロジックだけを検証するため）。
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: float = 1.0,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        if max_calls < 1:
            raise ValueError("max_calls は1以上を指定してください")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._time_func = time_func
        self._sleep_func = sleep_func
        self._call_times: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """
        呼び出し許可が得られるまでブロックする。
        許可が得られたら、その時刻を記録して即座に返る。
        """
        with self._lock:
            while True:
                now = self._time_func()
                # ウィンドウ外の古い記録を捨てる
                while self._call_times and now - self._call_times[0] >= self.window_seconds:
                    self._call_times.popleft()

                if len(self._call_times) < self.max_calls:
                    self._call_times.append(now)
                    return

                wait = self.window_seconds - (now - self._call_times[0])
                if wait > 0:
                    self._sleep_func(wait)
                # ループして再判定（sleep_funcがテスト用ダミーの場合も安全に収束する）
