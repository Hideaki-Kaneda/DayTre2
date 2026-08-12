"""
バックテストのエントリー・決済（利確/損切/強制決済等）ログをCSVファイルへ出力する。
"""

import csv
from pathlib import Path
from typing import List

from .models import OrderLogEntry

LOG_CSV_COLUMNS = ["日時", "種別", "銘柄コード", "株数", "価格", "理由", "実現損益"]

_REASON_LABELS = {
    "SIGNAL": "シグナル",
    "STOP_LOSS": "損切",
    "DAILY_PROFIT_TARGET": "日次利益上限",
    "DAILY_MAX_LOSS": "日次最大損失",
    "FORCE_CLOSE_TIME": "強制引け決済",
    "PUSH_DISCONNECT": "PUSH切断",
    "BACKTEST_END": "データ終了時強制決済",
}

_EVENT_TYPE_LABELS = {"ENTRY": "エントリー", "EXIT": "決済"}


def write_backtest_log_csv(log_entries: List[OrderLogEntry], path: str | Path) -> None:
    """
    エントリー・決済ログを時系列順にCSVへ書き出す。
    「理由」列で損切（STOP_LOSS）かどうかが分かるため、
    損切だけを後から抽出・集計することもできる。
    """
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
                entry.qty,
                f"{entry.price:.1f}",
                _REASON_LABELS.get(entry.reason, entry.reason),
                f"{entry.realized_pnl:+.0f}" if entry.realized_pnl is not None else "",
            ])
