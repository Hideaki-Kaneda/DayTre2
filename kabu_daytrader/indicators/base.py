"""
指標プラグインの共通インターフェース。

基本設計書3節「指標プラグイン設計（拡張ポイント）」に基づく。
新しい指標を追加する場合は、このIndicatorを継承したクラスを
indicators/配下に作成し、registry.pyのINDICATOR_REGISTRYに
登録するだけで、SignalEngine側のコードを変更せずに組み込める。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .models import PriceTick


class Indicator(ABC):
    """全テクニカル指標が実装する共通インターフェース。"""

    def __init__(self, params: Dict[str, Any] | None = None):
        """
        params: 設定ファイルから渡されるパラメータ辞書
                （例: {"period": 25} 、 {"period": 20, "num_std": 2}）
        """
        self.params: Dict[str, Any] = params or {}

    @abstractmethod
    def update(self, tick: PriceTick) -> None:
        """新しい価格データを受け取り、内部状態を更新する。"""
        raise NotImplementedError

    @abstractmethod
    def value(self) -> Dict[str, Any]:
        """
        現在の指標値を返す。
        is_ready() が False の間に呼ばれた場合も、
        キーは揃った状態でNoneを返すなど、呼び出し側が
        安全に扱える形にすること。
        """
        raise NotImplementedError

    @abstractmethod
    def is_ready(self) -> bool:
        """算出に十分なデータが揃っているかどうか。"""
        raise NotImplementedError

    def reset(self) -> None:
        """
        内部状態をリセットする（バックテストで銘柄・期間を切り替える際、
        あるいは日次セッション境界で使用）。
        既定実装は「同じparamsで__init__し直す」。多くの指標はこれで足りるが、
        VWAPのようにセッション単位でのリセットが必要な指標は独自実装する。
        """
        self.__init__(self.params)  # type: ignore[misc]

    def seed(
        self,
        prev_close: Optional[float] = None,
        prev_open: Optional[float] = None,
        prev_high: Optional[float] = None,
        prev_low: Optional[float] = None,
        prev_rsi: Optional[float] = None,
        prev_bb_upper: Optional[float] = None,
        prev_bb_middle: Optional[float] = None,
        prev_bb_lower: Optional[float] = None,
    ) -> bool:
        """
        前日終値・前日高値/安値・前日RSI・前日ボリンジャーバンド等の要約値から、
        寄り付き前に指標の内部状態を直接構築する（ウォームアップ）。

        kabuステーションAPIには分足の過去データを取得する手段がないため、
        スクリーナーCSV等で別途用意した前日の要約値を使って、寄り付き直後から
        指標が機能する状態を作るために使う。

        各指標クラスは自分に関係する引数だけを使って実装し、
        シードできた場合はTrueを返す。対応する情報が無ければFalseを返し、
        呼び出し元（SignalEngine）が代替手段（前日終値のみでの簡易ウォームアップ等）に
        フォールバックできるようにする。
        既定実装は何もせずFalseを返す（シード非対応の指標向け）。
        """
        return False
