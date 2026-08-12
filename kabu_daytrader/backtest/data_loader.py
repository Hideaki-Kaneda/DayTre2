"""
J-Quants API（分足データ、CSVエクスポート）の読込モジュール。

実機で確認した実際のフォーマット（Date列とTime列が分かれている点に注意）:
    Date,Time,Code,O,H,L,C,Vo,Va
    2025-01-06,09:00,31820,1283,1284,1278,1278,27000,34636900

    Date: 日付（YYYY-MM-DD）
    Time: 時刻（HH:MM、秒なし）
    Code: 銘柄コード（5桁。例: 3182の銘柄が31820）
    O/H/L/C: 始値・高値・安値・終値
    Vo: 出来高（本モジュールで使用）
    Va: 売買代金（本モジュールでは未使用。列として存在しても無視される）

また、J-Quants APIの銘柄コードは5桁（例: 3182 → 31820）。
kabuステーションAPI側は4桁コードを使うため、取り込み時に末尾の "0" を
落とす変換を既定で行う（normalize_jquants_code=True の場合）。
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .models import Bar

# Date/Time列が分かれているフォーマット（実機確認済み）を既定とする。
# 単一のDateTime列を持つCSVを読む場合は、load_bars_from_csv呼び出し時に
# column_map={"timestamp": "実際の列名"} のように指定すれば、
# date/time方式ではなくtimestamp方式（単一列）で読み込む。
DEFAULT_COLUMN_MAP: Dict[str, str] = {
    "symbol": "Code",
    "date": "Date",
    "time": "Time",
    "open": "O",
    "high": "H",
    "low": "L",
    "close": "C",
    "volume": "Vo",
}


def _normalize_symbol(raw_code: str, normalize_jquants_code: bool) -> str:
    code = str(raw_code).strip()
    if normalize_jquants_code and len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


def _parse_timestamp(raw: str) -> datetime:
    raw = raw.strip()
    # ISO8601形式・"YYYY-MM-DD HH:MM:SS"形式・秒無し"YYYY-MM-DD HH:MM"形式のいずれにも対応
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            if fmt is None:
                return datetime.fromisoformat(raw)
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"日時のパースに失敗しました: '{raw}'")


def load_bars_from_csv(
    csv_path: str | Path,
    column_map: Optional[Dict[str, str]] = None,
    normalize_jquants_code: bool = True,
    encoding: str = "utf-8-sig",
) -> List[Bar]:
    """
    CSVファイルを読み込み、タイムスタンプ昇順にソートした Bar のリストを返す。

    column_map: DEFAULT_COLUMN_MAP を上書きできる。
        - Date/Time列が分かれている場合（既定）: {"date": "...", "time": "...", ...}
        - 単一のDateTime列の場合: {"timestamp": "実際の列名", ...} を指定すると
          date/timeの代わりにtimestamp単一列モードで読み込む。
        - いずれの場合も symbol/open/high/low/close/volume は必須。
    """
    mapping = {**DEFAULT_COLUMN_MAP, **(column_map or {})}
    use_single_timestamp_col = "timestamp" in (column_map or {})

    if use_single_timestamp_col:
        required_keys = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
    else:
        required_keys = ("symbol", "date", "time", "open", "high", "low", "close", "volume")

    for key in required_keys:
        if key not in mapping:
            raise ValueError(f"column_mapに '{key}' の対応が指定されていません")

    bars: List[Bar] = []
    path = Path(csv_path)
    with path.open(newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        missing_columns = [mapping[k] for k in required_keys if mapping[k] not in (reader.fieldnames or [])]
        if missing_columns:
            raise ValueError(
                f"CSVに必要な列が見つかりません: {missing_columns}"
                f"（列名の対応が実際のJ-Quants出力と異なる場合はcolumn_mapで指定してください）"
            )

        for row in reader:
            symbol = _normalize_symbol(row[mapping["symbol"]], normalize_jquants_code)
            if use_single_timestamp_col:
                timestamp = _parse_timestamp(row[mapping["timestamp"]])
            else:
                timestamp = _parse_timestamp(f"{row[mapping['date']]} {row[mapping['time']]}")
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=float(row[mapping["open"]]),
                    high=float(row[mapping["high"]]),
                    low=float(row[mapping["low"]]),
                    close=float(row[mapping["close"]]),
                    volume=float(row[mapping["volume"]]),
                )
            )

    bars.sort(key=lambda b: b.timestamp)
    return bars


def _floor_to_interval(dt: datetime, interval_minutes: int) -> datetime:
    """
    dtを interval_minutes 単位の境界に切り下げる。
    9:00始まりの取引時間を前提に、真夜中0:00からの経過分を基準に丸めるため、
    9:00, 9:03, 9:06 ... のようにキリの良い境界になる
    （interval_minutesが3・5・15など、9:00(=540分)を割り切れる値であれば特に綺麗に揃う）。
    """
    total_minutes = dt.hour * 60 + dt.minute
    floored_minutes = (total_minutes // interval_minutes) * interval_minutes
    return dt.replace(hour=floored_minutes // 60, minute=floored_minutes % 60, second=0, microsecond=0)


def resample_bars(bars: List[Bar], interval_minutes: int) -> List[Bar]:
    """
    1分足（または元の足）のBarリストを、interval_minutes単位のOHLCVに集約する。
    複数銘柄が混在していても銘柄ごとに独立して集約する。

    interval_minutes<=1の場合は入力をそのまま返す（変換不要）。

    集約ルール（一般的な足の合成方法）:
        始値 = 区間内で最初のBarの始値
        高値 = 区間内の高値の最大値
        安値 = 区間内の安値の最小値
        終値 = 区間内で最後のBarの終値
        出来高 = 区間内の出来高の合計
    """
    if interval_minutes <= 1:
        return list(bars)
    if interval_minutes < 1:
        raise ValueError("interval_minutesは1以上を指定してください")

    buckets: Dict[tuple[str, datetime], List[Bar]] = {}
    for bar in bars:
        bucket_start = _floor_to_interval(bar.timestamp, interval_minutes)
        key = (bar.symbol, bucket_start)
        buckets.setdefault(key, []).append(bar)

    resampled: List[Bar] = []
    for (symbol, bucket_start), group in buckets.items():
        group.sort(key=lambda b: b.timestamp)
        resampled.append(
            Bar(
                symbol=symbol,
                timestamp=bucket_start,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
            )
        )

    resampled.sort(key=lambda b: b.timestamp)
    return resampled


def group_bars_by_timestamp(bars: List[Bar]) -> List[tuple[datetime, List[Bar]]]:
    """
    Barリストをタイムスタンプ昇順に整列した上で、同一時刻ごとにグルーピングする。
    複数銘柄のデータが同じ分足に存在する場合、それらを1つのティック群として
    まとめてBacktestEngineに渡すために使う。

    呼び出し元が複数銘柄のBarリストを単純に連結しただけ（銘柄ごとに固まっており
    時系列でインターリーブされていない）でも正しくグルーピングできるよう、
    内部で明示的にソートしてから処理する
    （load_bars_from_csv()は既にソート済みを返すが、それに依存しない）。
    """
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)

    groups: List[tuple[datetime, List[Bar]]] = []
    current_ts: Optional[datetime] = None
    current_group: List[Bar] = []

    for bar in sorted_bars:
        if bar.timestamp != current_ts:
            if current_group:
                groups.append((current_ts, current_group))  # type: ignore[arg-type]
            current_ts = bar.timestamp
            current_group = [bar]
        else:
            current_group.append(bar)

    if current_group:
        groups.append((current_ts, current_group))  # type: ignore[arg-type]

    return groups
