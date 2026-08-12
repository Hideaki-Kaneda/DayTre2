"""
LiveBarAggregator：PUSH APIから届く生Tickを、N分足のOHLCVバーへ集約する。

バックテストは分足CSV（resample_bars()で任意の足間隔に変換可能）を使うが、
本番監視はPUSH配信の生Tick（価格変化のたびに届く）をそのまま扱っている。
OpeningRangeARIndicator（当日のAR算出）は「確定したN分足」を前提にしているため、
本番側でも同様にバー確定単位でのIndicator更新が必要になる。

このクラスは、生Tickをバケット（N分単位の時間区間）に集約し、
バケットの区間が変わった（＝前のバーが確定した）タイミングで、
その確定バーを表す合成PriceTick（open/high/low/close/volumeを保持）を返す。
まだバケットが継続中の場合はNoneを返す。
"""

from datetime import datetime
from typing import Dict, Optional

from indicators import PriceTick


class LiveBarAggregator:
    def __init__(self, interval_minutes: int = 3):
        if interval_minutes < 1:
            raise ValueError("interval_minutesは1以上を指定してください")
        self.interval_minutes = interval_minutes
        self._buckets: Dict[str, dict] = {}

    def _floor_to_interval(self, dt: datetime) -> datetime:
        total_minutes = dt.hour * 60 + dt.minute
        floored_minutes = (total_minutes // self.interval_minutes) * self.interval_minutes
        return dt.replace(hour=floored_minutes // 60, minute=floored_minutes % 60, second=0, microsecond=0)

    def add_tick(self, tick: PriceTick) -> Optional[PriceTick]:
        """
        生Tickを取り込み、直前のバーが確定した場合はそのバーを表す
        合成PriceTick（バー開始時刻をtimestampとする）を返す。
        バケットが継続中の場合はNoneを返す（呼び出し元は何もしなくてよい）。
        """
        bucket_start = self._floor_to_interval(tick.timestamp)
        bucket = self._buckets.get(tick.symbol)

        completed_bar: Optional[PriceTick] = None
        if bucket is not None and bucket["start"] != bucket_start:
            completed_bar = PriceTick(
                symbol=tick.symbol,
                timestamp=bucket["start"],
                price=bucket["close"],
                volume=bucket["volume"],
                open=bucket["open"],
                high=bucket["high"],
                low=bucket["low"],
            )
            bucket = None

        if bucket is None:
            bucket = {
                "start": bucket_start,
                "open": tick.price,
                "high": tick.price,
                "low": tick.price,
                "close": tick.price,
                "volume": tick.volume or 0,
            }
        else:
            bucket["high"] = max(bucket["high"], tick.price)
            bucket["low"] = min(bucket["low"], tick.price)
            bucket["close"] = tick.price
            bucket["volume"] += tick.volume or 0

        self._buckets[tick.symbol] = bucket
        return completed_bar

    def flush(self, symbol: str) -> Optional[PriceTick]:
        """
        強制引け等、バケットの区間終了を待たずに現時点のバーを確定させたい場合に使う。
        該当銘柄のバケットが無ければNoneを返す。
        """
        bucket = self._buckets.pop(symbol, None)
        if bucket is None:
            return None
        return PriceTick(
            symbol=symbol,
            timestamp=bucket["start"],
            price=bucket["close"],
            volume=bucket["volume"],
            open=bucket["open"],
            high=bucket["high"],
            low=bucket["low"],
        )
