"""
trading.LiveBarAggregator の動作確認用テスト。 `python tests/test_bar_aggregator.py` で実行可能。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import PriceTick  # noqa: E402
from trading import LiveBarAggregator  # noqa: E402


def test_bar_aggregator_completes_bar_on_bucket_change():
    agg = LiveBarAggregator(interval_minutes=3)
    base = datetime(2026, 7, 28, 9, 0, 0)

    # 9:00〜9:02台は同一バケット（9:00開始の3分足）
    assert agg.add_tick(PriceTick("9432", base, 100.0)) is None
    assert agg.add_tick(PriceTick("9432", base + timedelta(minutes=1), 105.0)) is None
    assert agg.add_tick(PriceTick("9432", base + timedelta(minutes=2, seconds=30), 98.0)) is None

    # 9:03台に入った瞬間、9:00バーが確定して返る
    completed = agg.add_tick(PriceTick("9432", base + timedelta(minutes=3), 102.0))
    assert completed is not None
    assert completed.timestamp == base
    assert completed.open == 100.0
    assert completed.high == 105.0
    assert completed.low == 98.0
    assert completed.price == 98.0  # 直近のTick(9:02:30)が最後の終値（PriceTickではpriceが終値相当）
    print("test_bar_aggregator_completes_bar_on_bucket_change: OK", completed)


def test_bar_aggregator_sums_volume_within_bucket():
    agg = LiveBarAggregator(interval_minutes=3)
    base = datetime(2026, 7, 28, 9, 0, 0)
    agg.add_tick(PriceTick("9432", base, 100.0, volume=100))
    agg.add_tick(PriceTick("9432", base + timedelta(minutes=1), 101.0, volume=200))
    completed = agg.add_tick(PriceTick("9432", base + timedelta(minutes=3), 102.0, volume=50))
    assert completed.volume == 300  # 100+200（3本目のvolumeは次バケットへ）
    print("test_bar_aggregator_sums_volume_within_bucket: OK")


def test_bar_aggregator_handles_multiple_symbols_independently():
    agg = LiveBarAggregator(interval_minutes=3)
    base = datetime(2026, 7, 28, 9, 0, 0)
    agg.add_tick(PriceTick("A", base, 100.0))
    agg.add_tick(PriceTick("B", base, 200.0))
    completed_a = agg.add_tick(PriceTick("A", base + timedelta(minutes=3), 110.0))
    # Bはまだ同じバケット内なのでNoneのまま
    completed_b_none = agg.add_tick(PriceTick("B", base + timedelta(minutes=1), 205.0))
    completed_b = agg.add_tick(PriceTick("B", base + timedelta(minutes=3), 210.0))

    assert completed_a is not None and completed_a.symbol == "A"
    assert completed_b_none is None
    assert completed_b is not None and completed_b.symbol == "B"
    print("test_bar_aggregator_handles_multiple_symbols_independently: OK")


def test_bar_aggregator_flush_returns_partial_bar():
    agg = LiveBarAggregator(interval_minutes=3)
    base = datetime(2026, 7, 28, 9, 0, 0)
    agg.add_tick(PriceTick("9432", base, 100.0, volume=10))
    agg.add_tick(PriceTick("9432", base + timedelta(minutes=1), 105.0, volume=20))

    flushed = agg.flush("9432")
    assert flushed is not None
    assert flushed.open == 100.0
    assert flushed.price == 105.0
    assert flushed.volume == 30

    # flush後はバケットがクリアされているので再度flushしてもNone
    assert agg.flush("9432") is None
    print("test_bar_aggregator_flush_returns_partial_bar: OK")


def test_bar_aggregator_bucket_boundaries_align_to_market_open():
    agg = LiveBarAggregator(interval_minutes=3)
    # 9:00は540分（真夜中から）で3の倍数なので、9:00,9:03,9:06...ときれいに揃うはず
    t1 = datetime(2026, 7, 28, 9, 1, 0)
    bucket_start = agg._floor_to_interval(t1)
    assert bucket_start == datetime(2026, 7, 28, 9, 0, 0)

    t2 = datetime(2026, 7, 28, 9, 4, 30)
    bucket_start2 = agg._floor_to_interval(t2)
    assert bucket_start2 == datetime(2026, 7, 28, 9, 3, 0)
    print("test_bar_aggregator_bucket_boundaries_align_to_market_open: OK")


if __name__ == "__main__":
    test_bar_aggregator_completes_bar_on_bucket_change()
    test_bar_aggregator_sums_volume_within_bucket()
    test_bar_aggregator_handles_multiple_symbols_independently()
    test_bar_aggregator_flush_returns_partial_bar()
    test_bar_aggregator_bucket_boundaries_align_to_market_open()
    print("\nすべてのテストに成功しました。")
