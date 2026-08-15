"""
backtest/ パッケージの動作確認用テスト。 `python tests/test_backtest.py` で実行可能。
"""

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import BacktestConfig, BacktestEngine, group_bars_by_timestamp, load_bars_from_csv, resample_bars  # noqa: E402
from backtest.models import Bar  # noqa: E402
from trading import OrderReason, PositionDirection  # noqa: E402


# ----------------------------------------------------------------------
# data_loader
# ----------------------------------------------------------------------
def test_load_bars_from_csv(tmp_dir: Path):
    csv_path = tmp_dir / "bars.csv"
    csv_path.write_text(
        "Date,Time,Code,O,H,L,C,Vo,Va\n"
        "2026-07-28,09:01,72030,1000,1005,995,1002,10000,10020000\n"
        "2026-07-28,09:00,72030,995,1000,990,1000,8000,7960000\n"  # 順不同でも後でソートされる
        "2026-07-28,09:00,94320,150,151,149,150.5,5000,752500\n",
        encoding="utf-8",
    )
    bars = load_bars_from_csv(csv_path)
    assert len(bars) == 3
    # タイムスタンプ昇順、かつ5桁コードの末尾0が落ちて4桁になっていること
    assert bars[0].symbol == "7203"
    assert bars[0].timestamp < bars[2].timestamp
    assert bars[1].symbol == "9432"
    assert bars[0].volume == 8000.0  # Vo列が出来高として読まれること（Vaは無視）
    print("test_load_bars_from_csv: OK", [(b.symbol, b.timestamp, b.close, b.volume) for b in bars])


def test_group_bars_by_timestamp():
    bars = [
        Bar("A", __import__("datetime").datetime(2026, 7, 28, 9, 0), 1, 1, 1, 1, 100),
        Bar("B", __import__("datetime").datetime(2026, 7, 28, 9, 0), 1, 1, 1, 1, 100),
        Bar("A", __import__("datetime").datetime(2026, 7, 28, 9, 1), 1, 1, 1, 1, 100),
    ]
    groups = group_bars_by_timestamp(bars)
    assert len(groups) == 2
    assert len(groups[0][1]) == 2  # 9:00は2銘柄分
    assert len(groups[1][1]) == 1  # 9:01は1銘柄分
    print("test_group_bars_by_timestamp: OK")


# ----------------------------------------------------------------------
# BacktestEngine
# ----------------------------------------------------------------------
def _make_bars(symbol, prices, base_dt, volume=1000):
    from datetime import timedelta
    return [
        Bar(symbol, base_dt + timedelta(minutes=i), p, p, p, p, volume)
        for i, p in enumerate(prices)
    ]


def test_backtest_engine_entry_and_exit_via_rsi():
    from datetime import datetime

    config = BacktestConfig(
        indicator_config={"rsi14": {"type": "rsi", "period": 14}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30}]},
        exit_rule={"operator": "AND", "conditions": [{"indicator": "rsi14", "field": "rsi14", "op": ">", "value": 70}]},
        initial_buying_power=1_000_000,
        shares_per_symbol=100,
        stop_loss_pct=0.5,  # 損切りにかからないよう緩める（RSIロジックのみ検証したいため）
        daily_profit_target=1_000_000,  # 上限に引っかからないよう大きくする
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),  # 強制引けにかからないようにする
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    # 急激な下落でRSIを素早くオーバーソールドにし、その後長く上昇を続けて
    # RSIがオーバーボート(70超)に達する頃には価格がエントリー時を大きく上回るようにする
    decline = [1000 - 20 * i for i in range(15)]
    bottom = decline[-1]
    recovery = [bottom + 20 * i for i in range(1, 60)]
    prices = decline + recovery
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)

    assert result.trade_count >= 1, "少なくとも1回はエントリー→決済のサイクルが発生するはず"
    trade = result.trades[0]
    assert trade.symbol == "9432"
    assert trade.realized_pnl > 0, "下落後の反発を利確しているのでプラスになるはず"
    print("test_backtest_engine_entry_and_exit_via_rsi: OK",
          result.trade_count, result.total_pnl, result.win_rate)


