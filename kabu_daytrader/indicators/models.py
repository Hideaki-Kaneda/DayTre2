"""
指標計算で共通利用するデータモデル。

基本設計書3節のシステム構成図における「PushClientWorker」からの
価格Tick受信データを表す。kabuステーションAPIのPUSH配信データ
（Board API相当）から必要な項目のみを抜き出して生成する想定。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class PriceTick:
    """1回のPUSH配信（もしくはREST取得）で得られる価格情報。"""

    symbol: str
    timestamp: datetime
    price: float
    """現在値（CurrentPrice）"""

    volume: Optional[float] = None
    """このTickでの出来高（累計ではなく、直近約定分の出来高が分かる場合のみ設定。
    kabuステーションPUSHのTradingVolume等、累計出来高しか取れない場合は
    呼び出し側で差分に変換してから渡すこと）"""

    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None

    vwap_from_api: Optional[float] = None
    """kabuステーションPUSH配信のVWAPフィールドをそのまま保持する（参考値）。
    indicators.VWAPIndicatorは出来高から自前で計算する方式のままにしているが、
    API側の値と突き合わせて検算したい場合や、将来的にAPI提供値をそのまま
    使う実装に切り替えたい場合のためにここに保持しておく。"""
