"""
SignalEngineが生成するシグナルイベント。

DB設計（基本設計書6節 signal_log テーブル）に対応する形にしている。
occurred_at / symbol_code / signal_type / rule_name / indicator_snapshot
の各カラムにそのまま保存できる。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Literal, Optional

SignalType = Literal["ENTRY", "EXIT"]


@dataclass
class SignalEvent:
    symbol: str
    signal_type: SignalType
    rule_name: str
    occurred_at: datetime
    indicator_snapshot: Dict[str, Any] = field(default_factory=dict)
    """例: {"sma_short": {"sma5": 1234.5}, "rsi14": {"rsi14": 28.3}, ...}
    signal_logテーブルへ保存する際はjson.dumps()してindicator_snapshot列に入れる想定。"""


@dataclass
class WatchlistEntry:
    """
    スクリーナーCSVから読み込む監視銘柄1件分のデータ。

    銘柄コード・銘柄名に加え、指標のウォームアップに使う前日の要約値
    （終値・始値・高値・安値・出来高・RSI14・ボリンジャーバンド±2σ/ミドル）を保持する。
    要約値は任意（CSVに列がなければNoneのまま）で、無い場合は
    SignalEngine.warmup_symbol_from_summary()が可能な範囲でフォールバックする。
    """

    symbol: str
    name: str
    rank: int = 0
    prev_close: Optional[float] = None
    prev_open: Optional[float] = None
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    prev_volume: Optional[float] = None
    prev_rsi: Optional[float] = None
    prev_bb_upper: Optional[float] = None
    prev_bb_middle: Optional[float] = None
    prev_bb_lower: Optional[float] = None