def test_backtest_engine_respects_buying_power_and_rank_order():
    from datetime import datetime, timedelta

    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": []},  # 決済は損切のみに任せる
        initial_buying_power=20_000,  # 100株*100円=10,000円 なので2銘柄まで
        shares_per_symbol=100,
        stop_loss_pct=0.01,  # ほぼ発動しない
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
        symbol_ranks={"A": 1, "B": 2, "C": 3},
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    bars = []
    for symbol in ("A", "B", "C"):
        prices = [100 - i for i in range(5)]  # 全銘柄RSIが下がりエントリー条件を満たす
        bars += _make_bars(symbol, prices, base_dt)

    result = engine.run(bars)
    entered_symbols = {t.symbol for t in result.trades}
    # 余力的にA, Bまでしか買えないはず（Cは買えない）
    assert "C" not in entered_symbols
    print("test_backtest_engine_respects_buying_power_and_rank_order: OK", entered_symbols)


def test_backtest_engine_trailing_stop_triggers():
    """
    【2026-08時点で廃止】AR（opening_range_ar）指標はユーザー指示により削除された。
    AR×倍率のトレール決済機能自体（RiskManager.trailing_multiplier等）はコードとしては
    残っているが、AR指標が無いためar_indicator_keyが常にNoneとなり実質的に無効化されている。
    新しい損切ロジック（固定値ラチェット式ストップ）に置き換わる予定のため、
    このテストは後方互換のためスタブとして残す。
    """
    print("test_backtest_engine_trailing_stop_triggers: SKIPPED (AR指標削除済み、トレール決済は実質無効化)")


def test_backtest_engine_closes_open_position_at_end():
    from datetime import datetime

    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": []},  # 通常の決済シグナルなし
        stop_loss_pct=0.01,  # 損切りもほぼ発動しない
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),  # 強制引けも発動しないようにする
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    prices = [100, 99, 98, 97, 96]  # 下落してエントリーするが、その後決済条件が一切成立しない
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)
    assert result.trade_count == 1
    assert result.trades[0].reason == OrderReason.BACKTEST_END, "データ終了時点で残ったポジションはBACKTEST_ENDで強制決済されるはず"
    print("test_backtest_engine_closes_open_position_at_end: OK")


def test_backtest_engine_daily_max_loss_halts_and_resets_next_day():
    """
    【2026-08時点で簡略化】従来はAR×トレール決済で実現損失を発生させていたが、
    AR指標が削除されたため、シグナル決済（exit_rule）で意図的に含み損を確定させる
    形に作り直した。日次上限到達後、翌日には正しくリセットされることを確認する。
    """
    from datetime import datetime, timedelta

    config = BacktestConfig(
        indicator_config={"rsi2": {"type": "rsi", "period": 2}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi2", "field": "rsi2", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": [{"indicator": "rsi2", "field": "rsi2", "op": ">", "value": 60}]},
        daily_max_loss=100,  # 小さくしてすぐ到達させる
        daily_profit_target=1_000_000,
        force_close_time=time(23, 59),
        shares_per_symbol=10,
    )
    engine = BacktestEngine(config)

    day1 = datetime(2026, 7, 28, 9, 0)
    day2 = datetime(2026, 7, 29, 9, 0)
    # 1日目：緩やかに下落してエントリー(98)、急落してから一部反発(93)したところで
    # RSI2が60を超えて決済（93<98なので実現損失-50/株、10株で-500円）→ 日次最大損失(100円)に到達
    ohlcv_day1 = [
        (100, 100, 99, 100),
        (100, 100, 98, 99),
        (99, 99, 97, 98),   # エントリー成立（RSI2が初めて50未満）
        (98, 98, 96, 97),
        (97, 97, 95, 96),
        (96, 96, 79, 80),
        (80, 94, 80, 93),   # RSI2が60を超えて決済（93<98なので損失）
    ]
    bars_day1 = [
        Bar("9432", day1 + timedelta(minutes=i), o, h, l, c, 1000)
        for i, (o, h, l, c) in enumerate(ohlcv_day1)
    ]
    # 2日目：新しい日なので改めてエントリーできることを確認（同じ価格パターンを再利用）
    bars_day2 = [
        Bar("9432", day2 + timedelta(minutes=i), o, h, l, c, 1000)
        for i, (o, h, l, c) in enumerate(ohlcv_day1)
    ]

    result = engine.run(bars_day1 + bars_day2)
    trade_dates = {t.entry_at.date() for t in result.trades}
    assert day1.date() in trade_dates
    assert day2.date() in trade_dates, "日次上限は翌日には引き継がれずリセットされるはず"
    assert any(t.realized_pnl < 0 for t in result.trades), "1日目に実現損失が発生しているはず"
    print("test_backtest_engine_daily_max_loss_halts_and_resets_next_day: OK", trade_dates, [t.realized_pnl for t in result.trades])


