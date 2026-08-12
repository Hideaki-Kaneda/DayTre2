"""
trading/ パッケージ（PositionManager, RiskManager, OrderExecutor）の
動作確認用テスト。 `python tests/test_trading.py` で実行可能。
"""

import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading import (  # noqa: E402
    DuplicateEntryError,
    OrderExecutor,
    OrderReason,
    OrderStatus,
    PositionManager,
    RiskManager,
    SimulatedBrokerClient,
)

# entry_start_time（既定09:00）以降であることが保証された、テスト用の安全な時刻。
# 個別の日次テストではこの時刻を基準にminutes等をずらして使う。
SAFE_ENTRY_TIME = datetime(2026, 7, 28, 9, 30)


def test_position_manager_duplicate_entry_blocked():
    pm = PositionManager()
    pm.open_position("9432", 100, 150.0, "ORD-1", datetime(2026, 7, 28, 9, 0))
    assert pm.has_position("9432") is True

    try:
        pm.open_position("9432", 100, 151.0, "ORD-2", datetime(2026, 7, 28, 9, 1))
        assert False, "重複エントリーが例外なく成功してしまった"
    except DuplicateEntryError:
        pass
    print("test_position_manager_duplicate_entry_blocked: OK")


def test_position_manager_close_position_pnl():
    pm = PositionManager()
    pm.open_position("9432", 100, 150.0, "ORD-1", datetime(2026, 7, 28, 9, 0))
    closed = pm.close_position("9432", 155.0, datetime(2026, 7, 28, 10, 0), OrderReason.SIGNAL)
    assert closed.realized_pnl == (155.0 - 150.0) * 100  # 500.0
    assert pm.has_position("9432") is False
    print("test_position_manager_close_position_pnl: OK", closed.realized_pnl)


def test_risk_manager_stop_loss():
    rm = RiskManager(stop_loss_pct=0.97)
    entry_price = 1000.0
    assert rm.is_stop_loss_triggered(entry_price, 970.0) is False  # ちょうど97%はまだ下回っていない
    assert rm.is_stop_loss_triggered(entry_price, 969.9) is True
    assert rm.is_stop_loss_triggered(entry_price, 1000.0) is False
    print("test_risk_manager_stop_loss: OK")


def test_risk_manager_daily_profit_target_halts_trading():
    rm = RiskManager(daily_profit_target=20_000, daily_max_loss=20_000)
    rm.start_new_trading_day(date(2026, 7, 28))

    assert rm.record_realized_pnl(5_000) is None
    assert rm.trading_halted is False

    hit = rm.record_realized_pnl(16_000)  # 累計21,000円 → 上限到達
    assert hit == "PROFIT_TARGET"
    assert rm.trading_halted is True
    assert rm.halt_reason == OrderReason.DAILY_PROFIT_TARGET
    print("test_risk_manager_daily_profit_target_halts_trading: OK")


def test_risk_manager_daily_max_loss_halts_trading():
    rm = RiskManager(daily_profit_target=20_000, daily_max_loss=20_000)
    rm.start_new_trading_day(date(2026, 7, 28))

    assert rm.record_realized_pnl(-8_000) is None
    hit = rm.record_realized_pnl(-13_000)  # 累計-21,000円 → 下限到達
    assert hit == "MAX_LOSS"
    assert rm.trading_halted is True
    assert rm.halt_reason == OrderReason.DAILY_MAX_LOSS
    print("test_risk_manager_daily_max_loss_halts_trading: OK")


def test_risk_manager_force_close_time():
    rm = RiskManager(force_close_time=time(15, 0))
    before = datetime(2026, 7, 28, 14, 59)
    at_time = datetime(2026, 7, 28, 15, 0)
    after = datetime(2026, 7, 28, 15, 1)
    assert rm.is_force_close_time(before) is False
    assert rm.is_force_close_time(at_time) is True
    assert rm.is_force_close_time(after) is True
    print("test_risk_manager_force_close_time: OK")


