"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/matcher_v2.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
修 bug 请改 `core/matcher_v2.py`，不要在这里改。
v2 为唯一主线，v1 仅作只读迁移源。
"""
import re


class Matcher:

    def __init__(
        self,
        codes
    ):

        self.codes = codes

    def match(
        self,
        text
    ):

        text = text.upper()

        results = []

        for code in self.codes:

            patterns = [

                rf"{code}[-_ ]?(\d{{2,6}})",

                rf"{code}(\d{{2,6}})"
            ]

            for p in patterns:

                found = re.findall(
                    p,
                    text
                )

                for number in found:

                    results.append(
                        {
                            "number":
                                f"{code}-{number}",

                            "score":
                                self.score(
                                    text,
                                    code,
                                    number
                                )
                        }
                    )

        return results

    def score(
        self,
        text,
        code,
        number
    ):

        score = 50

        if f"{code}-{number}" in text:

            score += 50

        if code in text:

            score += 10

        return min(
            score,
            100
        )
