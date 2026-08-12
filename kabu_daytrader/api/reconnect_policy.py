"""
PUSH API(WebSocket)切断時の再接続方針。

CLAUDE.md確定仕様：
  「PUSH API接続が切断された場合は最大3回まで再接続をリトライし、
   失敗したら保有ポジションを成行で強制決済する」

WebSocket通信そのものとは切り離し、
「あと何回リトライすべきか」「何秒待ってから再接続すべきか」
の判断ロジックだけを独立させることで、ネットワークなしに単体テストできるようにしている。
"""

from dataclasses import dataclass


@dataclass
class ReconnectDecision:
    should_retry: bool
    wait_seconds: float
    retry_count: int  # 今回が何回目のリトライか（1始まり）


class ReconnectPolicy:
    def __init__(self, max_retry: int = 3, base_backoff_seconds: float = 2.0):
        self.max_retry = max_retry
        self.base_backoff_seconds = base_backoff_seconds
        self._retry_count = 0

    def on_connected(self) -> None:
        """接続確立に成功したらリトライカウントをリセットする。"""
        self._retry_count = 0

    def on_disconnected(self) -> ReconnectDecision:
        """
        切断が発生した際に呼び出す。
        まだリトライ余地があれば should_retry=True と待機秒数を返し、
        max_retryを使い切っていれば should_retry=False を返す
        （呼び出し元はこれを「恒久的な切断」として扱い、
        RiskManager.notify_push_disconnected_permanently() を呼ぶこと）。
        """
        self._retry_count += 1
        if self._retry_count > self.max_retry:
            return ReconnectDecision(should_retry=False, wait_seconds=0.0, retry_count=self._retry_count)

        # 指数バックオフ（2秒, 4秒, 8秒, ...）
        wait = self.base_backoff_seconds * (2 ** (self._retry_count - 1))
        return ReconnectDecision(should_retry=True, wait_seconds=wait, retry_count=self._retry_count)

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def permanently_failed(self) -> bool:
        return self._retry_count > self.max_retry
