"""
kabuステーション®API 検証用環境 疎通確認スクリプト

目的：
  1. 検証用ポート(18081)へのトークン認証ができること
  2. REST APIで基本情報（現物買付可能額）が取得できること
  3. 銘柄登録ができること
  4. PUSH API(WebSocket)で配信データを受信できること
     - 切断時の自動再接続（最大3回）ロジックも簡易検証する

前提：
  - kabuステーション®が起動済みで、APIシステム設定の「検証用」パスワードが
    設定されていること（本番用と検証用でパスワードが異なる点に注意）
  - pip install requests websocket-client

実行方法：
  python check_connectivity.py --password YOUR_VERIFY_PASSWORD --symbol 9432

  パスワードは環境変数 KABU_API_PASSWORD からも読み込めます
  （コード中にパスワードをハードコードしないこと）:
      export KABU_API_PASSWORD=xxxxx
      python check_connectivity.py --symbol 9432
"""

import argparse
import json
import os
import sys
import threading
import time

import requests

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


# 検証用環境のベースURL（本番用は18080）
VERIFY_BASE_URL = "http://localhost:18081/kabusapi"
VERIFY_WS_URL = "ws://localhost:18081/kabusapi/websocket"

# 基本設計書10章の方針に合わせ、将来的にはSecretsManager経由でENC(...)形式の
# 暗号化ファイルから取得する想定。本スクリプトでは検証目的のため
# 環境変数 or コマンドライン引数から取得する簡易実装に留める。


def get_token(password: str) -> str:
    """トークンを取得する。失敗時は例外を送出する。"""
    url = f"{VERIFY_BASE_URL}/token"
    headers = {"Content-Type": "application/json"}
    payload = {"APIPassword": password}

    resp = requests.post(url, data=json.dumps(payload), headers=headers, timeout=5)
    resp.raise_for_status()
    body = resp.json()
    token = body.get("Token")
    if not token:
        raise RuntimeError(f"トークン取得に失敗しました。レスポンス: {body}")
    return token


def check_wallet(token: str) -> None:
    """現物買付可能額の取得を試み、REST APIの疎通を確認する。"""
    url = f"{VERIFY_BASE_URL}/wallet/cash"
    headers = {"X-API-KEY": token}

    resp = requests.get(url, headers=headers, timeout=5)
    if resp.status_code != 200:
        print(f"[NG] REST API(残高照会)呼び出し失敗: {resp.status_code} {resp.text}")
        return
    print(f"[OK] REST API疎通確認（現物買付可能額）: {resp.json()}")


def register_symbol(token: str, symbol: str, exchange: int = 1) -> None:
    """銘柄をPUSH配信対象として登録する。exchange=1は東証。"""
    url = f"{VERIFY_BASE_URL}/register"
    headers = {"X-API-KEY": token, "Content-Type": "application/json"}
    payload = {"Symbols": [{"Symbol": symbol, "Exchange": exchange}]}

    resp = requests.put(url, data=json.dumps(payload), headers=headers, timeout=5)
    if resp.status_code != 200:
        print(f"[NG] 銘柄登録失敗: {resp.status_code} {resp.text}")
        return
    print(f"[OK] 銘柄登録成功: {symbol} → {resp.json()}")


class PushConnectivityCheck:
    """
    PUSH API(WebSocket)の疎通確認。
    基本設計書5章のPushClient設計に合わせ、切断時は最大3回まで再接続を試みる。
    このスクリールでは「一定時間メッセージを受信できたら成功」とみなす簡易チェックとする。
    """

    def __init__(self, max_retry: int = 3, listen_seconds: int = 20):
        self.max_retry = max_retry
        self.listen_seconds = listen_seconds
        self.received_count = 0
        self._retry_count = 0
        self._stop = threading.Event()

    def _on_message(self, ws, message):
        self.received_count += 1
        try:
            data = json.loads(message)
            symbol = data.get("Symbol")
            price = data.get("CurrentPrice")
            print(f"[PUSH受信 #{self.received_count}] Symbol={symbol} CurrentPrice={price}")
        except json.JSONDecodeError:
            print(f"[PUSH受信 #{self.received_count}] (JSON解析失敗) {message[:100]}")

    def _on_error(self, ws, error):
        print(f"[WARN] WebSocketエラー: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[INFO] WebSocket切断: code={close_status_code} msg={close_msg}")
        if self._stop.is_set():
            return
        self._retry_count += 1
        if self._retry_count > self.max_retry:
            print(f"[NG] 再接続を{self.max_retry}回試行しましたが失敗しました。"
                  f"（本番実装ではここでRiskManagerに保有ポジション強制決済を通知する）")
            return
        wait = 2 ** self._retry_count  # 簡易バックオフ
        print(f"[INFO] {wait}秒後に再接続を試みます（{self._retry_count}/{self.max_retry}回目）")
        time.sleep(wait)
        self._connect()

    def _on_open(self, ws):
        print("[OK] WebSocket接続確立")
        self._retry_count = 0  # 接続成功したらリトライカウントをリセット

    def _connect(self):
        if websocket is None:
            print("[NG] websocket-client がインストールされていません。"
                  "`pip install websocket-client` を実行してください。")
            return
        self.ws = websocket.WebSocketApp(
            VERIFY_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever()

    def run(self):
        if websocket is None:
            return
        t = threading.Thread(target=self._connect, daemon=True)
        t.start()
        print(f"[INFO] {self.listen_seconds}秒間PUSH配信を待機します...")
        time.sleep(self.listen_seconds)
        self._stop.set()
        try:
            self.ws.close()
        except Exception:
            pass

        if self.received_count > 0:
            print(f"[OK] PUSH API疎通確認：{self.received_count}件のメッセージを受信しました")
        else:
            print("[NG] PUSH APIでメッセージを受信できませんでした。"
                  "銘柄登録が正しいか、取引時間内か（前場9:00-11:30/後場12:30-15:00）を確認してください。")


def main():
    parser = argparse.ArgumentParser(description="kabuステーション検証用環境 疎通確認")
    parser.add_argument("--password", default=os.environ.get("KABU_API_PASSWORD"),
                         help="検証用APIパスワード（未指定時は環境変数 KABU_API_PASSWORD を使用）")
    parser.add_argument("--symbol", default="9432", help="疎通確認に使う銘柄コード（既定: 9432 = NTT）")
    parser.add_argument("--exchange", type=int, default=1, help="市場コード（既定: 1 = 東証）")
    parser.add_argument("--listen-seconds", type=int, default=20, help="PUSH受信を待機する秒数")
    args = parser.parse_args()

    if not args.password:
        print("エラー: APIパスワードが指定されていません。"
              " --password オプションか環境変数 KABU_API_PASSWORD を設定してください。")
        sys.exit(1)

    print("=== 1. トークン認証確認 ===")
    try:
        token = get_token(args.password)
        print(f"[OK] トークン取得成功: {token[:8]}...(以下省略)")
    except Exception as e:
        print(f"[NG] トークン取得失敗: {e}")
        print("kabuステーション®が起動しているか、検証用のAPIパスワードが正しいか確認してください。")
        sys.exit(1)

    print("\n=== 2. REST API疎通確認（残高照会） ===")
    check_wallet(token)

    print("\n=== 3. 銘柄登録確認 ===")
    register_symbol(token, args.symbol, args.exchange)

    print("\n=== 4. PUSH API(WebSocket)疎通確認 ===")
    push_check = PushConnectivityCheck(max_retry=3, listen_seconds=args.listen_seconds)
    push_check.run()

    print("\n=== 疎通確認完了 ===")


if __name__ == "__main__":
    main()
