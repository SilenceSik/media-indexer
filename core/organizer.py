"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/organizer_v2.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
⚠️ 本模块含真实文件移动实现（`shutil.move`）。v2 的移动走
`services/organize_service.py`，落盘前须过删除门控（见 P0/P1 门控契约）。
本模块已冻结：v2 为唯一主线，v1 仅作只读迁移源。
"""
import os
import shutil
import json
import hashlib


class Organizer:

    def __init__(
        self,
        library_path,
        mode="move"
    ):

        self.library_path = library_path

        self.mode = mode

        os.makedirs(
            library_path,
            exist_ok=True
        )

    def hash_file(
        self,
        path
    ):

        h = hashlib.sha256()

        with open(
            path,
            "rb"
        ) as f:

            for chunk in iter(
                lambda: f.read(1024 * 1024),
                b""
            ):

                h.update(
                    chunk
                )

        return h.hexdigest()

    def safe_target(
        self,
        path
    ):

        if not os.path.exists(path):

            return path

        base, ext = os.path.splitext(
            path
        )

        index = 1

        while True:

            new = f"{base}_{index}{ext}"

            if not os.path.exists(new):

                return new

            index += 1

    def organize(
        self,
        number,
        filepath,
        metadata=None
    ):

        folder = os.path.join(
            self.library_path,
            number
        )

        os.makedirs(
            folder,
            exist_ok=True
        )

        target = self.safe_target(
            os.path.join(
                folder,
                os.path.basename(filepath)
            )
        )

        source_hash = self.hash_file(
            filepath
        )

        if self.mode == "copy":

            shutil.copy2(
                filepath,
                target
            )

        else:

            shutil.copy2(
                filepath,
                target
            )

            target_hash = self.hash_file(
                target
            )

            if source_hash != target_hash:

                os.remove(
                    target
                )

                raise Exception(
                    "hash verify failed"
                )

            os.remove(
                filepath
            )

        if metadata:

            with open(
                os.path.join(
                    folder,
                    "info.json"
                ),
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    metadata,
                    f,
                    ensure_ascii=False,
                    indent=4
                )

        return target
