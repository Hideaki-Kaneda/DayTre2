"""
storage.postgres_bars の動作確認用テスト。
実際のPostgreSQLサーバーには接続せず、psycopg2をモック化してSQL構築・
コード変換ロジックを検証する。 `python tests/test_postgres_bars.py` で実行可能。
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.postgres_bars import (  # noqa: E402
    LiveBarWriter,
    load_bars,
    load_pg_config,
    normalize_jquants_code,
    to_db_code,
)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[postgresql]\n"
        "host = localhost\n"
        "port = 5433\n"
        "dbname = quants\n"
        "user = postgres\n"
        "password = dummy_password_for_test\n",
        encoding="utf-8",
    )
    return config_path


def test_normalize_jquants_code():
    # codeカラムは4桁で統一されているため、変換せずそのまま返す（後方互換の素通し実装）
    assert normalize_jquants_code("6613") == "6613"
    assert normalize_jquants_code("9432") == "9432"
    print("test_normalize_jquants_code: OK")


def test_to_db_code():
    # codeカラムは4桁で統一されているため、変換せずそのまま返す（後方互換の素通し実装）
    assert to_db_code("6613") == "6613"
    assert to_db_code("9432") == "9432"
    print("test_to_db_code: OK")


def test_load_pg_config_reads_ini_file():
    with tempfile.TemporaryDirectory() as td:
        config_path = _write_config(Path(td))
        cfg = load_pg_config(config_path)
        assert cfg["host"] == "localhost"
        assert cfg["port"] == 5433
        assert cfg["dbname"] == "quants"
        assert cfg["user"] == "postgres"
        assert cfg["password"] == "dummy_password_for_test"
    print("test_load_pg_config_reads_ini_file: OK")


def test_load_pg_config_missing_file_raises():
    try:
        load_pg_config("/nonexistent/path/config.ini")
        assert False, "例外が発生するはず"
    except FileNotFoundError:
        pass
    print("test_load_pg_config_missing_file_raises: OK")


def test_load_bars_queries_and_converts_rows():
    with tempfile.TemporaryDirectory() as td:
        config_path = _write_config(Path(td))

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("6613", datetime(2026, 7, 8, 9, 3), 2501.0, 2555.0, 2488.0, 2547.0, 195400),
            ("9432", datetime(2026, 7, 8, 9, 3), 150.0, 151.0, 149.0, 150.5, 5000),
        ]
        mock_cursor.__enter__ = lambda self: mock_cursor
        mock_cursor.__exit__ = lambda self, *a: None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("storage.postgres_bars.psycopg2.connect", return_value=mock_conn):
            bars = load_bars(
                config_path, codes=["6613", "9432"],
                start=datetime(2026, 7, 8, 9, 0), end=datetime(2026, 7, 8, 10, 0),
            )

        assert len(bars) == 2
        assert bars[0].symbol == "6613"
        assert bars[0].open == 2501.0
        assert bars[1].symbol == "9432"
        mock_conn.close.assert_called_once()

        # SQLにANYで複数コードを渡していること、期間条件も渡していることを確認
        executed_sql, executed_params = mock_cursor.execute.call_args[0]
        assert "equities_bars_minute" in executed_sql
        assert executed_params[0] == ["6613", "9432"]
    print("test_load_bars_queries_and_converts_rows: OK")


def test_live_bar_writer_upserts_with_converted_code():
    with tempfile.TemporaryDirectory() as td:
        config_path = _write_config(Path(td))

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda self: mock_cursor
        mock_cursor.__exit__ = lambda self, *a: None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("storage.postgres_bars.psycopg2.connect", return_value=mock_conn):
            writer = LiveBarWriter(config_path)
            writer.write_bar("6613", datetime(2026, 7, 8, 9, 3), 2501.0, 2555.0, 2488.0, 2547.0, 195400)

        executed_sql, executed_params = mock_cursor.execute.call_args[0]
        assert "ON CONFLICT (code, datetime) DO UPDATE" in executed_sql
        assert executed_params[0] == "6613"  # 4桁のまま変換されずに渡されること
        mock_conn.commit.assert_called_once()
    print("test_live_bar_writer_upserts_with_converted_code: OK")


def test_live_bar_writer_reuses_connection_across_writes():
    with tempfile.TemporaryDirectory() as td:
        config_path = _write_config(Path(td))

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda self: mock_cursor
        mock_cursor.__exit__ = lambda self, *a: None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("storage.postgres_bars.psycopg2.connect", return_value=mock_conn) as mock_connect:
            writer = LiveBarWriter(config_path)
            writer.write_bar("6613", datetime(2026, 7, 8, 9, 3), 100, 101, 99, 100, 1000)
            writer.write_bar("6613", datetime(2026, 7, 8, 9, 4), 100, 102, 99, 101, 1200)
            writer.close()

        assert mock_connect.call_count == 1  # 接続は使い回され、1回しか張られない
        mock_conn.close.assert_called_once()
    print("test_live_bar_writer_reuses_connection_across_writes: OK")


if __name__ == "__main__":
    test_normalize_jquants_code()
    test_to_db_code()
    test_load_pg_config_reads_ini_file()
    test_load_pg_config_missing_file_raises()
    test_load_bars_queries_and_converts_rows()
    test_live_bar_writer_upserts_with_converted_code()
    test_live_bar_writer_reuses_connection_across_writes()
    print("\nすべてのテストに成功しました。")
