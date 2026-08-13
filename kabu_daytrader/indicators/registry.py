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
from .dmi import DMIIndicator
from .macd import MACDIndicator
from .rsi import RSIIndicator

INDICATOR_REGISTRY: Dict[str, Type[Indicator]] = {
    "rsi": RSIIndicator,
    "macd": MACDIndicator,
    "dmi": DMIIndicator,
    # 新規指標はここに追記するだけでSignalEngine側の変更は不要
}


def create_indicator(name: str, params: Dict[str, Any] | None = None) -> Indicator:
    """レジストリからインジケータ名でインスタンスを生成する。"""
    cls = INDICATOR_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(INDICATOR_REGISTRY.keys()))
        raise ValueError(f"未登録の指標です: '{name}'（登録済み: {available}）")

    return cls(dict(params or {}))
