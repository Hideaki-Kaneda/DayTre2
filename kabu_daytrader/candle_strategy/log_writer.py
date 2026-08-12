"""
candle_strategyのバックテストのエントリー・決済ログをCSVファイルへ出力する。
backtest.log_writer.write_backtest_log_csv() と同じ考え方だが、
CandleLogEntry（direction列を持つ点が異なる）に対応させている。
"""

import csv
from pathlib import Path
from typing import List

from .models import CandleLogEntry

LOG_CSV_COLUMNS = ["日時", "種別", "銘柄コード", "方向", "株数", "価格", "理由", "実現損益"]

_REASON_LABELS = {
    "ENTRY_CONSECUTIVE_CANDLES": "連続陽線/陰線エントリー",
    "STOP_LOSS": "損切",
    "TAKE_PROFIT": "利確",
    "REVERSE_BARS": "逆足M本",
    "FORCE_CLOSE_TIME": "強制引け決済",
    "PUSH_DISCONNECT": "PUSH切断",
    "BACKTEST_END": "データ終了時強制決済",
}

_EVENT_TYPE_LABELS = {"ENTRY": "エントリー", "EXIT": "決済"}
_DIRECTION_LABELS = {"LONG": "買い建て", "SHORT": "売り建て（空売り）"}


def write_candle_log_csv(log_entries: List[CandleLogEntry], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(LOG_CSV_COLUMNS)
        for entry in log_entries:
            writer.writerow([
                entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                _EVENT_TYPE_LABELS.get(entry.event_type, entry.event_type),
                entry.symbol,
                _DIRECTION_LABELS.get(entry.direction.value, entry.direction.value),
                entry.qty,
                f"{entry.price:.1f}",
                _REASON_LABELS.get(entry.reason, entry.reason),
                f"{entry.realized_pnl:+.0f}" if entry.realized_pnl is not None else "",
            ])
