"""
設定タブ（基本設計書7.2節）。

監視銘柄数・1銘柄あたり株数・損切ライン・日次利益上限・日次最大損失・
強制引け決済時刻をGUIから設定でき、entry_rule/exit_ruleはJSON直接編集
（CLAUDE.md確定方針）で管理する。
"""

import json
from typing import Any, Dict

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)


class SettingsTab(QWidget):
    settings_saved = Signal(dict)

    def __init__(self, initial_settings: Dict[str, Any], parent=None):
        super().__init__(parent)

        self.watchlist_size = QSpinBox()
        self.watchlist_size.setRange(1, 50)  # kabuステーションAPIの銘柄登録上限(目安50)に合わせる

        self.shares_per_symbol = QSpinBox()
        self.shares_per_symbol.setRange(100, 1_000_000)
        self.shares_per_symbol.setSingleStep(100)

        self.trailing_multiplier = QDoubleSpinBox()
        self.trailing_multiplier.setRange(0.5, 10.0)
        self.trailing_multiplier.setSingleStep(0.1)
        self.trailing_multiplier.setDecimals(2)
        self.trailing_multiplier.setToolTip(
            "トレール決済の基準幅 = AR（寄り付き直後の値幅目安）× この倍率。\n"
            "保有中の最高値からこの幅だけ下落したら成行決済する。\n"
            "既定値2.0（AR×2）。固定%の損切りは廃止し、この方式に統一した。"
        )

        self.bar_interval_minutes = QSpinBox()
        self.bar_interval_minutes.setRange(1, 30)
        self.bar_interval_minutes.setToolTip(
            "本番監視で指標計算に使う足の間隔（分）。PUSH配信の生Tickをこの間隔で\n"
            "集約してから指標を更新する。AR（当日の値幅目安）の算出本数とも連動する。"
        )

        self.reentry_cooldown_bars = QSpinBox()
        self.reentry_cooldown_bars.setRange(0, 30)
        self.reentry_cooldown_bars.setToolTip(
            "決済後、同一銘柄への再エントリーを禁止するバー本数（既定3本）。\n"
            "バー間隔（上の「足の間隔」）×この本数が実際の無エントリー時間になる。\n"
            "0にすると無効化（決済直後でも条件が揃えばすぐ再エントリーする）。"
        )

        self.pg_config_path = QLineEdit()
        self.pg_config_path.setPlaceholderText(r"例: C:\Users\kaneda\ClaudeCode\minute_data_import\config.ini")
        self.pg_config_path.setToolTip(
            "1分足の4本値・出来高をPostgreSQL（equities_bars_minuteテーブル）へ\n"
            "書き込む場合のconfig.iniパス（別バッチ minute_data_import と同じテーブルを共有）。\n"
            "空欄の場合はDB書き込みを行わない。"
        )

        self.daily_profit_target = QSpinBox()
        self.daily_profit_target.setRange(0, 10_000_000)
        self.daily_profit_target.setSingleStep(1000)

        self.daily_max_loss = QSpinBox()
        self.daily_max_loss.setRange(0, 10_000_000)
        self.daily_max_loss.setSingleStep(1000)

        self.force_close_time = QTimeEdit()

        self.entry_start_time = QTimeEdit()
        self.entry_start_time.setToolTip(
            "この時刻より前は新規エントリーを行わない。\n"
            "寄り付き直後（9:00〜9:15頃）はボラティリティが高くリスクが大きいため、\n"
            "例えば09:12等に設定して序盤の値動きを避けられる（決済は対象外）。"
        )

        self.push_reconnect_max_retry = QSpinBox()
        self.push_reconnect_max_retry.setRange(0, 10)

        basic_group = QGroupBox("基本設定")
        form = QFormLayout()
        form.addRow("監視銘柄数", self.watchlist_size)
        form.addRow("1銘柄あたり株数", self.shares_per_symbol)
        form.addRow("トレール倍率（AR×倍率）", self.trailing_multiplier)
        form.addRow("足の間隔（分）", self.bar_interval_minutes)
        form.addRow("再エントリー禁止バー数", self.reentry_cooldown_bars)
        form.addRow("PostgreSQL設定ファイル(config.ini)パス", self.pg_config_path)
        form.addRow("日次利益上限（円）", self.daily_profit_target)
        form.addRow("日次最大損失（円）", self.daily_max_loss)
        form.addRow("新規エントリー開始時刻", self.entry_start_time)
        form.addRow("強制引け決済時刻", self.force_close_time)
        form.addRow("PUSH再接続リトライ回数", self.push_reconnect_max_retry)
        basic_group.setLayout(form)

        rule_group = QGroupBox("シグナルルール（指標定義・entry_rule・exit_ruleをJSONで直接編集）")
        rule_layout = QVBoxLayout()
        rule_layout.addWidget(QLabel(
            "indicators / entry_rule / exit_rule をJSON形式でまとめて編集します。"
            "\n新しい指標を使いたい場合は indicators に追記してください"
            "（対応する指標クラスをindicators/registry.pyに登録済みであること）。"
        ))
        self.rule_json_edit = QPlainTextEdit()
        self.rule_json_edit.setPlaceholderText('{"indicators": {...}, "entry_rule": {...}, "exit_rule": {...}}')
        rule_layout.addWidget(self.rule_json_edit)
        rule_group.setLayout(rule_layout)

        self.save_button = QPushButton("保存")
        self.save_button.clicked.connect(self._on_save_clicked)

        layout = QVBoxLayout()
        layout.addWidget(basic_group)
        layout.addWidget(rule_group)
        layout.addWidget(self.save_button)
        self.setLayout(layout)

        self.load_settings(initial_settings)

    def load_settings(self, settings: Dict[str, Any]) -> None:
        self.watchlist_size.setValue(int(settings.get("watchlist_size", 30)))
        self.shares_per_symbol.setValue(int(settings.get("shares_per_symbol", 100)))
        self.trailing_multiplier.setValue(float(settings.get("trailing_multiplier", 2.0)))
        self.bar_interval_minutes.setValue(int(settings.get("bar_interval_minutes", 3)))
        self.reentry_cooldown_bars.setValue(int(settings.get("reentry_cooldown_bars", 3)))
        self.pg_config_path.setText(settings.get("pg_config_path", ""))
        self.daily_profit_target.setValue(int(settings.get("daily_profit_target", 20000)))
        self.daily_max_loss.setValue(int(settings.get("daily_max_loss", 20000)))

        force_close_str = settings.get("force_close_time", "15:00")
        h, m = (int(x) for x in force_close_str.split(":"))
        self.force_close_time.setTime(QTime(h, m))

        entry_start_str = settings.get("entry_start_time", "09:00")
        eh, em = (int(x) for x in entry_start_str.split(":"))
        self.entry_start_time.setTime(QTime(eh, em))

        self.push_reconnect_max_retry.setValue(int(settings.get("push_reconnect_max_retry", 3)))

        rule_payload = {
            "indicators": settings.get("indicators", {}),
            "entry_rule": settings.get("entry_rule", {}),
            "exit_rule": settings.get("exit_rule", {}),
        }
        self.rule_json_edit.setPlainText(json.dumps(rule_payload, ensure_ascii=False, indent=2))

    def collect_settings(self) -> Dict[str, Any]:
        """
        現在のGUI入力値を設定dictにまとめて返す。
        JSON部分のパースに失敗した場合は ValueError を送出する
        （呼び出し元でユーザーにエラーメッセージを表示すること）。
        """
        rule_payload = json.loads(self.rule_json_edit.toPlainText())
        for key in ("indicators", "entry_rule", "exit_rule"):
            if key not in rule_payload:
                raise ValueError(f"JSON内に '{key}' が見つかりません")

        qtime = self.force_close_time.time()
        entry_qtime = self.entry_start_time.time()
        return {
            "watchlist_size": self.watchlist_size.value(),
            "shares_per_symbol": self.shares_per_symbol.value(),
            "trailing_multiplier": self.trailing_multiplier.value(),
            "bar_interval_minutes": self.bar_interval_minutes.value(),
            "reentry_cooldown_bars": self.reentry_cooldown_bars.value(),
            "pg_config_path": self.pg_config_path.text().strip(),
            "daily_profit_target": self.daily_profit_target.value(),
            "daily_max_loss": self.daily_max_loss.value(),
            "entry_start_time": f"{entry_qtime.hour():02d}:{entry_qtime.minute():02d}",
            "force_close_time": f"{qtime.hour():02d}:{qtime.minute():02d}",
            "push_reconnect_max_retry": self.push_reconnect_max_retry.value(),
            "indicators": rule_payload["indicators"],
            "entry_rule": rule_payload["entry_rule"],
            "exit_rule": rule_payload["exit_rule"],
        }

    def _on_save_clicked(self) -> None:
        try:
            settings = self.collect_settings()
        except (ValueError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "設定エラー", f"シグナルルールのJSONが不正です:\n{e}")
            return
        self.settings_saved.emit(settings)