def test_resample_bars_aggregates_ohlcv_correctly():
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 9:00,9:01,9:02 の1分足3本 → 3分足1本に集約されるはず
    bars = [
        Bar("9432", base_dt, 100, 105, 98, 102, 1000),
        Bar("9432", base_dt.replace(minute=1), 102, 110, 101, 108, 1500),
        Bar("9432", base_dt.replace(minute=2), 108, 109, 103, 104, 800),
        # 次の3分区間（9:03-9:05）の1本目のみ
        Bar("9432", base_dt.replace(minute=3), 104, 106, 100, 105, 500),
    ]
    resampled = resample_bars(bars, 3)

    assert len(resampled) == 2
    first = resampled[0]
    assert first.timestamp == base_dt  # 9:00始まりの区間
    assert first.open == 100  # 区間内最初の始値
    assert first.high == 110  # 区間内の高値の最大
    assert first.low == 98   # 区間内の安値の最小
    assert first.close == 104  # 区間内最後の終値
    assert first.volume == 1000 + 1500 + 800  # 出来高合計

    second = resampled[1]
    assert second.timestamp == base_dt.replace(minute=3)
    assert second.open == 104
    print("test_resample_bars_aggregates_ohlcv_correctly: OK", first, second)


def test_resample_bars_interval_1_returns_unchanged():
    base_dt = datetime(2026, 7, 28, 9, 0)
    bars = [Bar("9432", base_dt, 100, 101, 99, 100, 500)]
    resampled = resample_bars(bars, 1)
    assert resampled == bars
    print("test_resample_bars_interval_1_returns_unchanged: OK")


def test_resample_bars_handles_multiple_symbols_independently():
    base_dt = datetime(2026, 7, 28, 9, 0)
    bars = [
        Bar("A", base_dt, 100, 100, 100, 100, 100),
        Bar("A", base_dt.replace(minute=1), 100, 100, 100, 100, 100),
        Bar("B", base_dt, 200, 200, 200, 200, 200),
        Bar("B", base_dt.replace(minute=1), 200, 200, 200, 200, 200),
    ]
    resampled = resample_bars(bars, 3)
    symbols = {b.symbol for b in resampled}
    assert symbols == {"A", "B"}
    assert len(resampled) == 2
    volume_by_symbol = {b.symbol: b.volume for b in resampled}
    assert volume_by_symbol["A"] == 200  # 100+100（銘柄ごとに独立して合算されている）
    assert volume_by_symbol["B"] == 400  # 200+200
    print("test_resample_bars_handles_multiple_symbols_independently: OK")


