"""
indicators/ パッケージの動作確認用テスト。
標準ライブラリのみで `python tests/test_indicators.py` で実行可能。

2026-08時点でSMA/EMA・ボリンジャーバンド・VWAP・AR（opening_range_ar）は
ユーザー指示により削除済み。現行の指標セットはRSI・MACD・DMIの3種類。
"""

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import (  # noqa: E402
    DMIIndicator,
    MACDIndicator,
    PriceTick,
    RSIIndicator,
    create_indicator,
)


def make_tick(symbol, price, volume=None, dt=None, open=None, high=None, low=None):
    return PriceTick(
        symbol=symbol, timestamp=dt or datetime(2026, 7, 28, 9, 0), price=price, volume=volume,
        open=open, high=high, low=low,
    )


# ----------------------------------------------------------------------
# RSI
# ----------------------------------------------------------------------
def test_rsi_all_gains_hits_100():
    ind = RSIIndicator({"period": 5})
    prices = [100, 101, 102, 103, 104, 105]
    for p in prices:
        ind.update(make_tick("TEST", p))
    assert ind.is_ready()
    v = ind.value()
    assert v["rsi5"] == 100.0
    print("test_rsi_all_gains_hits_100: OK", v)


def test_rsi_mixed():
    ind = RSIIndicator({"period": 4})
    prices = [100, 102, 101, 103, 100]
    for p in prices:
        ind.update(make_tick("TEST", p))
    assert ind.is_ready()
    v = ind.value()
    print("test_rsi_mixed: OK", v)


def test_rsi_flat_prices_returns_neutral_50():
    ind = RSIIndicator({"period": 5})
    for _ in range(8):
        ind.update(make_tick("TEST", 1000.0))
    assert ind.is_ready()
    v = ind.value()
    assert v["rsi5"] == 50.0, v
    print("test_rsi_flat_prices_returns_neutral_50: OK", v)


def test_rsi_period6_matches_user_spec():
    """ユーザー確定仕様のRSI(6)がそのまま生成できることを確認する。"""
    ind = create_indicator("rsi", {"period": 6})
    prices = [100, 99, 98, 97, 96, 95, 94]
    for p in prices:
        ind.update(make_tick("TEST", p))
    assert ind.is_ready()
    assert ind.value()["rsi6"] == 0.0  # 全て陰線なのでRSI=0
    print("test_rsi_period6_matches_user_spec: OK")


# ----------------------------------------------------------------------
# MACD
# ----------------------------------------------------------------------
def test_macd_becomes_ready_after_slow_period_bars():
    ind = MACDIndicator({"fast_period": 7, "slow_period": 26, "signal_period": 7})
    base_dt = datetime(2026, 7, 28, 9, 0)
    for i in range(25):
        ind.update(make_tick("TEST", 1000.0 + i, dt=base_dt + timedelta(minutes=5 * i)))
        assert ind.is_ready() is False
    ind.update(make_tick("TEST", 1025.0, dt=base_dt + timedelta(minutes=5 * 25)))
    assert ind.is_ready() is True
    v = ind.value()
    assert v["dif"] is not None and v["dea"] is not None and v["macd"] is not None
    print("test_macd_becomes_ready_after_slow_period_bars: OK", v)


def test_macd_uptrend_produces_positive_dif():
    ind = MACDIndicator({"fast_period": 7, "slow_period": 26, "signal_period": 7})
    base_dt = datetime(2026, 7, 28, 9, 0)
    for i in range(40):
        ind.update(make_tick("TEST", 1000.0 + i * 5, dt=base_dt + timedelta(minutes=5 * i)))
    v = ind.value()
    assert v["dif"] > 0  # 一貫した上昇トレンドなので短期EMAが長期EMAを上回るはず
    print("test_macd_uptrend_produces_positive_dif: OK", v)


def test_macd_histogram_cross_detected_on_trend_reversal():
    ind = MACDIndicator({"fast_period": 3, "slow_period": 6, "signal_period": 3})
    base_dt = datetime(2026, 7, 28, 9, 0)
    # 下降トレンドでヒストグラムをマイナスにしてから、急激に反発させてクロスを起こす
    prices = [100 - i for i in range(10)] + [95, 110, 130, 150, 170, 190]
    crossed_up_seen = False
    for i, p in enumerate(prices):
        ind.update(make_tick("TEST", p, dt=base_dt + timedelta(minutes=5 * i)))
        if ind.is_ready() and ind.value()["macd_crossed_up"]:
            crossed_up_seen = True
    assert crossed_up_seen, "反発局面でmacd_crossed_upが一度も検出されなかった"
    print("test_macd_histogram_cross_detected_on_trend_reversal: OK")