def test_risk_manager_push_disconnect_halts_and_forces_close():
    rm = RiskManager()
    rm.start_new_trading_day(date(2026, 7, 28))
    assert rm.should_force_close_all(datetime(2026, 7, 28, 10, 0)) is None

    rm.notify_push_disconnected_permanently()
    assert rm.trading_halted is True
    reason = rm.should_force_close_all(datetime(2026, 7, 28, 10, 0))
    assert reason == OrderReason.PUSH_DISCONNECT
    print("test_risk_manager_push_disconnect_halts_and_forces_close: OK")


def test_order_executor_entry_and_exit_flow():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(stop_loss_pct=0.97, daily_profit_target=20_000, daily_max_loss=20_000)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 150.0)
    result = executor.try_entry("9432", 150.0, now=SAFE_ENTRY_TIME)
    assert result.status == OrderStatus.FILLED
    assert pm.has_position("9432") is True
    assert broker.get_buying_power() == 1_000_000 - 150.0 * 100

    # 同一銘柄への重複エントリーはNoneが返り、発注されない
    dup = executor.try_entry("9432", 150.0, now=SAFE_ENTRY_TIME)
    assert dup is None

    # 利益確定で決済
    broker.set_current_price("9432", 160.0)
    exit_result = executor.try_exit("9432", 160.0, OrderReason.SIGNAL)
    assert exit_result.status == OrderStatus.FILLED
    assert pm.has_position("9432") is False
    assert rm.daily_state.realized_pnl == (160.0 - 150.0) * 100  # 1000.0
    print("test_order_executor_entry_and_exit_flow: OK")


def test_order_executor_rank_order_stops_when_funds_exhausted():
    # 1銘柄150円*100株=15,000円。余力32,000円なので2銘柄までしか買えない
    broker = SimulatedBrokerClient(initial_buying_power=32_000)
    pm = PositionManager()
    rm = RiskManager()
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    candidates = [("A", 150.0), ("B", 150.0), ("C", 150.0), ("D", 150.0)]
    for symbol, price in candidates:
        broker.set_current_price(symbol, price)

    results = executor.try_entries_in_rank_order(candidates, now=SAFE_ENTRY_TIME)

    assert [r.symbol for r in results] == ["A", "B"]
    assert pm.has_position("A") is True
    assert pm.has_position("B") is True
    assert pm.has_position("C") is False
    assert pm.has_position("D") is False
    print("test_order_executor_rank_order_stops_when_funds_exhausted: OK", [r.symbol for r in results])


def test_order_executor_rank_order_skips_existing_position():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager()
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    pm.open_position("A", 100, 150.0, "ORD-EXISTING", datetime(2026, 7, 28, 9, 0))

    candidates = [("A", 150.0), ("B", 150.0)]
    for symbol, price in candidates:
        broker.set_current_price(symbol, price)

    results = executor.try_entries_in_rank_order(candidates, now=SAFE_ENTRY_TIME)
    # Aは既にポジションありなのでスキップされ、Bだけ新規発注される
    assert [r.symbol for r in results] == ["B"]
    print("test_order_executor_rank_order_skips_existing_position: OK")


def test_order_executor_stop_loss_trigger():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(stop_loss_pct=0.97)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", 1000.0, now=SAFE_ENTRY_TIME)

    # 97%(970円)を下回っていないので損切なし
    broker.set_current_price("9432", 975.0)
    result = executor.check_and_apply_stop_loss("9432", 975.0)
    assert result is None
    assert pm.has_position("9432") is True

    # 97%を下回ったので損切発動
    broker.set_current_price("9432", 969.0)
    result = executor.check_and_apply_stop_loss("9432", 969.0)
    assert result is not None
    assert result.reason == OrderReason.STOP_LOSS
    assert pm.has_position("9432") is False
    assert rm.daily_state.realized_pnl < 0
    print("test_order_executor_stop_loss_trigger: OK", rm.daily_state.realized_pnl)


