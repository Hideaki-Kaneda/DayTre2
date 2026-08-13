"""
signals/ パッケージ（SignalEngine, RuleGroup）の動作確認用テスト。
`python tests/test_signal_engine.py` で実行可能。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import PriceTick  # noqa: E402
from backtest.models import Bar  # noqa: E402
from signals import RuleGroup, SignalEngine  # noqa: E402
from signals.rule_config import RuleCondition  # noqa: E402


def make_tick(symbol, price, dt, volume=None):
    return PriceTick(symbol=symbol, timestamp=dt, price=price, volume=volume)


def test_rule_condition_basic():
    cond = RuleCondition(indicator="rsi14", field_name="rsi14", op="<", value=30)
    assert cond.evaluate({"rsi14": {"rsi14": 25.0}}) is True
    assert cond.evaluate({"rsi14": {"rsi14": 35.0}}) is False
    # 指標未準備（None）の場合はFalse扱い
    assert cond.evaluate({"rsi14": {"rsi14": None}}) is False
    print("test_rule_condition_basic: OK")


def test_rule_condition_cross_reference():
    # sma_short > sma_long の判定（ゴールデンクロス的な条件）
    cond = RuleCondition(indicator="sma_short", field_name="sma5", op=">", value="sma_long.sma25")
    values = {"sma_short": {"sma5": 110.0}, "sma_long": {"sma25": 100.0}}
    assert cond.evaluate(values) is True
    values2 = {"sma_short": {"sma5": 90.0}, "sma_long": {"sma25": 100.0}}
    assert cond.evaluate(values2) is False
    print("test_rule_condition_cross_reference: OK")


def test_rule_group_and_or():
    data = {
        "operator": "AND",
        "conditions": [
            {"indicator": "a", "field": "v", "op": "<", "value": 10},
            {
                "operator": "OR",
                "conditions": [
                    {"indicator": "b", "field": "v", "op": "==", "value": "X"},
                    {"indicator": "b", "field": "v", "op": "==", "value": "Y"},
                ],
            },
        ],
    }
    rule = RuleGroup.from_dict(data)

    assert rule.evaluate({"a": {"v": 5}, "b": {"v": "X"}}) is True
    assert rule.evaluate({"a": {"v": 5}, "b": {"v": "Z"}}) is False
    assert rule.evaluate({"a": {"v": 15}, "b": {"v": "X"}}) is False
    print("test_rule_group_and_or: OK")


def test_empty_rule_never_fires():
    rule = RuleGroup(operator="AND", conditions=[])
    assert rule.evaluate({"anything": {"v": 1}}) is False
    print("test_empty_rule_never_fires: OK")


def test_signal_engine_entry_exit_flow():
    """
    example_rules.json（実運用を想定した複合条件）はそのまま実際の
    設定サンプルとして別テスト(test_example_rules_json_loads)で
    パース確認のみ行い、ここではSignalEngineの基本的な
    「Tick投入→ルール評価→SignalEvent発火」の一連の流れそのものを
    検証するため、条件同士が矛盾しないシンプルなルールを使う。
    """
    config_indicators = {
        "rsi14": {"type": "rsi", "period": 14},
    }
    entry_rule = {
        "operator": "AND",
        "conditions": [{"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30}],
    }
    exit_rule = {
        "operator": "AND",
        "conditions": [{"indicator": "rsi14", "field": "rsi14", "op": ">", "value": 70}],
    }
    engine = SignalEngine(config_indicators, entry_rule, exit_rule)

    symbol = "9999"
    base_dt = datetime(2026, 7, 28, 9, 0, 0)

    # RSIが準備できる(period+1=15件)までシグナルが出ないことを確認しつつ、
    # 一貫して下落させてRSIを押し下げる
    price = 1000.0
    fired = None
    for i in range(30):
        price -= 5  # 一貫した下落
        tick = make_tick(symbol, price, base_dt + timedelta(minutes=i))
        event = engine.process_tick(tick, has_position=False)
        if event is not None:
            fired = event
            break

    assert fired is not None, "エントリーシグナルが発生しなかった"
    assert fired.signal_type == "ENTRY"
    assert fired.symbol == symbol
    assert fired.indicator_snapshot["rsi14"]["rsi14"] < 30
    print("test_signal_engine_entry_exit_flow (entry): OK",
          fired.rule_name, fired.indicator_snapshot["rsi14"]["rsi14"])

    # ポジション保有中として、価格を反発・急騰させ exit_rule (RSI>70) を狙う
    exit_event = None
    last_dt = tick.timestamp
    for i in range(1, 30):
        price += 8  # 一貫した上昇に転換
        t = make_tick(symbol, price, last_dt + timedelta(minutes=i))
        event = engine.process_tick(t, has_position=True)
        if event is not None:
            exit_event = event
            break

    assert exit_event is not None, "決済シグナルが発生しなかった"
    assert exit_event.signal_type == "EXIT"
    assert exit_event.indicator_snapshot["rsi14"]["rsi14"] > 70
    print("test_signal_engine_entry_exit_flow (exit): OK",
          exit_event.rule_name, exit_event.indicator_snapshot["rsi14"]["rsi14"])


def test_example_rules_json_loads_and_runs_without_error():
    """
    example_rules.json（実運用を想定した複合条件のサンプル）が
    正しくパースでき、SignalEngineに投入してもエラーなく動作することを確認する
    （シグナルが実際に発火するかどうかまでは問わない）。
    """
    with open(Path(__file__).resolve().parent.parent / "config" / "example_rules.json", encoding="utf-8") as f:
        config = json.load(f)

    engine = SignalEngine(
        indicator_config=config["indicators"],
        entry_rule=config["entry_rule"],
        exit_rule=config["exit_rule"],
    )
    symbol = "9999"
    base_dt = datetime(2026, 7, 28, 9, 0, 0)
    price = 1000.0
    for i in range(40):
        price += (-3 if i % 3 else 4)
        tick = make_tick(symbol, price, base_dt + timedelta(minutes=i), volume=1000)
        engine.process_tick(tick, has_position=False)  # エラーが出ずに完走すればOK
    print("test_example_rules_json_loads_and_runs_without_error: OK")


def test_multiple_symbols_are_independent():
    config = {
        "indicators": {"sma3": {"type": "sma", "period": 3}},
        "entry_rule": {"operator": "AND", "conditions": [{"indicator": "sma3", "field": "sma3", "op": ">", "value": 0}]},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    base_dt = datetime(2026, 7, 28, 9, 0, 0)
    for i in range(3):
        engine.process_tick(make_tick("A", 100 + i, base_dt + timedelta(minutes=i)), has_position=False)

    ctx_a = engine.get_context("A")
    ctx_b = engine.get_context("B")  # まだ一度もupdateしていない
    assert ctx_a.all_ready() is True
    assert ctx_b.all_ready() is False
    print("test_multiple_symbols_are_independent: OK")


def test_warmup_symbol_makes_indicators_ready_with_flat_price():
    config = {
        "indicators": {"sma5": {"type": "sma", "period": 5}, "rsi14": {"type": "rsi", "period": 14}},
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    seed_ts = datetime(2026, 7, 28, 15, 0)  # 前日大引け想定
    count = engine.warmup_symbol("9432", seed_price=1500.0, timestamp=seed_ts)

    ctx = engine.get_context("9432")
    assert ctx.all_ready() is True
    snap = ctx.snapshot()
    assert snap["sma5"]["sma5"] == 1500.0
    assert snap["rsi14"]["rsi14"] == 50.0  # 値動きゼロなので中立値
    assert count > 0
    print("test_warmup_symbol_makes_indicators_ready_with_flat_price: OK", count, snap)


def test_warmup_then_real_ticks_continue_naturally():
    config = {
        "indicators": {"sma3": {"type": "sma", "period": 3}},
        "entry_rule": {"operator": "AND", "conditions": [{"indicator": "sma3", "field": "sma3", "op": "<", "value": 1000}]},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    seed_ts = datetime(2026, 7, 28, 15, 0)
    engine.warmup_symbol("9432", seed_price=1000.0, timestamp=seed_ts)

    # ウォームアップ直後は前日終値のみでsma3が構成されているため、
    # 寄り付きの最初のTickからすぐシグナル判定が機能する
    today_open = datetime(2026, 7, 29, 9, 0)
    event = engine.process_tick(
        PriceTick(symbol="9432", timestamp=today_open, price=900.0, volume=1000), has_position=False
    )
    # (1000+1000+900)/3 = 966.67 < 1000 なのでentry成立するはず
    assert event is not None
    assert event.signal_type == "ENTRY"
    print("test_warmup_then_real_ticks_continue_naturally: OK")


def test_warmup_symbol_from_summary_uses_precise_seed_values():
    config = {
        "indicators": {
            "rsi14": {"type": "rsi", "period": 14},
            "bb": {"type": "bollinger", "period": 20, "num_std": 2},
            "sma5": {"type": "sma", "period": 5},
        },
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])
    seed_ts = datetime(2026, 7, 28, 15, 0)

    results = engine.warmup_symbol_from_summary(
        "9432", timestamp=seed_ts,
        prev_close=1000.0, prev_rsi=35.0,
        prev_bb_upper=1040.0, prev_bb_middle=1000.0, prev_bb_lower=960.0,
    )
    assert all(results.values()), results

    ctx = engine.get_context("9432")
    assert ctx.all_ready() is True
    snap = ctx.snapshot()
    assert abs(snap["rsi14"]["rsi14"] - 35.0) < 0.01, snap
    assert abs(snap["bb"]["middle"] - 1000.0) < 0.01, snap
    assert abs(snap["bb"]["upper"] - 1040.0) < 0.01, snap
    assert snap["sma5"]["sma5"] == 1000.0
    print("test_warmup_symbol_from_summary_uses_precise_seed_values: OK", snap)


def test_warmup_symbol_from_summary_falls_back_when_missing_data():
    """RSI/BBの要約値が無い銘柄でも、前日終値だけでフォールバックしてシードできること。"""
    config = {
        "indicators": {"rsi14": {"type": "rsi", "period": 14}},
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])
    seed_ts = datetime(2026, 7, 28, 15, 0)

    results = engine.warmup_symbol_from_summary("7203", timestamp=seed_ts, prev_close=2500.0)
    assert results["rsi14"] is True
    ctx = engine.get_context("7203")
    assert ctx.all_ready() is True
    assert ctx.snapshot()["rsi14"]["rsi14"] == 50.0  # 前日終値のみ・値動きゼロなので中立
    print("test_warmup_symbol_from_summary_falls_back_when_missing_data: OK")


def test_warmup_symbol_from_bars_uses_real_data_and_becomes_ready():
    config = {
        "indicators": {"sma3": {"type": "sma", "period": 3}, "rsi3": {"type": "rsi", "period": 3}},
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    base_dt = datetime(2026, 7, 27, 9, 0)  # 前営業日のデータを想定
    closes = [1000, 1002, 1001, 1005, 1010]
    bars = [
        Bar("9432", base_dt + timedelta(minutes=i), c - 1, c + 1, c - 2, c, 1000)
        for i, c in enumerate(closes)
    ]

    results = engine.warmup_symbol_from_bars("9432", bars)
    assert results["sma3"] is True
    assert results["rsi3"] is True

    ctx = engine.get_context("9432")
    snap = ctx.snapshot()
    # 実データそのものを使っているので、直近3本の単純平均と厳密に一致するはず
    assert snap["sma3"]["sma3"] == (1001 + 1005 + 1010) / 3
    print("test_warmup_symbol_from_bars_uses_real_data_and_becomes_ready: OK", snap)


def test_warmup_symbol_from_bars_session_scoped_indicators_reset_on_new_day():
    """
    VWAP・ARのように日次リセット前提の指標は、過去日のバーを流し込むと
    その日の分だけは準備できてしまうことがあるが、本番で実際に「今日」の
    Tickが届いた瞬間にセッションが切り替わり、正しくリセットされることを確認する
    （＝過去日のリプレイが「今日」に誤って持ち越されないことの確認）。
    """
    config = {
        "indicators": {"vwap": {"type": "vwap"}, "ar": {"type": "opening_range_ar", "bar_count": 2}},
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    yesterday = datetime(2026, 7, 27, 9, 0)  # 前営業日
    bars = [Bar("9432", yesterday + timedelta(minutes=i), 1000 + i, 1001 + i, 999 + i, 1000 + i, 1000) for i in range(5)]
    results = engine.warmup_symbol_from_bars("9432", bars)
    # 前営業日1日分のリプレイなので、その日のうちはready担ってしまうことがある
    assert results["vwap"] is True
    assert results["ar"] is True

    # 「今日」の最初のTickが届いた瞬間、日付が変わるためセッションがリセットされ、
    # 前営業日ぶんの状態は正しく引き継がれない（想定どおりの挙動）
    today = datetime(2026, 7, 28, 9, 0)
    engine.update(PriceTick(symbol="9432", timestamp=today, price=1010.0, volume=500, open=1009, high=1011, low=1008))
    ctx = engine.get_context("9432")
    assert ctx.indicators["vwap"].is_ready() is True  # VWAPは1本目のTickだけでready
    assert ctx.indicators["ar"].is_ready() is False  # ARはbar_count本(2本)必要なのでまだ
    print("test_warmup_symbol_from_bars_session_scoped_indicators_reset_on_new_day: OK")


def test_warmup_symbol_from_summary_skips_already_ready_indicators():
    """
    warmup_symbol_from_bars()で既にis_ready()になった指標へ、
    warmup_symbol_from_summary()が上書きのseed()を呼ばないことを確認する。
    """
    config = {
        "indicators": {"sma3": {"type": "sma", "period": 3}},
        "entry_rule": {"operator": "AND", "conditions": []},
        "exit_rule": {"operator": "AND", "conditions": []},
    }
    engine = SignalEngine(config["indicators"], config["entry_rule"], config["exit_rule"])

    base_dt = datetime(2026, 7, 27, 9, 0)
    bars = [Bar("9432", base_dt + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100 + i, 1000) for i in range(3)]
    engine.warmup_symbol_from_bars("9432", bars)
    real_data_value = engine.get_context("9432").snapshot()["sma3"]["sma3"]  # 実データ由来の値（=(100+101+102)/3）

    # 続けて、全く異なる前日終値でsummary版ウォームアップを呼んでも上書きされないはず
    results = engine.warmup_symbol_from_summary("9432", timestamp=base_dt, prev_close=99999.0)
    assert results["sma3"] is True
    assert engine.get_context("9432").snapshot()["sma3"]["sma3"] == real_data_value
    print("test_warmup_symbol_from_summary_skips_already_ready_indicators: OK", real_data_value)


if __name__ == "__main__":
    test_rule_condition_basic()
    test_rule_condition_cross_reference()
    test_rule_group_and_or()
    test_empty_rule_never_fires()
    test_signal_engine_entry_exit_flow()
    test_example_rules_json_loads_and_runs_without_error()
    test_multiple_symbols_are_independent()
    test_warmup_symbol_makes_indicators_ready_with_flat_price()
    test_warmup_then_real_ticks_continue_naturally()
    test_warmup_symbol_from_summary_uses_precise_seed_values()
    test_warmup_symbol_from_summary_falls_back_when_missing_data()
    test_warmup_symbol_from_bars_uses_real_data_and_becomes_ready()
    test_warmup_symbol_from_bars_session_scoped_indicators_reset_on_new_day()
    test_warmup_symbol_from_summary_skips_already_ready_indicators()
    print("\nすべてのテストに成功しました。")
