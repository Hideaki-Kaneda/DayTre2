"""
candle_strategy/ パッケージの動作確認用テスト。 `python tests/test_candle_strategy.py` で実行可能。
"""

import sys
from datetime import date, datetime, timedelta, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.models import Bar  # noqa: E402
from candle_strategy import (  # noqa: E402
    CandleBacktestConfig,
    CandleBacktestEngine,
    CandleDirection,
    CandleOrderExecutor,
    CandleOrderReason,
    CandleOrderStatus,
    CandlePositionManager,
    CandleStrategyEngine,
    SimulatedMarginBrokerClient,
    bar_direction,
)


def _bar(symbol, ts, o, h, l, c, v=1000):
    return Bar(symbol=symbol, timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


# ----------------------------------------------------------------------
# bar_direction / CandleStrategyEngine
# ----------------------------------------------------------------------
def test_bar_direction_classifies_correctly():
    base_dt = datetime(2026, 7, 28, 9, 0)
    assert bar_direction(_bar("A", base_dt, 100, 105, 99, 103)) == CandleDirection.LONG
    assert bar_direction(_bar("A", base_dt, 103, 105, 99, 100)) == CandleDirection.SHORT
    assert bar_direction(_bar("A", base_dt, 100, 105, 99, 100)) == CandleDirection.NEUTRAL
    print("test_bar_direction_classifies_correctly: OK")


def test_strategy_engine_entry_after_n_consecutive_bullish_bars():
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    closes = [(100, 102), (102, 104), (104, 106)]  # 3本連続陽線
    for i, (o, c) in enumerate(closes):
        engine.update(_bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c))
        if i < 2:
            assert engine.evaluate_entry("9432") is None
    assert engine.evaluate_entry("9432") == CandleDirection.LONG
    print("test_strategy_engine_entry_after_n_consecutive_bullish_bars: OK")


def test_strategy_engine_entry_after_n_consecutive_bearish_bars():
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    closes = [(106, 104), (104, 102), (102, 100)]  # 3本連続陰線
    for i, (o, c) in enumerate(closes):
        engine.update(_bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c))
    assert engine.evaluate_entry("9432") == CandleDirection.SHORT
    print("test_strategy_engine_entry_after_n_consecutive_bearish_bars: OK")


def test_strategy_engine_streak_resets_on_direction_change():
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    bars = [(100, 102), (102, 104), (104, 103), (103, 105), (105, 107)]  # 2連続陽線→陰線→2連続陽線
    for i, (o, c) in enumerate(bars):
        engine.update(_bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c))
        assert engine.evaluate_entry("9432") is None  # 3本連続に達する瞬間がないはず
    print("test_strategy_engine_streak_resets_on_direction_change: OK")


def test_strategy_engine_resets_on_new_session():
    engine = CandleStrategyEngine(consecutive_bars_n=2)
    day1 = datetime(2026, 7, 28, 9, 0)
    engine.update(_bar("9432", day1, 100, 102, 99, 102))
    engine.update(_bar("9432", day1 + timedelta(minutes=3), 102, 104, 101, 104))
    assert engine.evaluate_entry("9432") == CandleDirection.LONG

    day2 = datetime(2026, 7, 29, 9, 0)
    engine.update(_bar("9432", day2, 104, 106, 103, 106))
    assert engine.evaluate_entry("9432") is None  # 新セッションでリセットされ1本目のみ
    print("test_strategy_engine_resets_on_new_session: OK")


def test_strategy_engine_does_not_refire_beyond_exact_n():
    """
    N本を超えて連続しても再発火しないことを確認する
    （count>=Nで判定すると、決済直後の同一バーで即座に再エントリーしてしまうバグがあったため）。
    """
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    closes = [(100, 102), (102, 104), (104, 106), (106, 108), (108, 110)]  # 5本連続陽線
    fired_at = []
    for i, (o, c) in enumerate(closes):
        engine.update(_bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c))
        if engine.evaluate_entry("9432") is not None:
            fired_at.append(i)
    assert fired_at == [2]  # 3本目（インデックス2）でのみ発火するはず
    print("test_strategy_engine_does_not_refire_beyond_exact_n: OK")


