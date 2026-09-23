import hashlib
import json
import sqlite3
import time


def rules_fingerprint(rules):
    """
    规则集指纹：rules 列表的规范化 JSON 的 sha256。

    指纹随规则内容变化（含顺序、优先级），不受字典文件排版/空白影响。
    规则集变了 → 索引里存的旧指纹不等 → changed() 为 True → 该文件重新解析。
    """

    payload = json.dumps(
        rules,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


class FileIndex:

    def __init__(
        self,
        db,
        rules_version=None
    ):

        self.conn = sqlite3.connect(
            db
        )

        # None 表示「不启用规则版本判定」，行为与旧版一致
        self.rules_version = rules_version

        self.create()

    def create(self):

        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_index
        (

        path TEXT PRIMARY KEY,

        size INTEGER,

        mtime REAL,

        last_scan REAL,

        rules_version TEXT

        )
        """
        )

        self.conn.commit()

        self.migrate()

    def migrate(self):

        """
        兼容老库：file_index 若是 4 列旧结构，补 rules_version 列。

        直接 ALTER 已存在该列的库会报 duplicate column name，所以先查 PRAGMA。
        """

        columns = {

            row[1]

            for row in self.conn.execute(
            """
            PRAGMA table_info(file_index)
            """
            )

        }

        if "rules_version" not in columns:

            self.conn.execute(
            """
            ALTER TABLE file_index

            ADD COLUMN rules_version TEXT
            """
            )

            self.conn.commit()

    def changed(
        self,
        path,
        size,
        mtime
    ):

        row = self.conn.execute(
        """
        SELECT size,mtime,rules_version

        FROM file_index

        WHERE path=?

        """,
        (
            path,
        )
        ).fetchone()

        if not row:

            return True

        if (
            row[0] != size
            or
            row[1] != mtime
        ):

            return True

        return self.rules_changed(
            row[2]
        )

    def rules_changed(
        self,
        stored_version
    ):

        if self.rules_version is None:

            return False

        return stored_version != self.rules_version

    def update(
        self,
        path,
        size,
        mtime
    ):

        self.conn.execute(
        """
        INSERT OR REPLACE

        INTO file_index
        (
            path,
            size,
            mtime,
            last_scan,
            rules_version
        )

        VALUES(?,?,?,?,?)

        """,
        (
            path,
            size,
            mtime,
            time.time(),
            self.rules_version
        )
        )

        self.conn.commit()
