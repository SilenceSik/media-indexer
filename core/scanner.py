"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/scanner_v2.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
v2 的格式白名单（内置默认 + config 追加）在 `core/scanner_v2.py`。
本模块已冻结：v2 为唯一主线，v1 仅作只读迁移源。
"""
import os


class Scanner:

    def __init__(self, extensions):

        self.extensions = set(
            x.lower()
            for x in extensions
        )

    def scan(self, path):

        results = []

        for root, dirs, files in os.walk(path):

            for filename in files:

                ext = os.path.splitext(
                    filename
                )[1].lower()

                if ext in self.extensions:

                    results.append(
                        os.path.join(
                            root,
                            filename
                        )
                    )

        return results
