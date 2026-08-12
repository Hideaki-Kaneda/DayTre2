"""
storage/ パッケージの動作確認用テスト。 `python tests/test_storage.py` で実行可能。
インメモリSQLite（":memory:"）を使うため、実ファイルを作成しない。
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import (  # noqa: E402
    AppSettingsRepository,
    BacktestRunRepository,
    ClosedTradeRepository,
    DailyPnlRepository,
    OrderRepository,
    PositionRepository,
    SignalLogRepository,
    connect,
)
from trading.models import (  # noqa: E402
    ClosedPositionResult,
    OrderReason,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
)
from trading.risk_manager import DailyPnlState  # noqa: E402
from signals.models import SignalEvent  # noqa: E402
from backtest.models import BacktestResult, EquityPoint  # noqa: E402


def test_schema_created():
    conn = connect(":memory:")
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {
        "symbols", "watchlist", "signal_log", "orders", "positions",
        "closed_trades", "daily_pnl", "app_settings", "backtest_bars", "backtest_runs",
    }
    assert expected.issubset(tables), tables - expected
    print("test_schema_created: OK")


def test_order_repository_save_and_upsert():
    conn = connect(":memory:")
    repo = OrderRepository(conn)

    result = OrderResult(
        order_id="ORD-1", symbol="9432", side=OrderSide.BUY, qty=100,
        status=OrderStatus.PENDING, reason=OrderReason.SIGNAL,
        requested_at=datetime(2026, 7, 28, 9, 0),
    )
    repo.save(result)

    filled = OrderResult(
        order_id="ORD-1", symbol="9432", side=OrderSide.BUY, qty=100,
        status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
        requested_at=datetime(2026, 7, 28, 9, 0),
        filled_at=datetime(2026, 7, 28, 9, 0, 1), filled_price=150.0,
    )
    repo.save(filled)  # 同一order_idはUPDATEされる（INSERT重複にならない）

    rows = repo.list_recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"
    assert rows[0]["filled_price"] == 150.0
    print("test_order_repository_save_and_upsert: OK")


def test_position_repository_upsert_and_remove():
    conn = connect(":memory:")
    repo = PositionRepository(conn)

    pos = Position(symbol="9432", qty=100, entry_price=150.0, entry_order_id="ORD-1",
                    entry_at=datetime(2026, 7, 28, 9, 0))
    repo.upsert_open(pos)
    assert len(repo.list_open()) == 1

    repo.remove("9432")
    assert len(repo.list_open()) == 0
    print("test_position_repository_upsert_and_remove: OK")


def test_closed_trade_repository():
    conn = connect(":memory:")
    repo = ClosedTradeRepository(conn)
    closed = ClosedPositionResult(
        symbol="9432", qty=100, entry_price=150.0, exit_price=155.0,
        entry_at=datetime(2026, 7, 28, 9, 0), exit_at=datetime(2026, 7, 28, 10, 0),
        reason=OrderReason.SIGNAL, realized_pnl=500.0,
    )
    repo.save(closed)
    rows = repo.list_by_date(date(2026, 7, 28))
    assert len(rows) == 1
    assert rows[0]["realized_pnl"] == 500.0
    print("test_closed_trade_repository: OK")


def test_signal_log_repository():
    conn = connect(":memory:")
    repo = SignalLogRepository(conn)
    event = SignalEvent(
        symbol="9432", signal_type="ENTRY", rule_name="entry_rule",
        occurred_at=datetime(2026, 7, 28, 9, 0),
        indicator_snapshot={"rsi14": {"rsi14": 28.5}},
    )
    repo.save(event)
    rows = conn.execute("SELECT * FROM signal_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["symbol_code"] == "9432"
    assert "28.5" in rows[0]["indicator_snapshot"]
    print("test_signal_log_repository: OK")


def test_daily_pnl_repository_upsert():
    conn = connect(":memory:")
    repo = DailyPnlRepository(conn)
    state = DailyPnlState(trade_date=date(2026, 7, 28), realized_pnl=1000, win_count=2, lose_count=1)
    repo.save(state)

    state.realized_pnl = 1500
    state.win_count = 3
    repo.save(state)  # 同じtrade_dateはUPDATEされる

    row = repo.get(date(2026, 7, 28))
    assert row["realized_pnl"] == 1500
    assert row["win_count"] == 3
    print("test_daily_pnl_repository_upsert: OK")


def test_app_settings_repository():
    conn = connect(":memory:")
    repo = AppSettingsRepository(conn)
    repo.set("watchlist_size", 30)
    repo.set("entry_rule", {"operator": "AND", "conditions": []})

    assert repo.get("watchlist_size") == 30
    assert repo.get("entry_rule")["operator"] == "AND"
    assert repo.get("not_exist", default="fallback") == "fallback"
    print("test_app_settings_repository: OK")


def test_backtest_run_repository():
    conn = connect(":memory:")
    repo = BacktestRunRepository(conn)
    result = BacktestResult(
        trades=[], equity_curve=[EquityPoint(timestamp=datetime(2026, 7, 28, 9, 0), cumulative_pnl=1000.0)],
        total_pnl=1000.0, win_count=3, lose_count=1, max_drawdown=200.0,
    )
    run_id = repo.save(
        result, period_from=datetime(2026, 7, 1), period_to=datetime(2026, 7, 28),
        params={"shares_per_symbol": 100},
    )
    assert run_id == 1
    rows = repo.list_recent()
    assert len(rows) == 1
    assert rows[0]["total_pnl"] == 1000.0
    print("test_backtest_run_repository: OK")


if __name__ == "__main__":
    test_schema_created()
    test_order_repository_save_and_upsert()
    test_position_repository_upsert_and_remove()
    test_closed_trade_repository()
    test_signal_log_repository()
    test_daily_pnl_repository_upsert()
    test_app_settings_repository()
    test_backtest_run_repository()
    print("\nすべてのテストに成功しました。")
