"""
kabuステーションREST APIのラッパー。

trading.BrokerClient プロトコルを満たす（get_buying_power / place_market_buy /
place_market_sell）ため、trading.OrderExecutor へそのまま渡して使える。

sendorder直後、GET /orders を短間隔でポーリングして約定確認を行ってから
FILLED/約定価格をOrderResultへ反映する（_confirm_fill）。これにより、
sendorderのレスポンスに約定価格が含まれないという制約があっても、
OrderExecutor側は「place_market_buy/sellがFILLEDを返す」という
SimulatedBrokerClientと同じ前提のまま使える。
ただし、このポーリングは呼び出し元のスレッドをブロックする（既定最大約4.5秒）。
本番監視ループ（TradingController._on_tick）は現状シングルスレッドで
Tick処理と発注を同期的に行っているため、発注中は他銘柄のTick処理も
その分遅延する。多数銘柄を高頻度で監視する場合、発注処理だけを
別スレッド（基本設計書のOrderWorker相当）に分離する改修を検討すること。

【重要・要確認事項】
sendorder（発注）APIのリクエストには、口座種別・受渡区分・執行条件などを
表す複数のコード値（AccountType, DelivType, FrontOrderType 等）が必要です。
これらは証券口座の設定（特定口座／一般口座、預り区分など）によって
正しい値が変わるため、本実装では AccountConfig で一箇所にまとめ、
既定値には一般的な値を仮置きしています。
**実際に発注する前に、必ずkabuステーションAPI公式リファレンスと
ご自身の口座設定を突き合わせて値を確認してください**
（検証用環境18081はレスポンス自体が空/Nullになるため、この確認には
使えません。CLAUDE.mdの「検証用環境の既知の制約」を参照）。
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from trading.models import OrderReason, OrderResult, OrderSide, OrderStatus

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class KabuApiError(Exception):
    """kabuステーションAPIがエラー応答を返した場合に送出する。"""


@dataclass
class AccountConfig:
    """
    sendorder発注時に必要な口座関連コード値。

    現物の買い注文と売り注文でDelivType・FundTypeの正しい値が異なる
    （複数の公開実装例で確認済み）ため、買い用・売り用を別フィールドで持つ。

    現物買い（実機・複数の公開実装例で確認済みの一般的な値）:
        DelivType: 2（お預り金）
        FundType: "AA"（信用代用）または "02"（保護預り）。
                  どちらが正しいかは口座の資金区分設定次第で異なる。
                  誤っていると発注時にエラー（Code: 4001005）が返る。
    現物売り（実機・複数の公開実装例で確認済み）:
        DelivType: 0（指定なし）
        FundType: 半角スペース2文字（" "）または省略

    【要確認】上記はいずれも実機の口座設定によって変わりうる想定値。
    実際に発注する前に、必ずご自身の口座設定と突き合わせて確認すること。
    """

    order_password: str = ""  # 取引パスワード（APIパスワードとは別物）
    security_type: int = 1  # 1 = 株式
    cash_margin: int = 1  # 1 = 現物
    buy_deliv_type: int = 2  # 現物買い: 2 = お預り金
    buy_fund_type: str = "AA"  # 現物買い: "AA"=信用代用 または "02"=保護預り（要確認）
    sell_deliv_type: int = 0  # 現物売り: 0 = 指定なし
    sell_fund_type: str = "  "  # 現物売り: 半角スペース2文字（要確認）
    account_type: int = 4  # 4 = 特定口座（要確認。一般口座なら2、法人なら12）
    front_order_type: int = 10  # 10 = 成行


@dataclass
class MarginConfig:
    """
    信用取引（新規売り＝空売り含む）の発注に必要な設定値。

    参考: https://note.com/note_20260215/n/n6e4a2f905401
    （kabuステーションAPIでの信用取引発注パラメータの解説記事）

    信用新規（買い建て・売り建てとも）:
        CashMargin: 2
        DelivType : 0（指定なし）
        FundType  : "11"（省略可。省略時は自動的に"11"がセットされる）
        Exchange  : 通常時は東証"1"では新規発注できないため、東証+"27"を使う
    信用返済（買い建て・売り建てとも、反対売買で決済）:
        CashMargin: 3
        DelivType : 2（お預り金。要確認）
        FundType  : "11"（省略可）
        Exchange  : 返済対象の建玉を保有している市場に合わせる。
                    新規を東証+"27"で発注しているため、返済も"27"に揃える
        ClosePositionOrder: 決済順序を指定して建玉を自動選択する方式（0〜7）。
                    本実装では建玉ID（HoldID）を個別管理せず、この方式を使う
                    （HoldIDを使う場合はClosePositionsを使うが、
                    そちらは建玉一覧の取得・追跡が別途必要になるため未対応）。

    【重要・要確認】
    - margin_trade_type（制度信用/一般信用(長期)/一般信用(デイトレ)）は、
      実際の口座の信用取引区分設定に合わせて必ず確認すること。
      既定値は一般信用（デイトレ）＝3としているが、口座によっては
      制度信用（1）や一般信用・長期（2）が正しい場合がある。
    - close_position_orderの各値（0〜7）が具体的にどの決済順序を意味するかは
      公式リファレンスで確認できておらず、既定値0が意図した挙動になるかは未検証。
      実際に返済注文を送る前に、少額での実地確認を強く推奨する。
    - exchangeは東証+"27"を既定にしているが、実際の口座・銘柄で
      新規発注が通るか、返済時に建玉と市場が一致するか確認すること。
    """

    margin_trade_type: int = 3  # 1=制度信用 2=一般信用(長期) 3=一般信用(デイトレ)。要確認
    exchange: int = 27  # 東証+。新規発注では東証"1"は使用不可
    close_position_order: int = 0  # 決済順序指定（0〜7）。各値の意味は要確認
    fund_type: str = "11"  # 信用取引のFundType（省略可だが明示的に設定する）
    open_deliv_type: int = 0  # 信用新規のDelivType
    close_deliv_type: int = 2  # 信用返済のDelivType（お預り金。要確認）


class RestClient:
    def __init__(
        self,
        base_url: str,
        api_password: str,
        account_config: Optional[AccountConfig] = None,
        margin_config: Optional[MarginConfig] = None,
        session: Optional[requests.Session] = None,
        sleep_func: Optional[Any] = None,
    ):
        """
        base_url: 例 "http://localhost:18080/kabusapi"（本番）
                       "http://localhost:18081/kabusapi"（検証用）
        api_password: APIシステム設定で登録したAPIパスワード
                       （本番用・検証用で別のパスワードなので注意）
        sleep_func: 約定確認ポーリングの待機に使う関数（既定time.sleep）。
                    テストで実時間を待たずにポーリングロジックを検証するための差し替え口。
        """
        self.base_url = base_url.rstrip("/")
        self.api_password = api_password
        self.account_config = account_config or AccountConfig()
        self.margin_config = margin_config or MarginConfig()
        self.session = session or requests.Session()

        import time as _time_module
        self._sleep_func = sleep_func or _time_module.sleep

        self._token: Optional[str] = None

        # kabuステーションAPIのレート制限順守
        self._order_rate_limiter = RateLimiter(max_calls=5, window_seconds=1.0)
        self._info_rate_limiter = RateLimiter(max_calls=10, window_seconds=1.0)

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------
    def authenticate(self) -> str:
        """トークンを取得して保持する。失敗時は KabuApiError を送出する。"""
        url = f"{self.base_url}/token"
        resp = self.session.post(url, json={"APIPassword": self.api_password}, timeout=5)
        body = self._parse_response(resp)
        token = body.get("Token")
        if not token:
            raise KabuApiError(f"トークン取得に失敗しました: {body}")
        self._token = token
        logger.info("kabuステーションAPI トークン取得成功")
        return token

    def _headers(self) -> Dict[str, str]:
        if self._token is None:
            self.authenticate()
        return {"X-API-KEY": self._token, "Content-Type": "application/json"}

    @staticmethod
    def _parse_response(resp: requests.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise KabuApiError(f"予期しない応答形式です: {resp.text[:200]}")

        if resp.status_code != 200:
            raise KabuApiError(f"APIエラー status={resp.status_code} body={body}")
        return body

    # ------------------------------------------------------------------
    # 情報系（レート制限: 10件/秒）
    # ------------------------------------------------------------------
    def get_buying_power(self) -> float:
        """現物買付可能額を取得する。取得できない場合は0.0を返す（安全側）。"""
        self._info_rate_limiter.acquire()
        url = f"{self.base_url}/wallet/cash"
        resp = self.session.get(url, headers=self._headers(), timeout=5)
        body = self._parse_response(resp)

        value = body.get("StockAccountWallet")
        if value is None:
            logger.warning("現物買付可能額が取得できませんでした（応答: %s）", body)
            return 0.0
        return float(value)

    def get_board(self, symbol: str, exchange: int = 1) -> Dict[str, Any]:
        self._info_rate_limiter.acquire()
        url = f"{self.base_url}/board/{symbol}@{exchange}"
        resp = self.session.get(url, headers=self._headers(), timeout=5)
        return self._parse_response(resp)

    def get_previous_close(self, symbol: str, exchange: int = 1) -> Optional[float]:
        """
        前日終値（PreviousClose）を取得する。板情報(board)レスポンスに含まれる。
        取得できない場合はNoneを返す（呼び出し元はウォームアップを諦めて
        通常のTick投入から始めればよく、致命的なエラーにはしない）。
        """
        try:
            body = self.get_board(symbol, exchange)
        except KabuApiError as e:
            logger.warning("前日終値の取得に失敗しました: symbol=%s error=%s", symbol, e)
            return None

        value = body.get("PreviousClose")
        if value is None:
            return None
        return float(value)

    def register_symbols(self, symbols: List[Tuple[str, int]]) -> Dict[str, Any]:
        """symbols: [(銘柄コード, 市場コード), ...]  市場コード1=東証"""
        self._info_rate_limiter.acquire()
        url = f"{self.base_url}/register"
        payload = {"Symbols": [{"Symbol": s, "Exchange": e} for s, e in symbols]}
        resp = self.session.put(url, json=payload, headers=self._headers(), timeout=5)
        return self._parse_response(resp)

    def unregister_all_symbols(self) -> Dict[str, Any]:
        """
        登録済み銘柄をすべて解除する（PUT /unregister/all）。
        register_symbols()は既存の登録に「追加」されるだけで上書きしないため、
        監視銘柄リストを入れ替える際は必ず先にこれを呼んで前回分をクリアすること
        （実機で、前回セッションの銘柄が残ったまま新しいセッションのPUSHにも
        混ざって配信され続ける不具合が確認されたための対策）。
        API登録が1件もない状態で呼ぶとエラーになる場合があるため、失敗しても無視する。
        """
        self._info_rate_limiter.acquire()
        url = f"{self.base_url}/unregister/all"
        try:
            resp = self.session.put(url, headers=self._headers(), timeout=5)
            return self._parse_response(resp)
        except KabuApiError as e:
            logger.info("銘柄全解除でエラー（登録が0件だった可能性）: %s", e)
            return {}

    # ------------------------------------------------------------------
    # 発注系（レート制限: 5件/秒）／ trading.BrokerClient プロトコル実装
    # ------------------------------------------------------------------
    def place_market_buy(self, symbol: str, qty: int) -> OrderResult:
        return self._send_market_order(symbol, qty, side="2")  # "2" = 買

    def place_market_sell(self, symbol: str, qty: int) -> OrderResult:
        return self._send_market_order(symbol, qty, side="1")  # "1" = 売

    # ------------------------------------------------------------------
    # 信用取引（新規売り＝空売り含む）
    # 参考: https://note.com/note_20260215/n/n6e4a2f905401
    # ------------------------------------------------------------------
    def place_margin_buy_to_open(self, symbol: str, qty: int) -> OrderResult:
        """信用新規買い（買い建て）。"""
        return self._send_margin_order(symbol, qty, side="2", is_open=True)

    def place_margin_sell_to_open(self, symbol: str, qty: int) -> OrderResult:
        """信用新規売り（売り建て＝空売り）。"""
        return self._send_margin_order(symbol, qty, side="1", is_open=True)

    def place_margin_sell_to_close(self, symbol: str, qty: int) -> OrderResult:
        """買い建て玉の返済（反対売買の売り）。"""
        return self._send_margin_order(symbol, qty, side="1", is_open=False)

    def place_margin_buy_to_close(self, symbol: str, qty: int) -> OrderResult:
        """売り建て玉の返済（反対売買の買い）。"""
        return self._send_margin_order(symbol, qty, side="2", is_open=False)

    def _send_margin_order(self, symbol: str, qty: int, side: str, is_open: bool) -> OrderResult:
        """
        信用取引の新規・返済を成行で発注する。

        【要確認】margin_trade_type・close_position_order・exchangeはいずれも
        MarginConfigのdocstringに記載のとおり実機での確認が必要な想定値。
        実際に発注する前に、必ず少額でご自身の口座設定と突き合わせて確認すること。
        """
        now = datetime.now()
        side_enum = OrderSide.BUY if side == "2" else OrderSide.SELL

        self._order_rate_limiter.acquire()
        acfg = self.account_config
        mcfg = self.margin_config

        payload: Dict[str, Any] = {
            "Password": acfg.order_password,
            "Symbol": symbol,
            "Exchange": mcfg.exchange,
            "SecurityType": acfg.security_type,
            "Side": side,
            "CashMargin": 2 if is_open else 3,  # 2=信用新規 3=信用返済
            "MarginTradeType": mcfg.margin_trade_type,
            "DelivType": mcfg.open_deliv_type if is_open else mcfg.close_deliv_type,
            "FundType": mcfg.fund_type,
            "AccountType": acfg.account_type,
            "Qty": qty,
            "FrontOrderType": acfg.front_order_type,  # 成行
            "Price": 0,  # 成行のため0
            "ExpireDay": 0,
        }
        if not is_open:
            # 返済：建玉IDを個別指定せず、決済順序を指定して自動選択する方式を使う
            # （ClosePositionOrderとClosePositionsはどちらか一方のみ指定すること）
            payload["ClosePositionOrder"] = mcfg.close_position_order

        url = f"{self.base_url}/sendorder"
        try:
            resp = self.session.post(url, json=payload, headers=self._headers(), timeout=5)
            body = self._parse_response(resp)
        except KabuApiError as e:
            logger.error(
                "信用発注失敗: symbol=%s side=%s is_open=%s error=%s", symbol, side, is_open, e
            )
            return OrderResult(
                order_id="", symbol=symbol, side=side_enum, qty=qty,
                status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=str(e),
            )
        except requests.RequestException as e:
            logger.error(
                "信用発注リクエストで通信エラー: symbol=%s side=%s is_open=%s error=%s",
                symbol, side, is_open, e,
            )
            return OrderResult(
                order_id="", symbol=symbol, side=side_enum, qty=qty,
                status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=str(e),
            )

        order_id = str(body.get("OrderId", ""))
        return self._confirm_fill(order_id, symbol, qty, side_enum, now)

    def _send_market_order(self, symbol: str, qty: int, side: str) -> OrderResult:
        now = datetime.now()
        side_enum = OrderSide.BUY if side == "2" else OrderSide.SELL

        self._order_rate_limiter.acquire()
        cfg = self.account_config
        is_buy = side == "2"
        payload = {
            "Password": cfg.order_password,
            "Symbol": symbol,
            "Exchange": 1,
            "SecurityType": cfg.security_type,
            "Side": side,
            "CashMargin": cfg.cash_margin,
            "DelivType": cfg.buy_deliv_type if is_buy else cfg.sell_deliv_type,
            "FundType": cfg.buy_fund_type if is_buy else cfg.sell_fund_type,
            "AccountType": cfg.account_type,
            "Qty": qty,
            "FrontOrderType": cfg.front_order_type,  # 成行
            "Price": 0,  # 成行のため0
            "ExpireDay": 0,
        }

        url = f"{self.base_url}/sendorder"
        try:
            resp = self.session.post(url, json=payload, headers=self._headers(), timeout=5)
            body = self._parse_response(resp)
        except KabuApiError as e:
            logger.error("発注失敗: symbol=%s side=%s error=%s", symbol, side, e)
            return OrderResult(
                order_id="", symbol=symbol, side=side_enum, qty=qty,
                status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=str(e),
            )
        except requests.RequestException as e:
            logger.error("発注リクエストで通信エラー: symbol=%s side=%s error=%s", symbol, side, e)
            return OrderResult(
                order_id="", symbol=symbol, side=side_enum, qty=qty,
                status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                requested_at=now, error_message=str(e),
            )

        order_id = str(body.get("OrderId", ""))
        return self._confirm_fill(order_id, symbol, qty, side_enum, now)

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        GET /orders?product=0&id={order_id} で単一注文の状態を照会する。
        該当注文が見つからない場合はNoneを返す。

        レスポンスの主なフィールド（実機・公開情報から確認）:
            State: 1=待機 2=処理中 3=処理済 4=訂正取消送信中 5=終了
            CumQty: 約定数量（発注数量に対する累計約定株数）
            Details: 執行明細の配列。約定した明細はPrice>0で入る
                     （待機中の明細はPrice=0.0で入っていることが多い）
        """
        self._info_rate_limiter.acquire()
        url = f"{self.base_url}/orders"
        resp = self.session.get(
            url, params={"product": 0, "id": order_id}, headers=self._headers(), timeout=5
        )
        body = self._parse_response(resp)
        if not isinstance(body, list):
            return None
        for order in body:
            if str(order.get("ID")) == str(order_id):
                return order
        return None

    def _confirm_fill(
        self,
        order_id: str,
        symbol: str,
        qty: int,
        side_enum: OrderSide,
        requested_at: datetime,
        max_attempts: int = 15,
        interval_seconds: float = 0.3,
    ) -> OrderResult:
        """
        sendorder直後、GET /ordersを短い間隔でポーリングして約定を確認する。
        成行注文は取引時間中であれば通常1秒前後で約定するため、
        既定では最大約4.5秒（0.3秒×15回）待つ。

        【重要な制約・要確認】
        - この待機時間内に約定が確認できなかった場合はPENDINGのまま返す
          （FAILEDにはしない＝実際には後から約定している可能性があるため）。
          呼び出し元のtrading.OrderExecutorはFILLED以外を「発注されなかった」
          扱いにするため、この場合ポジションとして記録されない。実運用では
          別途でGET /positionsとの突き合わせ（残高照会との整合性チェック）を
          行う運用でカバーすることを推奨する。
        - 約定価格の算出方法（Details配列からPrice>0の明細をQty加重平均）は
          実機のレスポンス例を基にした推定であり、正式仕様書での確認が
          取れていない。実際の約定価格とズレる可能性があるため、
          実運用開始時は少額での実地確認を必ず行うこと。
        """
        for _ in range(max_attempts):
            try:
                order = self.get_order(order_id)
            except KabuApiError as e:
                logger.warning("注文照会に失敗しました。リトライします: order_id=%s error=%s", order_id, e)
                order = None

            if order is not None:
                state = order.get("State")
                cum_qty = float(order.get("CumQty", 0) or 0)

                if state == 5:  # 終了（全約定・取消・失効・エラー等）
                    if cum_qty >= qty:
                        filled_price = self._extract_filled_price(order)
                        if filled_price is not None:
                            now = datetime.now()
                            logger.info(
                                "約定確認: order_id=%s symbol=%s price=%.1f", order_id, symbol, filled_price
                            )
                            return OrderResult(
                                order_id=order_id, symbol=symbol, side=side_enum, qty=qty,
                                status=OrderStatus.FILLED, reason=OrderReason.SIGNAL,
                                requested_at=requested_at, filled_at=now, filled_price=filled_price,
                            )
                        # 全約定のはずだが価格が取れなかった場合は安全側でPENDING扱いにする
                        logger.warning("全約定を検知しましたが約定価格が取得できませんでした: order_id=%s", order_id)
                    else:
                        # 終了だが未約定＝取消・失効・エラー等
                        logger.warning("注文が約定せず終了しました: order_id=%s state=%s cum_qty=%s", order_id, state, cum_qty)
                        return OrderResult(
                            order_id=order_id, symbol=symbol, side=side_enum, qty=qty,
                            status=OrderStatus.FAILED, reason=OrderReason.SIGNAL,
                            requested_at=requested_at, error_message="注文が約定しないまま終了しました",
                        )

            self._sleep_func(interval_seconds)

        logger.warning(
            "約定確認がタイムアウトしました（%d回リトライ）。PENDINGのまま返します: order_id=%s",
            max_attempts, order_id,
        )
        return OrderResult(
            order_id=order_id, symbol=symbol, side=side_enum, qty=qty,
            status=OrderStatus.PENDING, reason=OrderReason.SIGNAL,
            requested_at=requested_at,
        )

    @staticmethod
    def _extract_filled_price(order: Dict[str, Any]) -> Optional[float]:
        """
        Details配列から、実際に約定した明細（Price>0のもの）をQty加重平均して
        約定価格を算出する。該当する明細が無ければNoneを返す。
        """
        details = order.get("Details") or []
        total_qty = 0.0
        total_value = 0.0
        for detail in details:
            price = float(detail.get("Price", 0) or 0)
            qty = float(detail.get("Qty", 0) or 0)
            if price > 0 and qty > 0:
                total_qty += qty
                total_value += price * qty
        if total_qty <= 0:
            return None
        return total_value / total_qty
