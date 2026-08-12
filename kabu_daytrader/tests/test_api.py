"""
api/ パッケージの動作確認用テスト。ネットワーク接続なしで検証できる範囲をカバーする:
  - RateLimiter のスライディングウィンドウ制御ロジック
  - ReconnectPolicy の最大リトライ回数・バックオフ計算
  - PushMessageParser の累計出来高→差分変換
  - RestClient のリクエスト組み立て・レスポンス解析（requestsをモック）

`python tests/test_api.py` で実行可能。
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.push_message_parser import PushMessageParser  # noqa: E402
from api.rate_limiter import RateLimiter  # noqa: E402
from api.reconnect_policy import ReconnectPolicy  # noqa: E402
from api.rest_client import AccountConfig, KabuApiError, RestClient  # noqa: E402
from trading.models import OrderStatus  # noqa: E402


# ----------------------------------------------------------------------
# RateLimiter
# ----------------------------------------------------------------------
def test_rate_limiter_allows_up_to_max_calls_without_waiting():
    fake_time = [0.0]
    sleeps = []

    def time_func():
        return fake_time[0]

    def sleep_func(seconds):
        sleeps.append(seconds)
        fake_time[0] += seconds  # 待機した分だけ時計を進める（テスト用の擬似スリープ）

    limiter = RateLimiter(max_calls=3, window_seconds=1.0, time_func=time_func, sleep_func=sleep_func)
    for _ in range(3):
        limiter.acquire()
    assert sleeps == [], "上限内なので待機は発生しないはず"
    print("test_rate_limiter_allows_up_to_max_calls_without_waiting: OK")


def test_rate_limiter_waits_when_exceeding_max_calls():
    fake_time = [0.0]
    sleeps = []

    def time_func():
        return fake_time[0]

    def sleep_func(seconds):
        sleeps.append(seconds)
        fake_time[0] += seconds

    limiter = RateLimiter(max_calls=2, window_seconds=1.0, time_func=time_func, sleep_func=sleep_func)
    limiter.acquire()  # t=0.0
    limiter.acquire()  # t=0.0 （2件目、まだ上限内）
    limiter.acquire()  # 3件目 → 上限超過のため待機が発生するはず

    assert len(sleeps) >= 1, "上限超過時には待機が発生するはず"
    assert fake_time[0] >= 1.0, "ウィンドウ(1秒)経過するまで待機しているはず"
    print("test_rate_limiter_waits_when_exceeding_max_calls: OK", sleeps)


# ----------------------------------------------------------------------
# ReconnectPolicy
# ----------------------------------------------------------------------
def test_reconnect_policy_retries_up_to_max_then_fails():
    policy = ReconnectPolicy(max_retry=3, base_backoff_seconds=2.0)

    d1 = policy.on_disconnected()
    assert d1.should_retry is True
    assert d1.retry_count == 1
    assert d1.wait_seconds == 2.0

    d2 = policy.on_disconnected()
    assert d2.should_retry is True
    assert d2.retry_count == 2
    assert d2.wait_seconds == 4.0

    d3 = policy.on_disconnected()
    assert d3.should_retry is True
    assert d3.retry_count == 3
    assert d3.wait_seconds == 8.0

    d4 = policy.on_disconnected()
    assert d4.should_retry is False, "3回リトライ後の4回目は恒久失敗扱いになるはず"
    assert policy.permanently_failed is True
    print("test_reconnect_policy_retries_up_to_max_then_fails: OK")


def test_reconnect_policy_resets_on_successful_connection():
    policy = ReconnectPolicy(max_retry=3)
    policy.on_disconnected()
    policy.on_disconnected()
    assert policy.retry_count == 2

    policy.on_connected()  # 接続成功でリセット
    assert policy.retry_count == 0

    d = policy.on_disconnected()
    assert d.retry_count == 1, "リセット後はまた1回目からカウントされるはず"
    print("test_reconnect_policy_resets_on_successful_connection: OK")


# ----------------------------------------------------------------------
# PushMessageParser
# ----------------------------------------------------------------------
def test_push_message_parser_first_tick_uses_cumulative_as_volume():
    parser = PushMessageParser()
    msg = {
        "Symbol": "9432", "CurrentPrice": 150.0,
        "CurrentPriceTime": "2026-07-28T09:00:00+09:00",
        "TradingVolume": 1000, "VWAP": 149.5,
    }
    tick = parser.parse(msg)
    assert tick is not None
    assert tick.symbol == "9432"
    assert tick.price == 150.0
    assert tick.volume == 1000  # 初回は累計値をそのまま
    assert tick.vwap_from_api == 149.5
    print("test_push_message_parser_first_tick_uses_cumulative_as_volume: OK")


def test_push_message_parser_computes_volume_diff():
    parser = PushMessageParser()
    parser.parse({
        "Symbol": "9432", "CurrentPrice": 150.0,
        "CurrentPriceTime": "2026-07-28T09:00:00+09:00", "TradingVolume": 1000,
    })
    tick2 = parser.parse({
        "Symbol": "9432", "CurrentPrice": 151.0,
        "CurrentPriceTime": "2026-07-28T09:01:00+09:00", "TradingVolume": 1500,
    })
    assert tick2.volume == 500  # 1500 - 1000
    print("test_push_message_parser_computes_volume_diff: OK")


def test_push_message_parser_resets_on_new_session_date():
    parser = PushMessageParser()
    parser.parse({
        "Symbol": "9432", "CurrentPrice": 150.0,
        "CurrentPriceTime": "2026-07-28T15:00:00+09:00", "TradingVolume": 50000,
    })
    # 翌営業日：累計出来高がリセットされてゼロから積み上がる想定
    tick_next_day = parser.parse({
        "Symbol": "9432", "CurrentPrice": 152.0,
        "CurrentPriceTime": "2026-07-29T09:00:00+09:00", "TradingVolume": 300,
    })
    assert tick_next_day.volume == 300, "セッションが変わったら差分ではなく累計をそのまま使うはず"
    print("test_push_message_parser_resets_on_new_session_date: OK")


def test_push_message_parser_independent_per_symbol():
    parser = PushMessageParser()
    parser.parse({"Symbol": "A", "CurrentPrice": 100.0,
                   "CurrentPriceTime": "2026-07-28T09:00:00+09:00", "TradingVolume": 1000})
    tick_b = parser.parse({"Symbol": "B", "CurrentPrice": 200.0,
                            "CurrentPriceTime": "2026-07-28T09:00:00+09:00", "TradingVolume": 5000})
    assert tick_b.volume == 5000, "銘柄Bは初回なので累計値そのまま（Aの状態と混ざらない）"
    print("test_push_message_parser_independent_per_symbol: OK")


def test_push_message_parser_missing_fields_returns_none():
    parser = PushMessageParser()
    assert parser.parse({"CurrentPrice": 100.0}) is None  # Symbol欠落
    assert parser.parse({"Symbol": "A"}) is None  # CurrentPrice欠落
    print("test_push_message_parser_missing_fields_returns_none: OK")


# ----------------------------------------------------------------------
# RestClient（requestsをモックして検証）
# ----------------------------------------------------------------------
def _mock_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


def test_rest_client_authenticate_success():
    session = MagicMock()
    session.post.return_value = _mock_response(200, {"ResultCode": 0, "Token": "TOKEN123"})

    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="pass", session=session)
    token = client.authenticate()
    assert token == "TOKEN123"

    called_url = session.post.call_args[0][0]
    assert called_url == "http://localhost:18080/kabusapi/token"
    called_json = session.post.call_args.kwargs["json"]
    assert called_json == {"APIPassword": "pass"}
    print("test_rest_client_authenticate_success: OK")


def test_rest_client_authenticate_failure_raises():
    session = MagicMock()
    session.post.return_value = _mock_response(400, {"Code": 4001001, "Message": "Bad"})

    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="wrong", session=session)
    try:
        client.authenticate()
        assert False, "例外が発生するはず"
    except KabuApiError:
        pass
    print("test_rest_client_authenticate_failure_raises: OK")


def test_rest_client_get_buying_power():
    session = MagicMock()
    session.post.return_value = _mock_response(200, {"Token": "TOKEN123"})
    session.get.return_value = _mock_response(200, {"StockAccountWallet": 543210.0})

    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="pass", session=session)
    power = client.get_buying_power()
    assert power == 543210.0
    print("test_rest_client_get_buying_power: OK")


def test_rest_client_place_market_buy_builds_correct_payload():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),  # token
        _mock_response(200, {"Result": 0, "OrderId": "ORDER-001"}),  # sendorder
    ]
    session.get.return_value = _mock_response(200, [
        {
            "ID": "ORDER-001", "State": 5, "CumQty": 100.0,
            "Details": [{"Price": 1500.0, "Qty": 100.0}],
        }
    ])

    account_config = AccountConfig(order_password="tradepass", account_type=4)
    client = RestClient(
        base_url="http://localhost:18080/kabusapi", api_password="pass",
        account_config=account_config, session=session,
    )
    result = client.place_market_buy("9432", 100)

    assert result.status == OrderStatus.FILLED
    assert result.filled_price == 1500.0
    assert result.order_id == "ORDER-001"
    assert result.symbol == "9432"
    assert result.qty == 100

    sendorder_call = session.post.call_args_list[1]
    payload = sendorder_call.kwargs["json"]
    assert payload["Symbol"] == "9432"
    assert payload["Side"] == "2"  # 買
    assert payload["Qty"] == 100
    assert payload["Price"] == 0  # 成行
    assert payload["Password"] == "tradepass"
    assert payload["AccountType"] == 4
    assert payload["FundType"] == "AA"  # 現物買いの既定値（実機で判明。要確認事項）
    assert payload["DelivType"] == 2  # 現物買い: お預り金
    print("test_rest_client_place_market_buy_builds_correct_payload: OK")


def test_rest_client_place_market_sell_uses_sell_side():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),
        _mock_response(200, {"Result": 0, "OrderId": "ORDER-002"}),
    ]
    session.get.return_value = _mock_response(200, [
        {"ID": "ORDER-002", "State": 5, "CumQty": 100.0, "Details": [{"Price": 1480.0, "Qty": 100.0}]}
    ])
    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="pass", session=session)
    result = client.place_market_sell("9432", 100)

    payload = session.post.call_args_list[1].kwargs["json"]
    assert payload["Side"] == "1"  # 売
    assert payload["DelivType"] == 0  # 現物売り: 指定なし
    assert payload["FundType"] == "  "  # 現物売り: 半角スペース2文字
    assert result.order_id == "ORDER-002"
    assert result.status == OrderStatus.FILLED
    assert result.filled_price == 1480.0
    print("test_rest_client_place_market_sell_uses_sell_side: OK")


def test_rest_client_order_failure_returns_failed_result_without_raising():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),
        _mock_response(400, {"Code": 4001031, "Message": "余力不足"}),
    ]
    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="pass", session=session)
    result = client.place_market_buy("9432", 100)

    assert result.status == OrderStatus.FAILED
    assert "余力不足" in (result.error_message or "")
    print("test_rest_client_order_failure_returns_failed_result_without_raising: OK")


def test_rest_client_register_symbols_payload():
    session = MagicMock()
    session.post.return_value = _mock_response(200, {"Token": "TOKEN123"})
    session.put.return_value = _mock_response(200, {"RegistList": [{"Symbol": "9432", "Exchange": 1}]})

    client = RestClient(base_url="http://localhost:18080/kabusapi", api_password="pass", session=session)
    body = client.register_symbols([("9432", 1), ("7203", 1)])

    put_payload = session.put.call_args.kwargs["json"]
    assert put_payload == {"Symbols": [{"Symbol": "9432", "Exchange": 1}, {"Symbol": "7203", "Exchange": 1}]}
    assert body["RegistList"][0]["Symbol"] == "9432"
    print("test_rest_client_register_symbols_payload: OK")


def test_rest_client_confirm_fill_retries_until_state_5():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),
        _mock_response(200, {"Result": 0, "OrderId": "ORDER-003"}),
    ]
    # 1回目・2回目は処理中(State=2)、3回目で約定完了(State=5)
    session.get.side_effect = [
        _mock_response(200, [{"ID": "ORDER-003", "State": 2, "CumQty": 0.0, "Details": []}]),
        _mock_response(200, [{"ID": "ORDER-003", "State": 2, "CumQty": 0.0, "Details": []}]),
        _mock_response(200, [{"ID": "ORDER-003", "State": 5, "CumQty": 100.0,
                               "Details": [{"Price": 1600.0, "Qty": 100.0}]}]),
    ]

    client = RestClient(
        base_url="http://localhost:18080/kabusapi", api_password="pass", session=session,
        sleep_func=lambda seconds: None,  # テストでは実時間を待たない
    )
    result = client.place_market_buy("9432", 100)

    assert result.status == OrderStatus.FILLED
    assert result.filled_price == 1600.0
    assert session.get.call_count == 3
    print("test_rest_client_confirm_fill_retries_until_state_5: OK")


def test_rest_client_confirm_fill_returns_failed_when_order_ends_without_fill():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),
        _mock_response(200, {"Result": 0, "OrderId": "ORDER-004"}),
    ]
    # 失効・取消等で終了(State=5)だが約定数量が0のケース
    session.get.return_value = _mock_response(200, [
        {"ID": "ORDER-004", "State": 5, "CumQty": 0.0, "Details": []}
    ])

    client = RestClient(
        base_url="http://localhost:18080/kabusapi", api_password="pass", session=session,
        sleep_func=lambda seconds: None,
    )
    result = client.place_market_buy("9432", 100)

    assert result.status == OrderStatus.FAILED
    assert result.error_message is not None
    print("test_rest_client_confirm_fill_returns_failed_when_order_ends_without_fill: OK")


def test_rest_client_confirm_fill_returns_pending_on_timeout():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(200, {"Token": "TOKEN123"}),
        _mock_response(200, {"Result": 0, "OrderId": "ORDER-005"}),
    ]
    # ずっと処理中のまま（タイムアウトさせる）
    session.get.return_value = _mock_response(200, [
        {"ID": "ORDER-005", "State": 2, "CumQty": 0.0, "Details": []}
    ])

    client = RestClient(
        base_url="http://localhost:18080/kabusapi", api_password="pass", session=session,
        sleep_func=lambda seconds: None,
    )
    result = client.place_market_buy("9432", 100)

    assert result.status == OrderStatus.PENDING
    assert result.order_id == "ORDER-005"
    print("test_rest_client_confirm_fill_returns_pending_on_timeout: OK")


def test_rest_client_extract_filled_price_weighted_average():
    order = {
        "Details": [
            {"Price": 1500.0, "Qty": 60.0},
            {"Price": 1502.0, "Qty": 40.0},
            {"Price": 0.0, "Qty": 100.0},  # 未約定明細（価格0）は無視されるはず
        ]
    }
    price = RestClient._extract_filled_price(order)
    expected = (1500.0 * 60 + 1502.0 * 40) / 100
    assert abs(price - expected) < 0.001
    print("test_rest_client_extract_filled_price_weighted_average: OK", price)


if __name__ == "__main__":
    test_rate_limiter_allows_up_to_max_calls_without_waiting()
    test_rate_limiter_waits_when_exceeding_max_calls()
    test_reconnect_policy_retries_up_to_max_then_fails()
    test_reconnect_policy_resets_on_successful_connection()
    test_push_message_parser_first_tick_uses_cumulative_as_volume()
    test_push_message_parser_computes_volume_diff()
    test_push_message_parser_resets_on_new_session_date()
    test_push_message_parser_independent_per_symbol()
    test_push_message_parser_missing_fields_returns_none()
    test_rest_client_authenticate_success()
    test_rest_client_authenticate_failure_raises()
    test_rest_client_get_buying_power()
    test_rest_client_place_market_buy_builds_correct_payload()
    test_rest_client_place_market_sell_uses_sell_side()
    test_rest_client_order_failure_returns_failed_result_without_raising()
    test_rest_client_register_symbols_payload()
    test_rest_client_confirm_fill_retries_until_state_5()
    test_rest_client_confirm_fill_returns_failed_when_order_ends_without_fill()
    test_rest_client_confirm_fill_returns_pending_on_timeout()
    test_rest_client_extract_filled_price_weighted_average()
    print("\nすべてのテストに成功しました。")