# ----------------------------------------------------------------------
# CandlePositionManager / CandleOrderExecutor
# ----------------------------------------------------------------------
def test_position_manager_short_pnl_is_profitable_on_price_decline():
    pm = CandlePositionManager()
    pm.open_position("9432", CandleDirection.SHORT, 100, 1000.0, "ORD-1", datetime(2026, 7, 28, 9, 0))
    closed = pm.close_position("9432", 950.0, datetime(2026, 7, 28, 9, 10), CandleOrderReason.TAKE_PROFIT)
    assert closed.realized_pnl == (1000.0 - 950.0) * 100  # 空売りは値下がりが利益
    print("test_position_manager_short_pnl_is_profitable_on_price_decline: OK", closed.realized_pnl)


def test_order_executor_long_stop_loss_priority_over_reverse_bars():
    """同時に損切とM本条件を満たす場合、損切が優先されることを確認する。"""
    broker = SimulatedMarginBrokerClient(initial_buying_power=1_000_000)
    pm = CandlePositionManager()
    executor = CandleOrderExecutor(broker, pm, shares_per_symbol=100, stop_loss_pct=0.015, take_profit_pct=0.035, reverse_bars_m=1)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", CandleDirection.LONG, 1000.0)
    pm.record_reverse_bar("9432")  # M=1をすでに満たす状態にしておく

    broker.set_current_price("9432", 980.0)  # -2% で損切ラインも下回る
    result = executor.check_exit_conditions("9432", 980.0)
    assert result is not None
    assert result.reason == CandleOrderReason.STOP_LOSS  # 逆足条件も満たすが損切が優先されるはず
    print("test_order_executor_long_stop_loss_priority_over_reverse_bars: OK")


def test_order_executor_long_take_profit_priority_over_reverse_bars():
    broker = SimulatedMarginBrokerClient(initial_buying_power=1_000_000)
    pm = CandlePositionManager()
    executor = CandleOrderExecutor(broker, pm, shares_per_symbol=100, stop_loss_pct=0.015, take_profit_pct=0.035, reverse_bars_m=1)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", CandleDirection.LONG, 1000.0)
    pm.record_reverse_bar("9432")

    broker.set_current_price("9432", 1040.0)  # +4% で利確ラインを上回る（損切には該当しない）
    result = executor.check_exit_conditions("9432", 1040.0)
    assert result is not None
    assert result.reason == CandleOrderReason.TAKE_PROFIT
    print("test_order_executor_long_take_profit_priority_over_reverse_bars: OK")


def test_order_executor_reverse_bars_triggers_when_no_pct_condition_met():
    broker = SimulatedMarginBrokerClient(initial_buying_power=1_000_000)
    pm = CandlePositionManager()
    executor = CandleOrderExecutor(broker, pm, shares_per_symbol=100, stop_loss_pct=0.015, take_profit_pct=0.035, reverse_bars_m=2)

    broker.set_current_price("9432", 1000.0)
    executor.try_entry("9432", CandleDirection.LONG, 1000.0)

    broker.set_current_price("9432", 1005.0)  # 損益ともに閾値未満
    assert executor.check_exit_conditions("9432", 1005.0) is None

    pm.record_reverse_bar("9432")
    assert executor.check_exit_conditions("9432", 1005.0) is None  # まだ1本目

    pm.record_reverse_bar("9432")
    result = executor.check_exit_conditions("9432", 1005.0)  # 2本目で決済
    assert result is not None
    assert result.reason == CandleOrderReason.REVERSE_BARS
    print("test_order_executor_reverse_bars_triggers_when_no_pct_condition_met: OK")


def test_order_executor_short_entry_and_exit():
    broker = SimulatedMarginBrokerClient(initial_buying_power=1_000_000)
    pm = CandlePositionManager()
    executor = CandleOrderExecutor(broker, pm, shares_per_symbol=100, stop_loss_pct=0.015, take_profit_pct=0.035, reverse_bars_m=2)

    broker.set_current_price("9432", 1000.0)
    result = executor.try_entry("9432", CandleDirection.SHORT, 1000.0)
    assert result.status == CandleOrderStatus.FILLED
    assert pm.get_position("9432").direction == CandleDirection.SHORT

    # SHORTなので値下がりが利確（-3.5%以上下落）
    broker.set_current_price("9432", 960.0)
    exit_result = executor.check_exit_conditions("9432", 960.0)
    assert exit_result is not None
    assert exit_result.reason == CandleOrderReason.TAKE_PROFIT
    assert pm.has_position("9432") is False
    print("test_order_executor_short_entry_and_exit: OK")


