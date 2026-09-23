import sqlite3
import os
import time
import json

from core.database_guard import DatabaseGuard


class Database:

    def __init__(
        self,
        path
    ):

        folder = os.path.dirname(
            path
        )

        if folder:

            os.makedirs(
                folder,
                exist_ok=True
            )

        DatabaseGuard().check(
            path
        )

        self.conn = sqlite3.connect(
            path
        )

        self.conn.row_factory = sqlite3.Row

        self.create()

    def create(self):

        self.create_titles()

        self.create_media_files()

        self.create_metadata()

        self.create_file_index()

        self.create_magnets()

    def create_titles(self):

        self.conn.executescript(
        """

        CREATE TABLE IF NOT EXISTS titles
        (
            id INTEGER PRIMARY KEY,

            number TEXT UNIQUE NOT NULL,

            title TEXT,

            maker TEXT,

            created_time REAL
        );


        CREATE INDEX IF NOT EXISTS idx_number

        ON titles(number);

        """
        )

        self.conn.commit()

    def create_media_files(self):

        self.conn.executescript(
        """

        CREATE TABLE IF NOT EXISTS media_files
        (
            id INTEGER PRIMARY KEY,

            title_id INTEGER,

            filepath TEXT UNIQUE NOT NULL,

            filename TEXT,

            file_hash TEXT,

            size INTEGER,

            created_time REAL,

            FOREIGN KEY(title_id)
            REFERENCES titles(id)
        );


        CREATE INDEX IF NOT EXISTS idx_path

        ON media_files(filepath);

        """
        )

        self.conn.commit()

    def create_metadata_table(self):

        self.conn.execute(
        """

        CREATE TABLE IF NOT EXISTS metadata
        (

            id INTEGER PRIMARY KEY,

            title_id INTEGER UNIQUE,

            title TEXT,

            cover TEXT,

            cover_local TEXT,

            release_date TEXT,

            maker TEXT,

            actresses TEXT,

            tags TEXT,

            screenshots TEXT,

            updated_time REAL,


            FOREIGN KEY(title_id)

            REFERENCES titles(id)

        )

        """
        )

        self.conn.commit()

        self.migrate()

    def migrate(self):
        """老库补列：metadata 缺 screenshots 时补上。

        直接 ALTER 已存在该列的库会报 duplicate column name，先查 PRAGMA
        （与 core/file_index.py 的迁移写法一致）。
        """

        columns = {

            row[1]

            for row in self.conn.execute(
            """
            PRAGMA table_info(metadata)
            """
            )

        }

        if "screenshots" not in columns:

            self.conn.execute(
            """
            ALTER TABLE metadata

            ADD COLUMN screenshots TEXT
            """
            )

            self.conn.commit()

    def create_metadata(self):

        self.create_metadata_table()

    def create_file_index(self):

        self.conn.execute(
        """

        CREATE TABLE IF NOT EXISTS file_index
        (

            path TEXT PRIMARY KEY,

            size INTEGER,

            mtime REAL,

            last_scan REAL

        )

        """
        )

        self.conn.commit()

    def create_magnets(self):

        self.conn.executescript(
        """

        CREATE TABLE IF NOT EXISTS magnets
        (

            id INTEGER PRIMARY KEY,

            title_id INTEGER,

            magnet TEXT NOT NULL,

            source TEXT,

            size_text TEXT,

            verified INTEGER DEFAULT 0,

            created_time REAL,


            FOREIGN KEY(title_id)

            REFERENCES titles(id)

        );


        CREATE INDEX IF NOT EXISTS idx_magnet_title

        ON magnets(title_id);


        CREATE UNIQUE INDEX IF NOT EXISTS idx_magnet_title_uri

        ON magnets(title_id, magnet);

        """
        )

        self.conn.commit()

    def get_or_create_title(
        self,
        number
    ):

        row = self.conn.execute(
        """
        SELECT id

        FROM titles

        WHERE number=?

        """,
        (
            number,
        )
        ).fetchone()

        if row:

            return row["id"]

        cur = self.conn.execute(
        """
        INSERT INTO titles
        (
            number,
            created_time
        )

        VALUES
        (?,?)

        """,
        (
            number,
            time.time()
        )
        )

        self.conn.commit()

        return cur.lastrowid

    def add_file(
        self,
        number,
        filepath,
        file_hash=None,
        size=None
    ):

        """
        落库一条 media_files（幂等 UPSERT）。

        同一 filepath 二次解析出不同番号时（C3 改字典后重扫是真实场景）：
          - 旧行为 INSERT OR IGNORE：整行跳过 → title_id 永远指向旧番号，
            且 size / filename 永不更新（扫描无法自愈）
          - 现行为 ON CONFLICT(filepath) DO UPDATE：重链 title_id + 刷新
            filename / size，同一条语句内完成（不引入任何 DELETE）

        size / file_hash 用 COALESCE(excluded.x, media_files.x)：
        新值为 NULL 时保留旧值，避免「文件暂时 stat 不到」把已知信息抹掉。
        created_time 保留首次入库时间，不随重扫刷新。
        """

        old_title_id = self.title_id_for_file(
            filepath
        )

        title_id = self.reusable_title_id(
            filepath,
            number
        )

        if title_id is None:

            title_id = self.get_or_create_title(
                number
            )

        self.conn.execute(
        """
        INSERT INTO media_files

        (
            title_id,
            filepath,
            filename,
            file_hash,
            size,
            created_time
        )

        VALUES
        (?,?,?,?,?,?)

        ON CONFLICT(filepath) DO UPDATE SET

            title_id = excluded.title_id,

            filename = excluded.filename,

            file_hash = COALESCE(
                excluded.file_hash,
                media_files.file_hash
            ),

            size = COALESCE(
                excluded.size,
                media_files.size
            )

        """,
        (
            title_id,
            filepath,
            os.path.basename(filepath),
            file_hash,
            size,
            time.time()
        )
        )

        self.conn.commit()

        return {

            "title_id":
                title_id,

            "previous_title_id":
                old_title_id

        }

    def title_id_for_file(
        self,
        filepath
    ):

        """
        该 filepath 当前挂在哪个 title 上（没有则 None）。

        先查再写，用于观测「重链」是否发生；同时避免为同一 filepath
        重复建 title。
        """

        row = self.conn.execute(
        """
        SELECT title_id

        FROM media_files

        WHERE filepath=?

        """,
        (
            filepath,
        )
        ).fetchone()

        if not row:

            return None

        return row["title_id"]

    def reusable_title_id(
        self,
        filepath,
        number
    ):

        """
        「能复用就不新建 title」：重扫同一 filepath 且番号未变时，
        直接复用现有 title_id，不产生任何新行。

        只有番号确实变了（C3 修字典后的纠错场景）才返回 None，
        由调用方走 get_or_create_title() 重建关联。
        """

        old_title_id = self.title_id_for_file(
            filepath
        )

        if old_title_id is None:

            return None

        for row in self.conn.execute(
        """
        SELECT number

        FROM titles

        WHERE id=?

        """,
        (
            old_title_id,
        )
        ):

            if row["number"] == number:

                return old_title_id

        return None

    def save_metadata(
        self,
        data
    ):

        title_id = self.get_or_create_title(
            data["number"]
        )

        self.conn.execute(
        """

        INSERT OR REPLACE INTO metadata

        (

        title_id,

        title,

        cover,

        cover_local,

        release_date,

        maker,

        actresses,

        tags,

        screenshots,

        updated_time

        )


        VALUES(?,?,?,?,?,?,?,?,?,?)

        """,

        (

        title_id,

        data.get("title"),

        data.get("cover"),

        data.get("cover_local"),

        data.get("release_date"),

        data.get("maker"),

        json.dumps(
            data.get("actresses", [])
        ),

        json.dumps(
            data.get("tags", [])
        ),

        json.dumps(
            data.get("screenshots", [])
        ),

        time.time()

        )

        )

        self.conn.commit()

    def search_number(
        self,
        number
    ):

        rows = self.conn.execute(
        """
        SELECT

        titles.number,

        media_files.filepath


        FROM titles


        JOIN media_files

        ON titles.id =
        media_files.title_id


        WHERE titles.number=?

        """,
        (
            number,
        )
        ).fetchall()

        return [
            dict(x)
            for x in rows
        ]

    # ─────────────────────────────── 磁力 / 可删数据（D5） ───────────────────────────────

    def add_magnet(
        self,
        number,
        magnet,
        source=None,
        size_text=None,
        verified=0,
        commit=True
    ):
        """落一条磁力。幂等：(title_id, magnet) 已存在则只补空字段，不新增行。

        返回 dict：{"id", "title_id", "created"}，created 标明本次是否新插行。

        commit=False 供批量导入用：由调用方统一提交，避免逐行 fsync
        （实测 3795 行逐行提交在 Windows 上要 30s，批量提交后 <1s）。
        默认 True，行为与逐条调用完全一致。
        """

        title_id = self.get_or_create_title(
            number
        )

        row = self.conn.execute(
        """
        SELECT id, source, size_text, verified
        FROM magnets
        WHERE title_id=? AND magnet=?
        """,
        (
            title_id,
            magnet
        )
        ).fetchone()

        if row:

            self.conn.execute(
            """
            UPDATE magnets
            SET
                source = COALESCE(?, source),
                size_text = COALESCE(?, size_text),
                verified = MAX(verified, ?)
            WHERE id=?
            """,
            (
                source,
                size_text,
                int(verified or 0),
                row["id"]
            )
            )

            if commit:

                self.conn.commit()

            return {
                "id": row["id"],
                "title_id": title_id,
                "created": False,
            }

        cur = self.conn.execute(
        """
        INSERT OR IGNORE INTO magnets
        (
            title_id,
            magnet,
            source,
            size_text,
            verified,
            created_time
        )

        VALUES
        (?,?,?,?,?,?)
        """,
        (
            title_id,
            magnet,
            source,
            size_text,
            int(verified or 0),
            time.time()
        )
        )

        if commit:

            self.conn.commit()

        return {
            "id": cur.lastrowid,
            "title_id": title_id,
            "created": bool(cur.rowcount),
        }

    def magnets_for_title(
        self,
        number
    ):
        """取某番号下的磁力列表。无此番号返回 []。

        ⚠️ number 是精确匹配。verify_cache 的 key 与落库番号可能差一个补零位
        （tsv 里 `PPT-18`，落库为 matched 的 `PPT-018`），查历史数据的 56 个
        非补零写法会拿到 [] —— 求稳一律用 deletable_titles() 返回的 number。
        """

        row = self.conn.execute(
        """
        SELECT id
        FROM titles
        WHERE number=?
        """,
        (
            number,
        )
        ).fetchone()

        if not row:

            return []

        rows = self.conn.execute(
        """
        SELECT
            magnets.id,
            magnets.magnet,
            magnets.source,
            magnets.size_text,
            magnets.verified,
            magnets.created_time
        FROM magnets
        WHERE magnets.title_id=?
        ORDER BY magnets.id
        """,
        (
            row["id"],
        )
        ).fetchall()

        return [
            dict(x)
            for x in rows
        ]

    def deletable_titles(
        self
    ):
        """有「已验证磁力」的番号 + 其本地文件与总占用字节。

        只统计 verified=1 的磁力（verified=0 是候选，不足以判定可删）。
        总占用走 media_files.size 求和，NULL 当 0。
        """

        rows = self.conn.execute(
        """
        SELECT
            titles.id AS title_id,
            titles.number AS number,
            (
                SELECT COUNT(*)
                FROM magnets
                WHERE magnets.title_id = titles.id
                AND magnets.verified = 1
            ) AS magnet_count,
            (
                SELECT COUNT(*)
                FROM media_files
                WHERE media_files.title_id = titles.id
            ) AS file_count,
            (
                SELECT COALESCE(SUM(COALESCE(media_files.size, 0)), 0)
                FROM media_files
                WHERE media_files.title_id = titles.id
            ) AS total_bytes
        FROM titles
        WHERE EXISTS
        (
            SELECT 1
            FROM magnets
            WHERE magnets.title_id = titles.id
            AND magnets.verified = 1
        )
        ORDER BY titles.number
        """
        ).fetchall()

        out = []

        for row in rows:

            files = self.conn.execute(
            """
            SELECT filepath, size
            FROM media_files
            WHERE title_id=?
            ORDER BY id
            """,
            (
                row["title_id"],
            )
            ).fetchall()

            out.append({
                "title_id": row["title_id"],
                "number": row["number"],
                "magnet_count": row["magnet_count"],
                "file_count": row["file_count"],
                "total_bytes": row["total_bytes"],
                "files": [
                    {"filepath": x["filepath"], "size": x["size"]}
                    for x in files
                ],
            })

        return out