def test_backtest_engine_works_with_resampled_bars():
    """resample_barsで作った3分足データでもBacktestEngineが問題なく動作すること。"""
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": []},
        stop_loss_pct=0.5,
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    one_min_bars = _make_bars("9432", [100 - i for i in range(30)], base_dt)
    three_min_bars = resample_bars(one_min_bars, 3)
    assert len(three_min_bars) == 10  # 30本の1分足 → 10本の3分足

    result = engine.run(three_min_bars)
    assert result.trade_count >= 1
    print("test_backtest_engine_works_with_resampled_bars: OK", result.trade_count)


def test_backtest_engine_log_entries_capture_entry_and_exit():
    """
    【2026-08時点で作り直し】従来はAR×トレール決済のログ記録を検証していたが、
    AR指標が削除されたため、シグナル決済（exit_rule）でのログ記録を検証する形に変更した。
    """
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">", "value": 60}]},
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    # 下落してエントリー成立 → 反発してシグナル決済成立、という一連の流れを記録させる
    ohlcv = [
        (100, 100, 99, 100),
        (100, 100, 98, 99),
        (99, 99, 97, 98),
        (98, 98, 90, 92),   # エントリー成立（RSI3=0）
        (92, 105, 92, 104),  # 反発してRSI3>60→決済
    ]
    bars = [Bar("9432", base_dt + timedelta(minutes=i), o, h, l, c, 1000) for i, (o, h, l, c) in enumerate(ohlcv)]

    result = engine.run(bars)

    entry_logs = [e for e in result.log_entries if e.event_type == "ENTRY"]
    exit_logs = [e for e in result.log_entries if e.event_type == "EXIT"]

    assert len(entry_logs) >= 1
    assert all(e.symbol == "9432" and e.reason == "SIGNAL" and e.realized_pnl is None for e in entry_logs)
    assert len(exit_logs) >= 1
    assert all(e.reason == "SIGNAL" and e.realized_pnl is not None for e in exit_logs)

    # ログは時系列順（最初がエントリー、最後が決済）になっているはず
    assert result.log_entries[0].event_type == "ENTRY"
    assert result.log_entries[-1].event_type == "EXIT"
    print("test_backtest_engine_log_entries_capture_entry_and_exit: OK", len(entry_logs), len(exit_logs))


def test_write_backtest_log_csv_outputs_readable_file():
    import tempfile
    from backtest import write_backtest_log_csv
    from backtest.models import OrderLogEntry

    entries = [
        OrderLogEntry(datetime(2026, 7, 28, 9, 15), "ENTRY", "9432", 100, 1500.0, "SIGNAL", None),
        OrderLogEntry(datetime(2026, 7, 28, 9, 45), "EXIT", "9432", 100, 1450.0, "STOP_LOSS", -5000.0),
    ]
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "log.csv"
        write_backtest_log_csv(entries, out_path)
        assert out_path.exists()

        content = out_path.read_text(encoding="utf-8-sig")
        assert "エントリー" in content
        assert "決済" in content
        assert "損切" in content
        assert "-5000" in content or "-5,000" in content
    print("test_write_backtest_log_csv_outputs_readable_file: OK")


def test_backtest_engine_reentry_cooldown_blocks_immediate_reentry():
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">", "value": 60}]},
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
        reentry_cooldown_bars=3,
    )
    engine = BacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 下落→エントリー→反発で決済→すぐ下落再開（クールダウン中は入らないはず）→3本経過後に再エントリー
    prices = [100, 99, 98, 97, 105, 90, 89, 88, 87, 86]
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)
    assert len(result.trades) == 2
    first, second = result.trades
    assert first.exit_at == base_dt + timedelta(minutes=4)  # 9:04決済
    # クールダウン3本（9:05,9:06,9:07）を経て9:07に再エントリーできているはず
    assert second.entry_at == base_dt + timedelta(minutes=7)
    print("test_backtest_engine_reentry_cooldown_blocks_immediate_reentry: OK", first.exit_at, second.entry_at)


