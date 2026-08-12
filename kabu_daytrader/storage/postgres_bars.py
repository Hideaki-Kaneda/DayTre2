"""
PostgreSQL上の分足データ（equities_bars_minute）への読み書き。

別途運用している分足取込バッチ（minute_data_import）と**同じテーブルを共有**する。
テーブル定義（既存バッチ側で作成済み、こちらでは作成しない）:

    CREATE TABLE IF NOT EXISTS equities_bars_minute (
        code        VARCHAR(10)   NOT NULL,
        datetime    TIMESTAMP     NOT NULL,
        open_price  NUMERIC(12,2) NOT NULL,
        high_price  NUMERIC(12,2) NOT NULL,
        low_price   NUMERIC(12,2) NOT NULL,
        close_price NUMERIC(12,2) NOT NULL,
        volume      BIGINT        NOT NULL,
        PRIMARY KEY (code, datetime)
    );

接続情報はconfig.ini（configparser形式、[postgresql]セクション）から読み込む。
パスワードを含む機密情報のため、コード中に一切ハードコードしない。

codeカラムの表記形式：kabuステーション形式と同じ**4桁**で統一されている
（確定済み）。変換処理は不要なため、normalize_jquants_code / to_db_code は
現在は素通し（何もしない）実装になっている。
"""

import configparser
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import psycopg2

from backtest.models import Bar

TABLE_NAME = "equities_bars_minute"


def load_pg_config(config_path: str | Path) -> dict:
    """
    config.ini（[postgresql]セクション）から接続情報を読み込む。
    パスワードはこの関数の戻り値にのみ含まれ、ログ出力・永続化はしないこと。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"config.iniが見つかりません: {path}")

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if "postgresql" not in parser:
        raise ValueError(f"config.iniに[postgresql]セクションがありません: {path}")

    section = parser["postgresql"]
    return {
        "host": section.get("host", "localhost"),
        "port": section.getint("port", 5432),
        "dbname": section.get("dbname"),
        "user": section.get("user"),
        "password": section.get("password"),
    }


def connect(config_path: str | Path):
    """
    psycopg2接続を新規に作成する。呼び出し側で必ずclose()すること。
    バックテスト（都度接続）と本番監視（1本の接続を使い回す）の両方から使う。
    """
    cfg = load_pg_config(config_path)
    return psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
    )


def normalize_jquants_code(raw_code: str) -> str:
    """
    【現在は実質不要】DB上のcode列はkabuステーション形式と同じ4桁で
    統一されているため、変換せずそのまま返す（後方互換のため関数として残す）。
    """
    return str(raw_code).strip()


def to_db_code(kabu_code: str) -> str:
    """
    【現在は実質不要】DB上のcode列はkabuステーション形式と同じ4桁で
    統一されているため、変換せずそのまま返す（後方互換のため関数として残す）。
    """
    return str(kabu_code).strip()


def _row_to_bar(row: tuple, normalize_code: bool) -> Bar:
    code, dt, open_p, high_p, low_p, close_p, volume = row
    return Bar(
        symbol=normalize_jquants_code(code) if normalize_code else str(code),
        timestamp=dt,
        open=float(open_p), high=float(high_p), low=float(low_p), close=float(close_p),
        volume=float(volume),
    )


def load_bars(
    config_path: str | Path,
    codes: List[str],
    start: datetime,
    end: datetime,
    normalize_code: bool = True,
) -> List[Bar]:
    """
    バックテスト用：指定銘柄（複数可）・期間の分足をDBから取得する。
    codesはDB上の表記（既存バッチの5桁コード）で指定する。
    戻り値はタイムスタンプ昇順（BacktestEngineがそのまま使える形）。
    """
    conn = connect(config_path)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT code, datetime, open_price, high_price, low_price, close_price, volume
                FROM {TABLE_NAME}
                WHERE code = ANY(%s) AND datetime >= %s AND datetime <= %s
                ORDER BY datetime
                """,
                (list(codes), start, end),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [_row_to_bar(row, normalize_code) for row in rows]


class LiveBarWriter:
    """
    本番監視で確定した1分足をequities_bars_minuteへ書き込む。
    毎分の書き込みで都度接続を張り直すのは非効率なため、1本の接続を
    保持して使い回す（start()〜stop()の間）。
    """

    def __init__(self, config_path: str | Path):
        self.config_path = config_path
        self._conn = None

    def open(self) -> None:
        self._conn = connect(self.config_path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def write_bar(
        self, kabu_code: str, dt: datetime, open_: float, high: float, low: float, close: float, volume: float
    ) -> None:
        """
        1分足を書き込む（同一キーなら上書き＝UPSERT）。
        kabu_codeはkabuステーション形式の4桁コードで渡してよい
        （内部でDB格納形式の5桁に変換する）。
        """
        if self._conn is None:
            self.open()

        db_code = to_db_code(kabu_code)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE_NAME} (code, datetime, open_price, high_price, low_price, close_price, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (code, datetime) DO UPDATE SET
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume
                """,
                (db_code, dt, open_, high, low, close, volume),
            )
        self._conn.commit()
