"""
entry_rule / exit_rule のルール定義と評価ロジック。

CLAUDE.mdの方針どおり、当面はGUI上でのJSON直接編集を前提とした
シンプルなAND/OR条件の組み合わせをサポートする。

JSON形式（基本設計書3節の例に準拠。ネストしたグループもサポートする拡張版）:

    {
        "operator": "AND",
        "conditions": [
            {"indicator": "rsi14", "field": "rsi14", "op": "<", "value": 30},
            {"indicator": "vwap",  "field": "deviation_pct", "op": "<", "value": -1.0},
            {
                "operator": "OR",
                "conditions": [
                    {"indicator": "bb", "field": "position", "op": "==", "value": "BELOW_LOWER"},
                    {"indicator": "sma_short", "field": "sma5", "op": "<", "value": "sma_long.sma25"}
                ]
            }
        ]
    }

"indicator" は indicator_params で定義したインスタンスキー
（例: "sma_short"）を指す。"field" はそのインジケータの value() が
返す辞書のキー（例: "sma5"）。

"value" が文字列で "他のインジケータキー.フィールド名" の形式
（例: "sma_long.sma25"）の場合は、定数ではなく別インジケータの
現在値と比較する（移動平均のゴールデンクロス等に使用）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Union

_OPERATORS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@dataclass
class RuleCondition:
    indicator: str
    field_name: str
    op: str
    value: Any

    def __post_init__(self):
        if self.op not in _OPERATORS:
            raise ValueError(
                f"未対応の演算子です: '{self.op}'（対応: {', '.join(_OPERATORS)}）"
            )

    def evaluate(self, indicator_values: Dict[str, Dict[str, Any]]) -> bool:
        """
        indicator_values: {"sma_short": {"sma5": 1234.5}, "rsi14": {"rsi14": 28.3}, ...}
        いずれかの指標がまだ準備できておらず値がNoneの場合は False とみなす
        （データ不足時に誤ってシグナルを出さないための安全側の挙動）。
        """
        left = self._lookup(indicator_values, self.indicator, self.field_name)
        if left is None:
            return False

        right = self.value
        if isinstance(right, str) and "." in right:
            ref_indicator, ref_field = right.split(".", 1)
            right = self._lookup(indicator_values, ref_indicator, ref_field)
            if right is None:
                return False

        return _OPERATORS[self.op](left, right)

    @staticmethod
    def _lookup(indicator_values: Dict[str, Dict[str, Any]], indicator: str, field_name: str):
        indicator_dict = indicator_values.get(indicator)
        if indicator_dict is None:
            return None
        return indicator_dict.get(field_name)


@dataclass
class RuleGroup:
    operator: str  # "AND" or "OR"
    conditions: List[Union["RuleGroup", RuleCondition]] = field(default_factory=list)

    def __post_init__(self):
        op = self.operator.upper()
        if op not in ("AND", "OR"):
            raise ValueError(f"operatorは AND/OR のみ対応しています: '{self.operator}'")
        self.operator = op

    def evaluate(self, indicator_values: Dict[str, Dict[str, Any]]) -> bool:
        if not self.conditions:
            return False
        results = (c.evaluate(indicator_values) for c in self.conditions)
        return any(results) if self.operator == "OR" else all(results)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuleGroup":
        conditions: List[Union["RuleGroup", RuleCondition]] = []
        for item in data.get("conditions", []):
            if "operator" in item and "conditions" in item:
                conditions.append(RuleGroup.from_dict(item))
            else:
                conditions.append(
                    RuleCondition(
                        indicator=item["indicator"],
                        field_name=item["field"],
                        op=item["op"],
                        value=item["value"],
                    )
                )
        return cls(operator=data.get("operator", "AND"), conditions=conditions)


def load_rule(data: Dict[str, Any] | None) -> RuleGroup:
    """
    設定ファイルのentry_rule/exit_ruleセクション（dict、または未設定でNone）を
    RuleGroupへ変換する。未設定の場合は「常にFalse」となる空のANDグループを返す
    （ルール未設定時に誤ってシグナルが出続けないようにする安全側の既定値）。
    """
    if not data:
        return RuleGroup(operator="AND", conditions=[])
    return RuleGroup.from_dict(data)
