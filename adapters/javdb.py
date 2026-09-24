"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `adapters/javdb_adapter.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
v2 为唯一主线，v1 仅作只读迁移源。
"""
import subprocess
import json


class JavdbClient:

    def __init__(
        self,
        command="javdb"
    ):

        self.command = command

    def query(
        self,
        number
    ):

        try:

            result = subprocess.run(
                [
                    self.command,
                    "search",
                    number,
                    "--json"
                ],

                capture_output=True,

                text=True,

                timeout=30
            )

            if result.returncode != 0:

                return None

            return json.loads(
                result.stdout
            )

        except Exception:

            return None
