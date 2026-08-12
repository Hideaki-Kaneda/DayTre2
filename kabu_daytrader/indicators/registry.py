"""
指標レジストリ。

新規指標を追加する手順（基本設計書3節の拡張ポイント）:
    1. indicators/配下に Indicator を継承した新しいクラスを作成する
    2. このファイルの INDICATOR_REGISTRY に "キー名": クラス を追加する
    3. 設定ファイル（entry_rule/exit_rule や indicator_params）から
       そのキー名で参照できるようになる

SignalEngineや他のモジュールは、この registry.py 経由でのみ
指標クラスを取得すること（個別モジュールを直接importしない）。
"""

from typing import Any, Dict, Type

from .base import Indicator
from .bollinger_band import BollingerBandIndicator
from .moving_average import MovingAverageIndicator
from .opening_range_ar import OpeningRangeARIndicator
from .rsi import RSIIndicator
from .vwap import VWAPIndicator

INDICATOR_REGISTRY: Dict[str, Type[Indicator]] = {
    "sma": MovingAverageIndicator,
    "ema": MovingAverageIndicator,  # type=ema をparamsで指定して使う
    "rsi": RSIIndicator,
    "bollinger": BollingerBandIndicator,
    "vwap": VWAPIndicator,
    "opening_range_ar": OpeningRangeARIndicator,
    # 新規指標はここに追記するだけでSignalEngine側の変更は不要
}


def create_indicator(name: str, params: Dict[str, Any] | None = None) -> Indicator:
    """
    レジストリからインジケータ名でインスタンスを生成する。

    "ema" を指定した場合は MovingAverageIndicator に type="ema" を
    自動的にマージして渡す（設定ファイルの記述を簡潔にするため）。
    """
    cls = INDICATOR_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(INDICATOR_REGISTRY.keys()))
        raise ValueError(f"未登録の指標です: '{name}'（登録済み: {available}）")

    params = dict(params or {})
    if name == "ema":
        params.setdefault("type", "ema")
    elif name == "sma":
        params.setdefault("type", "sma")

    return cls(params)