def test_order_executor_daily_loss_limit_forces_close_all_and_halts_new_entries():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(daily_max_loss=1_000)  # 小さい値にしてテストしやすくする
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    # 2銘柄エントリー
    broker.set_current_price("A", 100.0)
    broker.set_current_price("B", 100.0)
    executor.try_entry("A", 100.0, now=SAFE_ENTRY_TIME)
    executor.try_entry("B", 100.0, now=SAFE_ENTRY_TIME)
    assert pm.position_count() == 2

    # Aが大きく含み損 → 損切りで日次最大損失(1,000円)に到達させる
    broker.set_current_price("A", 90.0)  # (90-100)*100 = -1000
    executor.check_and_apply_stop_loss("A", 89.0)  # 97%=97円を下回る89円で損切発動

    assert rm.trading_halted is True
    assert rm.halt_reason == OrderReason.DAILY_MAX_LOSS

    # RiskManagerが全決済トリガーを出すことを確認し、OrderExecutorで全決済を実行
    broker.set_current_price("B", 100.0)
    now = datetime(2026, 7, 28, 10, 30)
    results = executor.check_forced_liquidation(now, price_lookup=lambda s: broker._prices.get(s, 100.0))
    assert any(r.symbol == "B" and r.reason == OrderReason.DAILY_MAX_LOSS for r in results)
    assert pm.position_count() == 0

    # 新規エントリーも停止されていることを確認
    broker.set_current_price("C", 100.0)
    blocked = executor.try_entry("C", 100.0, now=SAFE_ENTRY_TIME)
    assert blocked is None
    print("test_order_executor_daily_loss_limit_forces_close_all_and_halts_new_entries: OK")


def test_order_executor_force_close_time():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(force_close_time=time(15, 0))
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", 1000.0, now=SAFE_ENTRY_TIME)
    assert pm.has_position("9432") is True

    before_close = datetime(2026, 7, 28, 14, 59)
    results = executor.check_forced_liquidation(before_close, price_lookup=lambda s: 1000.0)
    assert results == []
    assert pm.has_position("9432") is True

    at_close = datetime(2026, 7, 28, 15, 0)
    results = executor.check_forced_liquidation(at_close, price_lookup=lambda s: 1005.0)
    assert len(results) == 1
    assert results[0].reason == OrderReason.FORCE_CLOSE_TIME
    assert pm.has_position("9432") is False

    # 大引け後、ポジションが既に0件の状態でタイマーが繰り返し呼ばれても
    # 何も返らない（＝ログスパムや無駄な処理が発生しない）ことを確認
    after_close = datetime(2026, 7, 28, 15, 5)
    results2 = executor.check_forced_liquidation(after_close, price_lookup=lambda s: 1005.0)
    assert results2 == []

    # 大引け後は新規エントリーも停止されることを確認
    broker.set_current_price("7203", 500.0)
    blocked = executor.try_entry("7203", 500.0, now=datetime(2026, 7, 28, 15, 5))
    assert blocked is None
    assert rm.trading_halted is True
    assert rm.halt_reason == OrderReason.FORCE_CLOSE_TIME
    print("test_order_executor_force_close_time: OK")


def test_order_executor_push_disconnect_forces_close_all():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager()
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    for sym in ("A", "B", "C"):
        broker.set_current_price(sym, 100.0)
        executor.try_entry(sym, 100.0, now=SAFE_ENTRY_TIME)
    assert pm.position_count() == 3

    rm.notify_push_disconnected_permanently()
    results = executor.check_forced_liquidation(
        datetime(2026, 7, 28, 11, 0), price_lookup=lambda s: 100.0
    )
    assert len(results) == 3
    assert all(r.reason == OrderReason.PUSH_DISCONNECT for r in results)
    assert pm.position_count() == 0
    print("test_order_executor_push_disconnect_forces_close_all: OK")


def test_risk_manager_entry_start_time():
    rm = RiskManager(entry_start_time=time(9, 12))
    before = datetime(2026, 7, 28, 9, 11, 59)
    at_time = datetime(2026, 7, 28, 9, 12, 0)
    after = datetime(2026, 7, 28, 9, 12, 1)
    assert rm.is_entry_time_allowed(before) is False
    assert rm.is_entry_time_allowed(at_time) is True
    assert rm.is_entry_time_allowed(after) is True
    print("test_risk_manager_entry_start_time: OK")


