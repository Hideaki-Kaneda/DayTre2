"""
アプリケーションのエントリポイント。

現時点ではGUIの起動のみを行う。kabuステーションAPIへの接続
（api.RestClient / api.PushClient を使った本番監視ループの起動）は、
次段の統合作業でここに追加する想定
（CLAUDE.mdの「未実装・詳細設計待ちの項目」を参照）。
"""

import logging
import sys

from PySide6.QtWidgets import QApplication

from gui import MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
