"""
indicators/ パッケージの動作確認用テスト。
pytestがあれば `pytest tests/test_indicators.py` で実行できるが、
標準ライブラリのみでも `python tests/test_indicators.py` で実行可能。
"""

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import (  # noqa: E402
    BollingerBandIndicator,
    MovingAverageIndicator,
    OpeningRangeARIndicator,
    PriceTick,
    RSIIndicator,
    VWAPIndicator,
    create_indicator,
)


def make_tick(symbol, price, volume=None, dt=None):
    return PriceTick(symbol=symbol, timestamp=dt or datetime(2026, 7, 28, 10, 0, 0), price=price, volume=volume)


def test_sma():
    ind = MovingAverageIndicator({"period": 3, "type": "sma"})
    prices = [10, 20, 30, 40]
    results = []
    for p in prices:
        ind.update(make_tick("TEST", p))
        results.append((ind.is_ready(), ind.value()))

    assert results[0][0] is False
    assert results[1][0] is False
    assert results[2][0] is True
    assert results[2][1]["sma3"] == 20.0  # (10+20+30)/3
    assert results[3][1]["sma3"] == 30.0  # (20+30+40)/3
    print("test_sma: OK", results[-1])


def test_ema():
    ind = MovingAverageIndicator({"period": 3, "type": "ema"})
    prices = [10, 20, 30, 40, 50]
    last = None
    for p in prices:
        ind.update(make_tick("TEST", p))
        last = ind.value()
    # period=3のシードSMA=(10+20+30)/3=20 → multiplier=2/(3+1)=0.5
    # 4件目(40): (40-20)*0.5+20=30
    # 5件目(50): (50-30)*0.5+30=40
    assert math.isclose(last["ema3"], 40.0), last
    print("test_ema: OK", last)


def test_rsi_all_gains_hits_100():
    ind = RSIIndicator({"period": 5})
    prices = [100, 101, 102, 103, 104, 105, 106]
    last = None
    for p in prices:
        ind.update(make_tick("TEST", p))
        last = ind.value()
    assert ind.is_ready()
    assert last["rsi5"] == 100.0, last  # 全部上昇のみなのでRSI=100
    print("test_rsi_all_gains_hits_100: OK", last)


def test_rsi_mixed():
    ind = RSIIndicator({"period": 4})
    # 上昇・下落混在。RSIが0〜100の範囲に収まることを確認する程度の健全性チェック
    prices = [100, 102, 101, 103, 99, 98, 105, 107, 104]
    last = None
    for p in prices:
        ind.update(make_tick("TEST", p))
        if ind.is_ready():
            last = ind.value()
    assert last is not None
    assert 0.0 <= last["rsi4"] <= 100.0, last
    print("test_rsi_mixed: OK", last)


def test_rsi_flat_prices_returns_neutral_50():
    """値動きが全くない（例：前日終値の複数回投入）場合はRSI=50（中立）を返すこと。"""
    ind = RSIIndicator({"period": 5})
    for _ in range(8):
        ind.update(make_tick("TEST", 1000.0))
    assert ind.is_ready()
    v = ind.value()
    assert v["rsi5"] == 50.0, v
    print("test_rsi_flat_prices_returns_neutral_50: OK", v)


def test_bollinger():
    ind = BollingerBandIndicator({"period": 4, "num_std": 2})
    prices = [10, 10, 10, 10]  # 分散0のケース
    last = None
    for p in prices:
        ind.update(make_tick("TEST", p))
        last = ind.value()
    assert ind.is_ready()
    assert last["middle"] == 10.0
    assert last["upper"] == 10.0
    assert last["lower"] == 10.0
    assert last["position"] == "WITHIN"
    print("test_bollinger (flat): OK", last)

    # 急騰させてバンド上抜けを確認
    ind2 = BollingerBandIndicator({"period": 4, "num_std": 1})
    for p in [10, 10, 10, 10]:
        ind2.update(make_tick("TEST", p))
    ind2.update(make_tick("TEST", 100))  # windowはmaxlen=4なので古い値が押し出される
    v = ind2.value()
    assert v["position"] == "ABOVE_UPPER", v
    print("test_bollinger (breakout): OK", v)


