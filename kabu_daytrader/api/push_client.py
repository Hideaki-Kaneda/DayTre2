"""
kabuステーションPUSH API(WebSocket)クライアント。

CLAUDE.md確定仕様に対応:
  「PUSH API接続が切断された場合は最大3回まで再接続をリトライし、
   失敗したら保有ポジションを成行で強制決済する」

再接続の可否・待機時間の判断は ReconnectPolicy に、
メッセージ→PriceTick変換は PushMessageParser に委譲しており、
このクラス自体はWebSocketの配線（スレッド起動・コールバック接続）に専念する。
"""

import json
import logging
import threading
from typing import Callable, Optional

from indicators import PriceTick

from .push_message_parser import PushMessageParser
from .reconnect_policy import ReconnectPolicy

logger = logging.getLogger(__name__)

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


class PushClient:
    def __init__(
        self,
        ws_url: str,
        on_tick: Callable[[PriceTick], None],
        on_permanent_disconnect: Callable[[], None],
        max_retry: int = 3,
        base_backoff_seconds: float = 2.0,
    ):
        """
        ws_url: 例 "ws://localhost:18080/kabusapi/websocket"（本番）
        on_tick: PriceTickを受け取るたびに呼ばれるコールバック
                 （典型的には signals.SignalEngine.update / process_tick を渡す）
        on_permanent_disconnect: max_retry回のリトライを使い切っても再接続できなかった際に
                 一度だけ呼ばれるコールバック
                 （典型的には trading.RiskManager.notify_push_disconnected_permanently を渡す）
        """
        if websocket is None:
            raise RuntimeError(
                "websocket-client がインストールされていません。`pip install websocket-client` を実行してください。"
            )

        self.ws_url = ws_url
        self.on_tick = on_tick
        self.on_permanent_disconnect = on_permanent_disconnect
        self.reconnect_policy = ReconnectPolicy(max_retry=max_retry, base_backoff_seconds=base_backoff_seconds)
        self.parser = PushMessageParser()

        self._ws_app: Optional["websocket.WebSocketApp"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run_with_reconnect, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        if self._ws_app is not None:
            try:
                self._ws_app.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------
    def _run_with_reconnect(self) -> None:
        while not self._stop_requested.is_set():
            self._connect_once()  # ブロッキング（run_forever）。切断されるとreturnする

            if self._stop_requested.is_set():
                return

            decision = self.reconnect_policy.on_disconnected()
            if not decision.should_retry:
                logger.error(
                    "PUSH API再接続を%d回試行しましたが失敗しました。保有ポジションの強制決済処理を通知します。",
                    self.reconnect_policy.max_retry,
                )
                self.on_permanent_disconnect()
                return

            logger.warning(
                "PUSH API切断を検知。%.1f秒後に再接続します（%d/%d回目）",
                decision.wait_seconds, decision.retry_count, self.reconnect_policy.max_retry,
            )
            self._stop_requested.wait(decision.wait_seconds)

    def _connect_once(self) -> None:
        self._ws_app = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_app.run_forever()

    # ------------------------------------------------------------------
    # WebSocketコールバック
    # ------------------------------------------------------------------
    def _on_open(self, ws) -> None:
        logger.info("PUSH API接続確立")
        self.reconnect_policy.on_connected()

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("PUSH配信メッセージのJSON解析に失敗しました: %s", message[:200])
            return

        tick = self.parser.parse(data)
        if tick is not None:
            self.on_tick(tick)

    def _on_error(self, ws, error) -> None:
        logger.warning("PUSH APIエラー: %s", error)

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info("PUSH API切断: code=%s msg=%s", close_status_code, close_msg)