def test_order_executor_blocks_entry_before_entry_start_time():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(entry_start_time=time(9, 12))
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 150.0)

    too_early = datetime(2026, 7, 28, 9, 5)
    blocked = executor.try_entry("9432", 150.0, now=too_early)
    assert blocked is None
    assert pm.has_position("9432") is False

    allowed_time = datetime(2026, 7, 28, 9, 12)
    result = executor.try_entry("9432", 150.0, now=allowed_time)
    assert result.status == OrderStatus.FILLED
    assert pm.has_position("9432") is True

    # 決済（損切等）はentry_start_timeの制約を受けない
    result2 = executor.check_and_apply_stop_loss("9432", 100.0)
    assert result2 is not None
    assert pm.has_position("9432") is False
    print("test_order_executor_blocks_entry_before_entry_start_time: OK")


def test_order_executor_rank_order_blocked_before_entry_start_time():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(entry_start_time=time(9, 12))
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    candidates = [("A", 100.0), ("B", 100.0)]
    for symbol, price in candidates:
        broker.set_current_price(symbol, price)

    too_early = datetime(2026, 7, 28, 9, 0)
    results = executor.try_entries_in_rank_order(candidates, now=too_early)
    assert results == []
    assert pm.position_count() == 0

    allowed_time = datetime(2026, 7, 28, 9, 12)
    results2 = executor.try_entries_in_rank_order(candidates, now=allowed_time)
    assert len(results2) == 2
    print("test_order_executor_rank_order_blocked_before_entry_start_time: OK")


def test_position_manager_update_high_water_mark():
    pm = PositionManager()
    pm.open_position("9432", 100, 1000.0, "ORD-1", SAFE_ENTRY_TIME)
    assert pm.get_position("9432").high_water_mark == 1000.0

    pm.update_high_water_mark("9432", 1050.0)
    assert pm.get_position("9432").high_water_mark == 1050.0

    # 現在値が最高値を下回っても更新されない（下方修正はしない）
    pm.update_high_water_mark("9432", 1020.0)
    assert pm.get_position("9432").high_water_mark == 1050.0
    print("test_position_manager_update_high_water_mark: OK")


def test_order_executor_trailing_stop_triggers_on_pullback_from_high():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(trailing_multiplier=2.0)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", 1000.0, now=SAFE_ENTRY_TIME)
    ar_value = 10.0  # トレール幅 = 10 * 2 = 20

    # 上昇：最高値を更新。トレール幅(20)以内の下落なのでまだ発動しない
    pm.update_high_water_mark("9432", 1030.0)
    result = executor.check_and_apply_trailing_stop("9432", 1015.0, ar_value)
    assert result is None
    assert pm.has_position("9432") is True

    # さらに下落し、最高値1030から20を超えて下がったら発動するはず（1030-20=1010）
    broker.set_current_price("9432", 1005.0)
    result2 = executor.check_and_apply_trailing_stop("9432", 1005.0, ar_value)
    assert result2 is not None
    assert result2.reason == OrderReason.TRAILING_STOP
    assert pm.has_position("9432") is False
    print("test_order_executor_trailing_stop_triggers_on_pullback_from_high: OK")


def test_order_executor_trailing_stop_does_nothing_without_ar_value():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(trailing_multiplier=2.0)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", 1000.0, now=SAFE_ENTRY_TIME)

    # ARがまだ確定していない(None)場合は判定自体を行わない
    result = executor.check_and_apply_trailing_stop("9432", 500.0, ar_value=None)
    assert result is None
    assert pm.has_position("9432") is True
    print("test_order_executor_trailing_stop_does_nothing_without_ar_value: OK")


def test_risk_manager_reentry_cooldown_blocks_then_releases():
    rm = RiskManager(reentry_cooldown_bars=3)
    assert rm.is_reentry_allowed("9432") is True  # 決済前は制約なし

    rm.record_exit("9432")
    assert rm.is_reentry_allowed("9432") is False

    rm.tick_reentry_cooldown("9432")  # 1本目
    assert rm.is_reentry_allowed("9432") is False
    rm.tick_reentry_cooldown("9432")  # 2本目
    assert rm.is_reentry_allowed("9432") is False
    rm.tick_reentry_cooldown("9432")  # 3本目でクールダウン解除
    assert rm.is_reentry_allowed("9432") is True
    print("test_risk_manager_reentry_cooldown_blocks_then_releases: OK")