def test_order_executor_respects_external_has_position():
    broker = SimulatedMarginBrokerClient(initial_buying_power=1_000_000)
    pm = CandlePositionManager()
    executor = CandleOrderExecutor(
        broker, pm, shares_per_symbol=100, external_has_position=lambda s: s == "9432",
    )
    broker.set_current_price("9432", 1000.0)
    result = executor.try_entry("9432", CandleDirection.LONG, 1000.0)
    assert result is None
    assert pm.has_position("9432") is False
    print("test_order_executor_respects_external_has_position: OK")


# ----------------------------------------------------------------------
# CandleBacktestEngine
# ----------------------------------------------------------------------
def test_backtest_engine_long_entry_and_take_profit_end_to_end():
    config = CandleBacktestConfig(
        consecutive_bars_n=3, reverse_bars_m=2, take_profit_pct=0.035, stop_loss_pct=0.015,
        force_close_time=time(23, 59),
    )
    engine = CandleBacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 3本連続陽線でエントリー、その後大きく上昇して利確
    ohlc = [
        (100, 102), (102, 104), (104, 106),  # エントリー成立（106でLONG）
        (106, 110), (110, 115),  # 115 は 106*1.035=109.71 を上回る→利確のはず
    ]
    bars = [
        Bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c, 1000)
        for i, (o, c) in enumerate(ohlc)
    ]
    result = engine.run(bars)
    assert result.trade_count == 1
    assert result.trades[0].direction == CandleDirection.LONG
    assert result.trades[0].reason == CandleOrderReason.TAKE_PROFIT
    assert result.total_pnl > 0
    print("test_backtest_engine_long_entry_and_take_profit_end_to_end: OK", result.total_pnl)


def test_backtest_engine_short_entry_and_stop_loss_end_to_end():
    config = CandleBacktestConfig(
        consecutive_bars_n=3, reverse_bars_m=2, take_profit_pct=0.035, stop_loss_pct=0.015,
        force_close_time=time(23, 59),
    )
    engine = CandleBacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 3本連続陰線でSHORTエントリー、その後上昇して損切
    ohlc = [
        (110, 108), (108, 106), (106, 104),  # エントリー成立（104でSHORT）
        (104, 108),  # 108 は 104*1.015=105.6 を上回る→損切のはず
    ]
    bars = [
        Bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c, 1000)
        for i, (o, c) in enumerate(ohlc)
    ]
    result = engine.run(bars)
    assert result.trade_count == 1
    assert result.trades[0].direction == CandleDirection.SHORT
    assert result.trades[0].reason == CandleOrderReason.STOP_LOSS
    assert result.total_pnl < 0
    print("test_backtest_engine_short_entry_and_stop_loss_end_to_end: OK", result.total_pnl)


def test_backtest_engine_force_closes_at_close_time():
    config = CandleBacktestConfig(
        consecutive_bars_n=3, reverse_bars_m=10, take_profit_pct=0.5, stop_loss_pct=0.5,
        force_close_time=time(9, 10),  # すぐ強制引けが来るようにする
    )
    engine = CandleBacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    ohlc = [(100, 102), (102, 104), (104, 106), (106, 107), (107, 108)]
    bars = [
        Bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c, 1000)
        for i, (o, c) in enumerate(ohlc)
    ]
    result = engine.run(bars)
    assert result.trade_count == 1
    assert result.trades[0].reason == CandleOrderReason.FORCE_CLOSE_TIME
    print("test_backtest_engine_force_closes_at_close_time: OK")


def test_backtest_engine_respects_external_has_position():
    config = CandleBacktestConfig(
        consecutive_bars_n=3, force_close_time=time(23, 59),
        external_has_position=lambda s: True,  # 常に既存戦略が保有中とみなす
    )
    engine = CandleBacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    ohlc = [(100, 102), (102, 104), (104, 106)]
    bars = [
        Bar("9432", base_dt + timedelta(minutes=3 * i), o, max(o, c) + 1, min(o, c) - 1, c, 1000)
        for i, (o, c) in enumerate(ohlc)
    ]
    result = engine.run(bars)
    assert result.trade_count == 0  # 既存戦略が保有中のためエントリーされないはず
    print("test_backtest_engine_respects_external_has_position: OK")