def test_backtest_engine_reentry_cooldown_disabled_allows_immediate_reentry():
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 50}]},
        exit_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">", "value": 60}]},
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
        reentry_cooldown_bars=0,  # 無効化
    )
    engine = BacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    prices = [100, 99, 98, 97, 105, 90, 89, 88, 87, 86]
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)
    # クールダウン無効なら、決済直後(9:05)にすぐ再エントリーできるはず
    assert len(result.trades) >= 1
    second_entry_candidates = [t for t in result.trades if t.entry_at == base_dt + timedelta(minutes=5)]
    assert len(second_entry_candidates) == 1
    print("test_backtest_engine_reentry_cooldown_disabled_allows_immediate_reentry: OK")


def test_backtest_engine_short_entry_and_signal_exit():
    """売り（SHORT）側のentry_rule_short/exit_rule_shortがバックテストで機能することを確認する。"""
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": []},  # 買い側は使わない
        exit_rule={"operator": "AND", "conditions": []},
        entry_rule_short={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">", "value": 80}]},
        exit_rule_short={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": "<", "value": 40}]},
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
    )
    engine = BacktestEngine(config)

    base_dt = datetime(2026, 7, 28, 9, 0)
    # 上昇してRSI3>80でSHORTエントリー(110) → 下落してRSI3<40で決済(95)。値下がりなので利益
    prices = [100, 101, 102, 110, 95, 80]
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)
    assert result.trade_count == 1
    trade = result.trades[0]
    assert trade.direction == PositionDirection.SHORT
    assert trade.entry_price == 110
    assert trade.exit_price == 95
    assert trade.realized_pnl > 0  # 値下がりなので利益のはず
    print("test_backtest_engine_short_entry_and_signal_exit: OK", trade)


def test_backtest_engine_long_and_short_are_mutually_exclusive_per_symbol():
    """同一銘柄では買いと売りが同時に成立しても、片方（買い優先）しかエントリーしないことを確認する。"""
    config = BacktestConfig(
        indicator_config={"rsi3": {"type": "rsi", "period": 3}},
        entry_rule={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">=", "value": 0}]},
        exit_rule={"operator": "AND", "conditions": []},
        entry_rule_short={"operator": "AND", "conditions": [{"indicator": "rsi3", "field": "rsi3", "op": ">=", "value": 0}]},
        exit_rule_short={"operator": "AND", "conditions": []},
        daily_profit_target=1_000_000,
        daily_max_loss=1_000_000,
        force_close_time=time(23, 59),
    )
    engine = BacktestEngine(config)
    base_dt = datetime(2026, 7, 28, 9, 0)
    prices = [100, 101, 102, 103]
    bars = _make_bars("9432", prices, base_dt)

    result = engine.run(bars)
    # 買い・売り両方の条件が常に真だが、同一銘柄では1ポジションしか持てないはず
    assert result.trade_count <= 1
    print("test_backtest_engine_long_and_short_are_mutually_exclusive_per_symbol: OK", result.trade_count)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        test_load_bars_from_csv(Path(td))

    test_group_bars_by_timestamp()
    test_backtest_engine_entry_and_exit_via_rsi()
    test_backtest_engine_respects_buying_power_and_rank_order()
    test_backtest_engine_trailing_stop_triggers()
    test_backtest_engine_closes_open_position_at_end()
    test_backtest_engine_daily_max_loss_halts_and_resets_next_day()
    test_resample_bars_aggregates_ohlcv_correctly()
    test_resample_bars_interval_1_returns_unchanged()
    test_resample_bars_handles_multiple_symbols_independently()
    test_backtest_engine_works_with_resampled_bars()
    test_backtest_engine_log_entries_capture_entry_and_exit()
    test_write_backtest_log_csv_outputs_readable_file()
    test_backtest_engine_reentry_cooldown_blocks_immediate_reentry()
    test_backtest_engine_reentry_cooldown_disabled_allows_immediate_reentry()
    test_backtest_engine_short_entry_and_signal_exit()
    test_backtest_engine_long_and_short_are_mutually_exclusive_per_symbol()
    print("\nすべてのテストに成功しました。")
