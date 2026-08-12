"""
SQLite接続・スキーマ初期化。

基本設計書6節「DB設計（SQLite）」のテーブル定義に対応する。
DBファイルは既定でプロジェクト直下の data/kabu_daytrader.db に作成する
（Gitの管理対象外にすること。機密情報は含まないが、取引履歴という
個人情報に準じるデータのため）。
"""

import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "kabu_daytrader.db"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS symbols (
        symbol_code   TEXT PRIMARY KEY,
        symbol_name   TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date    TEXT NOT NULL,
        symbol_code   TEXT NOT NULL,
        rank          INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at   TEXT NOT NULL,
        symbol_code   TEXT NOT NULL,
        signal_type   TEXT NOT NULL,
        rule_name     TEXT NOT NULL,
        indicator_snapshot TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id      TEXT PRIMARY KEY,
        symbol_code   TEXT NOT NULL,
        side          TEXT NOT NULL,
        order_type    TEXT NOT NULL DEFAULT 'MARKET',
        qty           INTEGER NOT NULL,
        status        TEXT NOT NULL,
        reason        TEXT,
        requested_at  TEXT NOT NULL,
        filled_at     TEXT,
        filled_price  REAL,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        symbol_code    TEXT PRIMARY KEY,
        qty            INTEGER NOT NULL,
        entry_price    REAL NOT NULL,
        entry_order_id TEXT NOT NULL,
        entry_at       TEXT NOT NULL,
        status         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS closed_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol_code   TEXT NOT NULL,
        qty           INTEGER NOT NULL,
        entry_price   REAL NOT NULL,
        exit_price    REAL NOT NULL,
        entry_at      TEXT NOT NULL,
        exit_at       TEXT NOT NULL,
        reason        TEXT NOT NULL,
        realized_pnl  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_pnl (
        trade_date       TEXT PRIMARY KEY,
        realized_pnl     REAL NOT NULL DEFAULT 0,
        win_count        INTEGER NOT NULL DEFAULT 0,
        lose_count       INTEGER NOT NULL DEFAULT 0,
        daily_limit_hit  TEXT,
        stopped_at       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key           TEXT PRIMARY KEY,
        value         TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_bars (
        symbol_code   TEXT NOT NULL,
        ts            TEXT NOT NULL,
        open          REAL,
        high          REAL,
        low           REAL,
        close         REAL,
        volume        INTEGER,
        PRIMARY KEY (symbol_code, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at        TEXT NOT NULL,
        period_from   TEXT NOT NULL,
        period_to     TEXT NOT NULL,
        params_json   TEXT NOT NULL,
        total_pnl     REAL,
        win_rate      REAL,
        max_drawdown  REAL
    )
    """,
]


def connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """
    DB接続を作成し、テーブルが存在しなければ作成して返す。
    db_path=":memory:" を渡すとインメモリDB（テスト用）になる。
    """
    path = db_path if db_path is not None else DEFAULT_DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False: TradingControllerはPUSH受信スレッド（別スレッド）
    # からもこの接続を使うため、sqlite3側のスレッドチェックを無効化する。
    # 排他制御はTradingController側のthreading.Lockで行う前提
    # （複数スレッドから同時にsqlite3接続へアクセスしないことを呼び出し元が保証する）。
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn)
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