def test_strategy_engine_long_entry_blocked_by_long_upper_wick():
    """買い：N本目の上ヒゲが実体以上に長い場合はエントリーしないことを確認する。"""
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 1,2本目は通常の陽線
    engine.update(_bar("9432", base_dt, 100, 103, 99, 102))
    engine.update(_bar("9432", base_dt + timedelta(minutes=3), 102, 105, 101, 104))
    # 3本目：実体はc-o=2だが上ヒゲ(high-close)=3で実体以上（条件不成立）
    engine.update(_bar("9432", base_dt + timedelta(minutes=6), 104, 109, 103, 106))
    assert engine.evaluate_entry("9432") is None
    print("test_strategy_engine_long_entry_blocked_by_long_upper_wick: OK")


def test_strategy_engine_long_entry_allowed_with_short_upper_wick():
    """買い：N本目の上ヒゲが実体より短ければエントリーすることを確認する。"""
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    engine.update(_bar("9432", base_dt, 100, 103, 99, 102))
    engine.update(_bar("9432", base_dt + timedelta(minutes=3), 102, 105, 101, 104))
    # 3本目：実体=6-4=2、上ヒゲ=7-6=1（実体より短い）
    engine.update(_bar("9432", base_dt + timedelta(minutes=6), 104, 107, 103, 106))
    assert engine.evaluate_entry("9432") == CandleDirection.LONG
    print("test_strategy_engine_long_entry_allowed_with_short_upper_wick: OK")


def test_strategy_engine_short_entry_blocked_by_long_lower_wick():
    """売り：N本目の下ヒゲが実体以上に長い場合はエントリーしないことを確認する。"""
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    engine.update(_bar("9432", base_dt, 106, 107, 103, 104))
    engine.update(_bar("9432", base_dt + timedelta(minutes=3), 104, 105, 101, 102))
    # 3本目：実体=102-100=2、下ヒゲ(close-low)=100-95=5（実体以上）
    engine.update(_bar("9432", base_dt + timedelta(minutes=6), 102, 103, 95, 100))
    assert engine.evaluate_entry("9432") is None
    print("test_strategy_engine_short_entry_blocked_by_long_lower_wick: OK")


def test_strategy_engine_short_entry_allowed_with_short_lower_wick():
    """売り：N本目の下ヒゲが実体より短ければエントリーすることを確認する。"""
    engine = CandleStrategyEngine(consecutive_bars_n=3)
    base_dt = datetime(2026, 7, 28, 9, 0)
    engine.update(_bar("9432", base_dt, 106, 107, 103, 104))
    engine.update(_bar("9432", base_dt + timedelta(minutes=3), 104, 105, 101, 102))
    # 3本目：実体=102-100=2、下ヒゲ=100-99=1（実体より短い）
    engine.update(_bar("9432", base_dt + timedelta(minutes=6), 102, 103, 99, 100))
    assert engine.evaluate_entry("9432") == CandleDirection.SHORT
    print("test_strategy_engine_short_entry_allowed_with_short_lower_wick: OK")


if __name__ == "__main__":
    test_bar_direction_classifies_correctly()
    test_strategy_engine_entry_after_n_consecutive_bullish_bars()
    test_strategy_engine_entry_after_n_consecutive_bearish_bars()
    test_strategy_engine_streak_resets_on_direction_change()
    test_strategy_engine_resets_on_new_session()
    test_strategy_engine_does_not_refire_beyond_exact_n()
    test_strategy_engine_long_entry_blocked_by_long_upper_wick()
    test_strategy_engine_long_entry_allowed_with_short_upper_wick()
    test_strategy_engine_short_entry_blocked_by_long_lower_wick()
    test_strategy_engine_short_entry_allowed_with_short_lower_wick()
    test_position_manager_short_pnl_is_profitable_on_price_decline()
    test_order_executor_long_stop_loss_priority_over_reverse_bars()
    test_order_executor_long_take_profit_priority_over_reverse_bars()
    test_order_executor_reverse_bars_triggers_when_no_pct_condition_met()
    test_order_executor_short_entry_and_exit()
    test_order_executor_respects_external_has_position()
    test_backtest_engine_long_entry_and_take_profit_end_to_end()
    test_backtest_engine_short_entry_and_stop_loss_end_to_end()
    test_backtest_engine_force_closes_at_close_time()
    test_backtest_engine_respects_external_has_position()
    print("\nすべてのテストに成功しました。")