def test_macd_not_ready_returns_none_fields():
    ind = MACDIndicator({"fast_period": 7, "slow_period": 26, "signal_period": 7})
    ind.update(make_tick("TEST", 1000.0))
    v = ind.value()
    assert v["dif"] is None and v["macd"] is None
    assert v["macd_crossed_up"] is False and v["dif_crossed_dea_up"] is False
    print("test_macd_not_ready_returns_none_fields: OK")


# ----------------------------------------------------------------------
# DMI
# ----------------------------------------------------------------------
def test_dmi_strong_uptrend_pdi_above_mdi_and_high_adx():
    ind = DMIIndicator({"di_period": 6, "adx_period": 14})
    base_dt = datetime(2026, 7, 28, 9, 0)
    price = 1000.0
    for i in range(40):
        price += 10  # 一貫した強い上昇トレンド
        tick = make_tick(
            "TEST", price, dt=base_dt + timedelta(minutes=5 * i),
            open=price - 8, high=price + 2, low=price - 10,
        )
        ind.update(tick)
    assert ind.is_ready()
    v = ind.value()
    assert v["pdi"] > v["mdi"]  # 上昇トレンドなので+DIが-DIを上回るはず
    assert v["adx"] > 30  # 一貫したトレンドなのでADXも高めに出るはず
    print("test_dmi_strong_uptrend_pdi_above_mdi_and_high_adx: OK", v)


def test_dmi_strong_downtrend_mdi_above_pdi():
    ind = DMIIndicator({"di_period": 6, "adx_period": 14})
    base_dt = datetime(2026, 7, 28, 9, 0)
    price = 2000.0
    for i in range(40):
        price -= 10
        tick = make_tick(
            "TEST", price, dt=base_dt + timedelta(minutes=5 * i),
            open=price + 8, high=price + 10, low=price - 2,
        )
        ind.update(tick)
    assert ind.is_ready()
    v = ind.value()
    assert v["mdi"] > v["pdi"]
    print("test_dmi_strong_downtrend_mdi_above_pdi: OK", v)


def test_dmi_not_ready_before_di_period_bars():
    ind = DMIIndicator({"di_period": 6, "adx_period": 14})
    base_dt = datetime(2026, 7, 28, 9, 0)
    for i in range(3):
        ind.update(make_tick("TEST", 1000.0 + i, dt=base_dt + timedelta(minutes=5 * i), high=1002 + i, low=998 + i))
    assert ind.is_ready() is False
    v = ind.value()
    assert v["pdi"] is None and v["mdi"] is None and v["adx"] is None
    print("test_dmi_not_ready_before_di_period_bars: OK")


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------
def test_registry():
    ind = create_indicator("macd", {"fast_period": 7, "slow_period": 26, "signal_period": 7})
    assert isinstance(ind, MACDIndicator)
    ind2 = create_indicator("dmi", {"di_period": 6, "adx_period": 14})
    assert isinstance(ind2, DMIIndicator)
    try:
        create_indicator("no_such_indicator")
        assert False, "未登録の指標名でエラーにならなかった"
    except ValueError:
        pass
    print("test_registry: OK")


if __name__ == "__main__":
    test_rsi_all_gains_hits_100()
    test_rsi_mixed()
    test_rsi_flat_prices_returns_neutral_50()
    test_rsi_period6_matches_user_spec()
    test_macd_becomes_ready_after_slow_period_bars()
    test_macd_uptrend_produces_positive_dif()
    test_macd_histogram_cross_detected_on_trend_reversal()
    test_macd_not_ready_returns_none_fields()
    test_dmi_strong_uptrend_pdi_above_mdi_and_high_adx()
    test_dmi_strong_downtrend_mdi_above_pdi()
    test_dmi_not_ready_before_di_period_bars()
    test_registry()
    print("\nすべてのテストに成功しました。")