def test_vwap():
    ind = VWAPIndicator()
    base_dt = datetime(2026, 7, 28, 9, 0, 0)
    ticks = [
        (100.0, 1000),
        (102.0, 500),
        (98.0, 1500),
    ]
    last = None
    for i, (price, vol) in enumerate(ticks):
        ind.update(make_tick("TEST", price, volume=vol, dt=base_dt + timedelta(minutes=i)))
        last = ind.value()

    expected_vwap = (100.0 * 1000 + 102.0 * 500 + 98.0 * 1500) / (1000 + 500 + 1500)
    assert math.isclose(last["vwap"], expected_vwap), (last, expected_vwap)
    print("test_vwap: OK", last)

    # 翌日のTickでセッションがリセットされることを確認
    next_day = datetime(2026, 7, 29, 9, 0, 0)
    ind.update(make_tick("TEST", 200.0, volume=100, dt=next_day))
    v2 = ind.value()
    assert math.isclose(v2["vwap"], 200.0), v2
    print("test_vwap (session reset): OK", v2)


def test_registry():
    ind = create_indicator("sma", {"period": 5})
    assert isinstance(ind, MovingAverageIndicator)
    ind2 = create_indicator("ema", {"period": 5})
    assert ind2.ma_type == "ema"
    try:
        create_indicator("unknown_indicator")
        assert False, "例外が発生するはず"
    except ValueError:
        pass
    print("test_registry: OK")


def test_sma_seed_reproduces_prev_close():
    ind = MovingAverageIndicator({"period": 5, "type": "sma"})
    seeded = ind.seed(prev_close=1234.5)
    assert seeded is True
    assert ind.is_ready() is True
    assert ind.value()["sma5"] == 1234.5
    print("test_sma_seed_reproduces_prev_close: OK")


def test_rsi_seed_reproduces_prev_rsi():
    ind = RSIIndicator({"period": 14})
    seeded = ind.seed(prev_close=1000.0, prev_rsi=28.5)
    assert seeded is True
    assert ind.is_ready() is True
    v = ind.value()["rsi14"]
    assert abs(v - 28.5) < 0.01, v
    print("test_rsi_seed_reproduces_prev_rsi: OK", v)


def test_rsi_seed_then_real_ticks_evolve_naturally():
    ind = RSIIndicator({"period": 14})
    ind.seed(prev_close=1000.0, prev_rsi=50.0)
    # RSI50からスタートし、その後大きく上昇させればRSIも上がっていくはず
    price = 1000.0
    for _ in range(10):
        price += 20
        ind.update(make_tick("TEST", price))
    v = ind.value()["rsi14"]
    assert v > 50.0, v
    print("test_rsi_seed_then_real_ticks_evolve_naturally: OK", v)


def test_bollinger_seed_reproduces_prev_bands():
    ind = BollingerBandIndicator({"period": 20, "num_std": 2})
    seeded = ind.seed(prev_close=1000.0, prev_bb_upper=1050.0, prev_bb_middle=1000.0, prev_bb_lower=950.0)
    assert seeded is True
    assert ind.is_ready() is True
    v = ind.value()
    assert abs(v["middle"] - 1000.0) < 0.01, v
    assert abs(v["upper"] - 1050.0) < 0.01, v
    assert abs(v["lower"] - 950.0) < 0.01, v
    print("test_bollinger_seed_reproduces_prev_bands: OK", v)


def test_bollinger_seed_odd_period_reproduces_prev_bands():
    """期間が奇数でも平均・母標準偏差が正しく再現されることを確認する。"""
    ind = BollingerBandIndicator({"period": 21, "num_std": 2})
    seeded = ind.seed(prev_close=500.0, prev_bb_upper=520.0, prev_bb_middle=500.0, prev_bb_lower=480.0)
    assert seeded is True
    v = ind.value()
    assert abs(v["middle"] - 500.0) < 0.01, v
    assert abs(v["upper"] - 520.0) < 0.05, v
    assert abs(v["lower"] - 480.0) < 0.05, v
    print("test_bollinger_seed_odd_period_reproduces_prev_bands: OK", v)