def test_risk_manager_reentry_cooldown_disabled_when_zero():
    rm = RiskManager(reentry_cooldown_bars=0)
    rm.record_exit("9432")
    assert rm.is_reentry_allowed("9432") is True  # 0本なら即座に再エントリー可
    print("test_risk_manager_reentry_cooldown_disabled_when_zero: OK")


def test_risk_manager_reentry_cooldown_resets_on_new_trading_day():
    rm = RiskManager(reentry_cooldown_bars=3)
    rm.record_exit("9432")
    assert rm.is_reentry_allowed("9432") is False

    rm.start_new_trading_day(date(2026, 7, 29))
    assert rm.is_reentry_allowed("9432") is True
    print("test_risk_manager_reentry_cooldown_resets_on_new_trading_day: OK")


def test_order_executor_blocks_reentry_during_cooldown_after_exit():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(reentry_cooldown_bars=3)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)

    broker.set_current_price("9432", 100.0)
    executor.try_entry("9432", 100.0, now=SAFE_ENTRY_TIME)
    executor.try_exit("9432", 105.0, OrderReason.SIGNAL)
    assert pm.has_position("9432") is False

    # クールダウン中は再エントリーできない
    blocked = executor.try_entry("9432", 100.0, now=SAFE_ENTRY_TIME)
    assert blocked is None
    assert pm.has_position("9432") is False

    # 3本分のバー経過後は再エントリーできる
    rm.tick_reentry_cooldown("9432")
    rm.tick_reentry_cooldown("9432")
    rm.tick_reentry_cooldown("9432")
    allowed = executor.try_entry("9432", 100.0, now=SAFE_ENTRY_TIME)
    assert allowed is not None
    assert allowed.status == OrderStatus.FILLED
    assert pm.has_position("9432") is True
    print("test_order_executor_blocks_reentry_during_cooldown_after_exit: OK")


def test_order_executor_rank_order_skips_symbol_in_cooldown():
    broker = SimulatedBrokerClient(initial_buying_power=1_000_000)
    pm = PositionManager()
    rm = RiskManager(reentry_cooldown_bars=3)
    rm.start_new_trading_day(date(2026, 7, 28))
    executor = OrderExecutor(broker, pm, rm, shares_per_symbol=100)
    rm.record_exit("A")  # Aだけクールダウン中とみなす

    candidates = [("A", 100.0), ("B", 100.0)]
    for symbol, price in candidates:
        broker.set_current_price(symbol, price)

    results = executor.try_entries_in_rank_order(candidates, now=SAFE_ENTRY_TIME)
    assert [r.symbol for r in results] == ["B"]
    print("test_order_executor_rank_order_skips_symbol_in_cooldown: OK")


if __name__ == "__main__":
    test_position_manager_duplicate_entry_blocked()
    test_position_manager_close_position_pnl()
    test_risk_manager_stop_loss()
    test_risk_manager_daily_profit_target_halts_trading()
    test_risk_manager_daily_max_loss_halts_trading()
    test_risk_manager_force_close_time()
    test_risk_manager_push_disconnect_halts_and_forces_close()
    test_order_executor_entry_and_exit_flow()
    test_order_executor_rank_order_stops_when_funds_exhausted()
    test_order_executor_rank_order_skips_existing_position()
    test_order_executor_stop_loss_trigger()
    test_order_executor_daily_loss_limit_forces_close_all_and_halts_new_entries()
    test_order_executor_force_close_time()
    test_order_executor_push_disconnect_forces_close_all()
    test_risk_manager_entry_start_time()
    test_order_executor_blocks_entry_before_entry_start_time()
    test_order_executor_rank_order_blocked_before_entry_start_time()
    test_position_manager_update_high_water_mark()
    test_order_executor_trailing_stop_triggers_on_pullback_from_high()
    test_order_executor_trailing_stop_does_nothing_without_ar_value()
    test_risk_manager_reentry_cooldown_blocks_then_releases()
    test_risk_manager_reentry_cooldown_disabled_when_zero()
    test_risk_manager_reentry_cooldown_resets_on_new_trading_day()
    test_order_executor_blocks_reentry_during_cooldown_after_exit()
    test_order_executor_rank_order_skips_symbol_in_cooldown()
    print("\nすべてのテストに成功しました。")
