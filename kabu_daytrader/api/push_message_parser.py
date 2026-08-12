"""
kabuステーションPUSH配信の生JSONメッセージを、indicators.PriceTick へ変換する。

PUSH配信フィールド例（実機確認・各種資料より）:
    {
        "Symbol": "9432",
        "SymbolName": "...",
        "CurrentPrice": 150.0,
        "CurrentPriceTime": "2026-07-28T09:01:23+09:00",
        "VWAP": 149.8,           # APIが計算済みのVWAP（参考値としてそのまま保持）
        "TradingVolume": 123456,  # 当日の"累計"出来高
        ...
    }

TradingVolumeは累計値で送られてくるため、直近の約定出来高（差分）に
変換してから PriceTick.volume に設定する。差分計算に必要な「前回の
累計出来高」は銘柄ごとにこのクラスの内部状態として保持する
（日付が変わったらリセットする）。
"""

from datetime import date, datetime
from typing import Any, Dict, Optional

from indicators import PriceTick


class PushMessageParser:
    def __init__(self):
        self._last_cum_volume: Dict[str, float] = {}
        self._last_date: Dict[str, date] = {}

    def parse(self, message: Dict[str, Any]) -> Optional[PriceTick]:
        """
        必要なフィールドが欠けている場合は None を返す
        （呼び出し元は None のTickを無視すればよい）。
        """
        symbol = message.get("Symbol")
        price = message.get("CurrentPrice")
        if symbol is None or price is None:
            return None

        timestamp = self._parse_timestamp(message.get("CurrentPriceTime"))
        volume_diff = self._compute_volume_diff(symbol, message.get("TradingVolume"), timestamp)

        return PriceTick(
            symbol=symbol,
            timestamp=timestamp,
            price=float(price),
            volume=volume_diff,
            vwap_from_api=self._to_float_or_none(message.get("VWAP")),
        )

    def _compute_volume_diff(
        self, symbol: str, cum_volume: Any, timestamp: datetime
    ) -> Optional[float]:
        if cum_volume is None:
            return None
        cum_volume = float(cum_volume)

        today = timestamp.date()
        last_date = self._last_date.get(symbol)
        if last_date is not None and last_date != today:
            # 日付が変わった＝新しいセッション。累計出来高がリセットされている前提でクリア
            self._last_cum_volume.pop(symbol, None)
        self._last_date[symbol] = today

        last_cum = self._last_cum_volume.get(symbol)
        self._last_cum_volume[symbol] = cum_volume

        if last_cum is None:
            # この銘柄で初めて受信したTick。差分を計算する基準がないため
            # 「その時点までの累計」をそのまま初回分の出来高として扱う。
            return cum_volume

        diff = cum_volume - last_cum
        if diff < 0:
            # 累計が減ることは通常ないが、万一の異常値は0として扱う（安全側）
            return 0.0
        return diff

    @staticmethod
    def _parse_timestamp(raw: Any) -> datetime:
        if raw is None:
            return datetime.now()
        if isinstance(raw, datetime):
            return raw
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return datetime.now()

    @staticmethod
    def _to_float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