def test_opening_range_ar_computes_average_true_range():
    ind = OpeningRangeARIndicator({"bar_count": 4})
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 4本分のバー（高値・安値・終値）。1本目は前本終値なしなので高値-安値のみ。
    bars = [
        (1010.0, 990.0, 1000.0),   # TR = 1010-990 = 20
        (1015.0, 995.0, 1005.0),   # TR = max(20, |1015-1000|=15, |995-1000|=5) = 20
        (1020.0, 1000.0, 1010.0),  # TR = max(20, |1020-1005|=15, |1000-1005|=5) = 20
        (1025.0, 1005.0, 1015.0),  # TR = max(20, |1025-1010|=15, |1005-1010|=5) = 20
    ]
    for i, (h, l, c) in enumerate(bars):
        tick = PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=3 * i), price=c, high=h, low=l)
        ind.update(tick)

    assert ind.is_ready() is True
    assert ind.value()["ar"] == 20.0
    print("test_opening_range_ar_computes_average_true_range: OK", ind.value())


def test_opening_range_ar_freezes_after_bar_count():
    ind = OpeningRangeARIndicator({"bar_count": 2})
    base_dt = datetime(2026, 7, 28, 9, 0)
    ticks = [
        PriceTick(symbol="9432", timestamp=base_dt, price=100.0, high=105.0, low=95.0),
        PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=3), price=100.0, high=110.0, low=90.0),
        # 3本目：本来ならTRが大きく変わるはずだが、確定済みなので無視されるはず
        PriceTick(symbol="9432", timestamp=base_dt + timedelta(minutes=6), price=100.0, high=500.0, low=1.0),
    ]
    for t in ticks:
        ind.update(t)
    first_value = ind.value()["ar"]
    assert first_value == 15.0  # (10 + 20) / 2
    print("test_opening_range_ar_freezes_after_bar_count: OK", first_value)


def test_opening_range_ar_resets_on_new_session():
    ind = OpeningRangeARIndicator({"bar_count": 2})
    day1 = datetime(2026, 7, 28, 9, 0)
    ind.update(PriceTick(symbol="9432", timestamp=day1, price=100.0, high=105.0, low=95.0))
    ind.update(PriceTick(symbol="9432", timestamp=day1 + timedelta(minutes=3), price=100.0, high=105.0, low=95.0))
    assert ind.is_ready() is True

    day2 = datetime(2026, 7, 29, 9, 0)
    ind.update(PriceTick(symbol="9432", timestamp=day2, price=100.0, high=101.0, low=99.0))
    assert ind.is_ready() is False  # 新しいセッションでリセットされ、1本目しかない状態
    print("test_opening_range_ar_resets_on_new_session: OK")


def test_opening_range_ar_seed_prevents_flat_fallback():
    """seed()は常にTrueを返し、SignalEngine側の無意味なフラットフォールバックを防ぐ。"""
    ind = OpeningRangeARIndicator({"bar_count": 4})
    seeded = ind.seed(prev_close=1000.0)
    assert seeded is True
    assert ind.is_ready() is False  # 前日終値だけではARは確定しない
    print("test_opening_range_ar_seed_prevents_flat_fallback: OK")


def test_bollinger_bbw_normalizes_band_width_by_middle():
    ind = BollingerBandIndicator({"period": 20, "num_std": 2})
    ind.seed(prev_close=1000.0, prev_bb_upper=1010.0, prev_bb_middle=1000.0, prev_bb_lower=990.0)
    v = ind.value()
    expected_bbw = (1010.0 - 990.0) / 1000.0  # = 0.02
    assert abs(v["bbw"] - expected_bbw) < 1e-9
    print("test_bollinger_bbw_normalizes_band_width_by_middle: OK", v["bbw"])


if __name__ == "__main__":
    test_sma()
    test_ema()
    test_rsi_all_gains_hits_100()
    test_rsi_mixed()
    test_rsi_flat_prices_returns_neutral_50()
    test_bollinger()
    test_vwap()
    test_registry()
    test_sma_seed_reproduces_prev_close()
    test_rsi_seed_reproduces_prev_rsi()
    test_rsi_seed_then_real_ticks_evolve_naturally()
    test_bollinger_seed_reproduces_prev_bands()
    test_bollinger_seed_odd_period_reproduces_prev_bands()
    test_opening_range_ar_computes_average_true_range()
    test_opening_range_ar_freezes_after_bar_count()
    test_opening_range_ar_resets_on_new_session()
    test_opening_range_ar_seed_prevents_flat_fallback()
    test_bollinger_bbw_normalizes_band_width_by_middle()
    print("\nすべてのテストに成功しました。")
