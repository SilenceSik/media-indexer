"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/cache_v2.py`。

保留原因：作为 v1 数据库的只读迁移参考。v2 链路不引用本文件。
本模块已冻结：v2 为唯一主线，v1 仅作只读迁移源。
"""
import os
import sqlite3
import time


class ScanCache:

    def __init__(self, db):

        self.conn = sqlite3.connect(
            db
        )

        self.create()

    def create(self):

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_cache
            (
                filepath TEXT PRIMARY KEY,
                size INTEGER,
                modify_time REAL,
                scan_time REAL
            )
            """
        )

        self.conn.commit()

    def need_scan(
        self,
        filepath
    ):

        stat = os.stat(
            filepath
        )

        size = stat.st_size
        mtime = stat.st_mtime

        row = self.conn.execute(
            """
            SELECT size, modify_time
            FROM scan_cache
            WHERE filepath=?
            """,
            (
                filepath,
            )
        ).fetchone()

        if row is None:

            return True

        old_size, old_time = row

        if (
            old_size != size
            or old_time != mtime
        ):

            return True

        return False

    def update(
        self,
        filepath
    ):

        stat = os.stat(
            filepath
        )

        self.conn.execute(
            """
            INSERT OR REPLACE INTO scan_cache
            VALUES(?,?,?,?)
            """,
            (
                filepath,
                stat.st_size,
                stat.st_mtime,
                time.time()
            )
        )

        self.conn.commit()
