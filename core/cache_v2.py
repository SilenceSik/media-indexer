"""⚠️ 未接线模块（v2 主线下无引用者）。

`core/cache_v2.py` 是 v2 版扫描缓存，但 `services/scan_service.py` 目前
没有用它——也就是说**重复扫描不会命中缓存，每次都重算**。
这是已知缺口（记在 ARCHITECTURE-AND-ISSUES.md），不是 bug 但影响大库性能。
"""
import os
import hashlib


class Cache:

    def __init__(
        self,
        folder
    ):

        self.folder = folder

        os.makedirs(
            folder,
            exist_ok=True
        )

    def path(
        self,
        url
    ):

        name = hashlib.md5(
            url.encode()
        ).hexdigest()

        ext = os.path.splitext(
            url
        )[1]

        return os.path.join(
            self.folder,
            name + ext
        )
