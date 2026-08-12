"""
ConfigManager：通常設定（機密情報を含まない）の読み書き。

基本設計書のパッケージ構成 config/settings.py に対応。
機密情報（APIトークン等）はここでは扱わない
（別途 SecretsManager で ENC(...) 形式の暗号化ファイルとして管理する想定。
 CLAUDE.mdの「セキュリティ・設定管理」を参照。今回のGUI実装のスコープ外）。
"""

import json
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "default_config.json"


class ConfigManager:
    def __init__(self, config_path: Path | str | None = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        with open(self.config_path, encoding="utf-8") as f:
            self._data = json.load(f)
        return self._data

    def save(self, path: Path | str | None = None) -> None:
        target = Path(path) if path else self.config_path
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
