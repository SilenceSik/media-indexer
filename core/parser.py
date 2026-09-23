"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/parser_v2.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
修 bug / 加规则请改 `core/parser_v2.py`，不要在这里改。
本模块已冻结：v2 为唯一主线，v1 仅作只读迁移源。
"""
import json
import re

from core.normalizer import Normalizer


class NumberParser:

    def __init__(
        self,
        dictionary
    ):

        with open(
            dictionary,
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        self.rules = data["rules"]

    def parse(
        self,
        filename
    ):

        text = Normalizer.clean(
            filename
        )

        result = []

        for rule in self.rules:

            matches = re.findall(
                rule["pattern"],
                text.upper()
            )

            for item in matches:

                number = item.replace(
                    "_",
                    "-"
                )

                result.append(
                    {
                        "number":
                            number,

                        "score":
                            rule["priority"],

                        "source":
                            "dictionary"
                    }
                )

        # 去重

        final = []

        exists = set()

        for item in result:

            if item["number"] not in exists:

                exists.add(
                    item["number"]
                )

                final.append(
                    item
                )

        return final
