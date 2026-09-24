import sqlite3
import os
import time
import json

from core.av_format import is_typical_av_ext
from core.database_guard import DatabaseGuard
from core.magnet_judge import is_trusted_recognition


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

        # WAL：读写并行。默认的 delete 模式是**读写互斥** —— 抓取线程每写一条
        # 就独占锁，Web 端每次刷新都要等它，实测表现为"抓取时页面非常卡"。
        # WAL 下读不阻塞写、写不阻塞读，这正是本工具的使用形态（后台抓取 +
        # 前台浏览）。busy_timeout 兜住偶发竞争，避免直接抛 database is locked。
        try:

            self.conn.execute("PRAGMA journal_mode=WAL")

            self.conn.execute("PRAGMA busy_timeout=10000")

            self.conn.execute("PRAGMA synchronous=NORMAL")

        except sqlite3.DatabaseError:

            # 只读文件系统等极端情况下不能让构造失败，退回默认行为
            pass

        self.create()

        # 顺序要紧：先建（补齐缺失的表），再迁（给已存在的表补列）。
        # 反过来会在新库上撞 `no such table`。
        self.migrate()

    def create(self):

        self.create_titles()

        self.create_media_files()

        self.create_metadata()

        self.create_file_index()

        self.create_magnets()

        self.create_comments()

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

    def migrate(self):
        """老库补列。

        直接 ALTER 已存在该列的库会报 duplicate column name，先查 PRAGMA
        （与 core/file_index.py 的迁移写法一致）。

        2026-09-23 新增（D9/D11 落地）：
          * `titles.tier`            档位（极低/低/高/极高）
          * `titles.correct_magnets` 正确磁力条数（判定器算出来的）
          * `titles.comments_count`  javdb 评论数（定「极高」用）
          * `titles.javdb_number`    javdb 返回的番号（D11 第二个条件）
          * `titles.norm_number`     归一化键（比对/查重用）
          * `titles.evidence_strong` >=10 条正确磁力（仅展示/排序）
          * `magnets.is_correct`     这条磁力是否属于该番号

        ⚠️ 只对**已存在**的表补列。表还没建就跳过 —— 新库由 `create()`
        里的建表语句直接带上这些列，不需要 ALTER。
        （早期版本没做这个判断，新库构造时直接 `no such table: magnets`。）
        """

        def add_columns(table, wanted):
            exists = self.conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            ).fetchone()

            if not exists:
                return

            have = {
                row[1]
                for row in self.conn.execute(
                    "PRAGMA table_info({})".format(table)
                )
            }

            for name, decl in wanted:
                if name not in have:
                    self.conn.execute(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(
                            table, name, decl
                        )
                    )

            self.conn.commit()

        add_columns("metadata", [
            ("screenshots", "TEXT"),

            # 宣传视频的直接地址（javdb `assets list --type video` 给的
            # m3u8）。主人 09-24 要求「接在截图最后、可预览」。
            #
            # ⚠️ 这是个**带签名的临时链接**（`?sign=...&t=...`），会过期。
            # 所以只当"存下来备用"，抓不到就抓不到，不拿它当必须品 ——
            # 过期后重抓一次即可。
            ("preview_video", "TEXT"),
        ])

        # titles：档位与证据字段
        add_columns("titles", [
            ("tier", "TEXT"),
            ("correct_magnets", "INTEGER"),
            ("comments_count", "INTEGER"),
            ("javdb_number", "TEXT"),
            ("norm_number", "TEXT"),
            ("evidence_strong", "INTEGER DEFAULT 0"),
            ("lookup_state", "TEXT"),
            ("lookup_source", "TEXT"),
            ("number_matches", "INTEGER"),

            # ❤️ 收藏位。0/1，默认 0。
            #
            # 主人 2026-09-24 定：收藏是**纯人工标记**，不参与任何自动判定 ——
            # 既不抬高置信分，也不解锁删除（那是 tier/磁力门控的事）。
            # 它的作用是「我想留着看的」，所以只用来筛选与排序。
            ("favorite", "INTEGER DEFAULT 0"),
        ])

        # magnets：单条判定结果
        add_columns("magnets", [
            ("is_correct", "INTEGER DEFAULT 0"),
            ("magnet_hash", "TEXT"),
            ("name", "TEXT"),
        ])

        # media_files：识别来源（D11 第三条要用）
        #
        # `match_source` 记 matcher 是用哪种形态认出这个番号的
        # （std / fc2 / numpfx / nosep / bare ...）。批量删要求它来自
        # 高置信主路径 —— 无分隔符与裸数字形态不可信，见 core/magnet_judge.py。
        add_columns("media_files", [
            ("match_source", "TEXT"),
            ("match_confidence", "INTEGER"),

            # ── 本地文件已删（送回收站了），但**卡要留着** ──
            #
            # 主人 2026-09-24 定：这个工具的用处是「保卡」—— 卡是这部片的
            # 档案（番号/元数据/磁力/截图），删源文件只是做磁盘管理。
            # 早前删完直接 `DELETE FROM media_files`，而首页卡片是
            # `FROM media_files JOIN titles` 驱动的，于是**卡跟着没了** ——
            # 与意图正好相反。
            #
            # 现在改为**标记**：行留着（filepath 也留着做参考），
            # 只是不再算进「本地文件数/占用」，扫描/时长/打开位置也跳过。
            # 若文件日后又出现（重下），重扫会把它 upsert 回来并清零此位。
            ("local_deleted", "INTEGER DEFAULT 0"),
            ("deleted_time", "REAL"),
            # 时长校验（**只作置信度评分，不作硬标准**）
            #
            # 主人 2026-09-24 定：AV 在传播时常被加几分钟广告，或同目录下
            # 放个几分钟的番号预览片，所以时长差异**不能**当拒绝理由。
            # 存下来供打分与展示。
            ("duration_local", "REAL"),
            ("duration_ref", "REAL"),
            ("duration_ratio", "REAL"),
        ])

        # 筛选日志：**被挡下的都要留痕**（主人 2026-09-24 要求）
        #
        # 之前这些只存在扫描任务的**内存**里（`_SCAN_JOB["skipped_samples"]`），
        # 任务一结束就没了 —— 用户看不到"哪些文件被筛掉了、为什么"，
        # 也无从复查。改为落库。
        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_log
        (
            id INTEGER PRIMARY KEY,

            path TEXT,
            filename TEXT,
            size INTEGER,

            number TEXT,
            confidence INTEGER,
            source TEXT,

            -- 机器可读的原因码，见 core/filter_log.py 的 REASONS
            reason TEXT,
            -- 人话说明（给界面直接显示）
            detail TEXT,
            -- 发生在哪一步：scan / lookup / tier / duration / extension
            stage TEXT,

            created_time REAL
        )
        """
        )

        self.conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_filter_log_reason
        ON filter_log(reason);
        """
        )

        self.conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_filter_log_path
        ON filter_log(path);
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

    def create_comments(self):
        """javdb 的评论文本。

        以前只存 `titles.comments_count`（定「极高」档用的数字），评论**正文**
        看过就丢 —— 主人 09-24 要求把评论放进卡片，所以要落库。

        去重键用 `javdb_id`（javdb 的评论 id）：同一条评论重复抓不会翻倍。
        老库里的评论没有 id 时留空，靠 `UNIQUE` 允许 NULL 重复（SQLite 里
        NULL 互不相等）—— 不会因此报错，只是那几条可能重复。
        """

        self.conn.executescript(
        """

        CREATE TABLE IF NOT EXISTS comments
        (

            id INTEGER PRIMARY KEY,

            title_id INTEGER,

            javdb_id TEXT,

            content TEXT,

            score INTEGER,

            likes_count INTEGER,

            username TEXT,

            created_at TEXT,

            fetched_time REAL,


            FOREIGN KEY(title_id)

            REFERENCES titles(id)

        );


        CREATE INDEX IF NOT EXISTS idx_comments_title

        ON comments(title_id);


        CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_javdb

        ON comments(title_id, javdb_id);

        """
        )

        self.conn.commit()

    def save_comments(self, title_id, comments):
        """写入评论（按 javdb_id 幂等）。返回新写入条数。

        `comments` 是 `[{"content","score","likes_count","username",
        "created_at","id"}, ...]`（字段与 javdb comments --json 的
        reviews 对齐，缺的按 None 存）。
        """

        added = 0

        for c in comments or []:

            if not isinstance(c, dict) or not (c.get("content") or "").strip():

                continue

            try:

                cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO comments
                (title_id, javdb_id, content, score, likes_count,
                 username, created_at, fetched_time)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    title_id,
                    str(c.get("id")) if c.get("id") is not None else None,
                    (c.get("content") or "").strip(),
                    c.get("score"),
                    c.get("likes_count"),
                    c.get("username"),
                    c.get("created_at"),
                    __import__("time").time(),
                ))

                added += cur.rowcount or 0

            except Exception:                                   # noqa: BLE001
                continue

        self.conn.commit()

        return added

    def comments_of(self, title_id, limit=20):
        """取评论：先按赞数、再按时间。"""

        try:

            rows = self.conn.execute(
            """
            SELECT content, score, likes_count, username,
                   created_at, javdb_id
            FROM comments
            WHERE title_id = ?
            ORDER BY COALESCE(likes_count, 0) DESC,
                     created_at DESC
            LIMIT ?
            """,
            (title_id, limit)).fetchall()

        except Exception:                                       # noqa: BLE001
            return []

        return [
            {
                "content": r["content"],
                "score": r["score"],
                "likes_count": r["likes_count"],
                "username": r["username"],
                "created_at": r["created_at"],
                "javdb_id": r["javdb_id"],
            }
            for r in rows
        ]

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
        size=None,
        match_source=None,
        match_confidence=None
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
            match_source,
            match_confidence,
            created_time
        )

        VALUES
        (?,?,?,?,?,?,?,?)

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
            ),

            -- 识别来源同样 COALESCE：老库重扫时若这次没带 source，
            -- 保留上一次记下来的，别把已知信息抹成 NULL
            -- （NULL 在 D11 第三条里等于「不可批量删」）。
            match_source = COALESCE(
                excluded.match_source,
                media_files.match_source
            ),

            match_confidence = COALESCE(
                excluded.match_confidence,
                media_files.match_confidence
            ),

            -- 文件又出现了（重下/移回来）-> 撤销「本地已删」标记。
            -- 扫描看到的才是事实：磁盘上有它，就不该再算作已删。
            local_deleted = 0,
            deleted_time = NULL

        """,
        (
            title_id,
            filepath,
            os.path.basename(filepath),
            file_hash,
            size,
            match_source,
            match_confidence,
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

        # 只回**存活**文件：已送回收站的路径再报给调用方，会被当成
        # 「这东西还在磁盘上」—— 是误导。
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

        AND COALESCE(media_files.local_deleted, 0) = 0

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

    def save_tier_evidence(self, title_id, evidence):
        """把判定器的结果落到 titles / magnets。

        ``evidence`` 由 `core.magnet_judge.judge()` + `tier_of()` 产出：
            {number, correct, comments, tier, javdb_number, matches, magnets:[...]}

        这段是 D9 分档与 D11 双条件的**唯一**数据来源 —— 前端展示、删除门控
        都读这里，不各自重算（避免两处算法漂移）。
        """

        from core.magnet_judge import (
            is_evidence_strong,
            normalize_number,
        )

        correct = int(evidence.get("correct") or 0)

        self.conn.execute(
        """
        UPDATE titles
        SET tier = ?,
            correct_magnets = ?,
            comments_count = ?,
            javdb_number = ?,
            norm_number = ?,
            evidence_strong = ?,
            number_matches = ?,
            lookup_source = COALESCE(?, lookup_source)
        WHERE id = ?
        """,
            (
                evidence.get("tier"),
                correct,
                evidence.get("comments"),
                evidence.get("javdb_number"),
                normalize_number(evidence.get("number") or ""),
                1 if is_evidence_strong(correct) else 0,
                1 if evidence.get("matches") else 0,
                evidence.get("source"),
                title_id,
            ),
        )

        for m in evidence.get("magnets") or []:

            self.conn.execute(
            """
            UPDATE magnets
            SET is_correct = ?, magnet_hash = ?, name = ?
            WHERE title_id = ? AND magnet = ?
            """,
                (
                    1 if m.get("is_correct") else 0,
                    m.get("hash"),
                    m.get("name"),
                    title_id,
                    m.get("magnet"),
                ),
            )

        self.conn.commit()

    def deletion_eligibility(self):
        """按 D9/D10/D11 算出每个番号的删除资格。

        取代旧的 `deletable_titles()`（那个只看 `verified=1`，1 条挂错的
        磁力就能放行 —— P0-1）。

        返回 `{number, tier, correct_magnets, number_matches,
                 batch, manual, blocked_reason, bytes, files}`：

          * `batch`  = 可批量删（档位 >= 高 **且** number 核对通过）
          * `manual` = 只能逐条手动删（档位 = 低）
          * 极低档一律不可删（blocked_reason = 'tier_too_low'）

        **没跑过判定的番号 tier 为空 -> 一律不可删。** 这是有意的：
        宁可漏删，不可错删。跑完 M2 的查询定档后它们才会有档位。
        """

        rows = self.conn.execute(
        """
        SELECT
            titles.id AS title_id,
            titles.number AS number,
            titles.tier AS tier,
            titles.correct_magnets AS correct_magnets,
            titles.number_matches AS number_matches,
            COALESCE((
                SELECT SUM(COALESCE(media_files.size, 0))
                FROM media_files
                WHERE media_files.title_id = titles.id
                  AND COALESCE(media_files.local_deleted, 0) = 0
            ), 0) AS bytes
        FROM titles
        ORDER BY titles.number
        """
        ).fetchall()

        # 文件清单单独取。
        #
        # ⚠️ 不要用 `MAX(filepath)` 在 SQL 里「挑一个代表文件」—— 那取决于
        # 字母序，而不是实质。实测后果：某番号同时有 `.mp4` 与 `.webm` 时，
        # 判成合格还是不合格取决于哪个字母在前（初版就这样漏了 6 个）。
        #
        # 批量删会删掉**该番号的全部文件**，所以第三条要求**所有**文件都合格。
        files_by_title = {}

        for f in self.conn.execute(
            # ⚠️ 必须排除**已送回收站**的文件（`local_deleted=1`）。
            #
            # 否则「一键送回收站」跑完一轮后，这些番号的文件行还在、
            # 会被继续算成「可批量删」—— 实测后果：候选数一直是 285 部，
            # 弹窗第二轮仍声称能腾 852 GB，但文件早就不在了。**误导。**
            "SELECT title_id, filepath, match_source, match_confidence "
            "FROM media_files WHERE COALESCE(local_deleted, 0) = 0"
        ):
            files_by_title.setdefault(f[0], []).append({
                "filepath": f[1],
                "source": f[2],
                "confidence": f[3],
            })

        out = []

        for r in rows:

            tier = r["tier"]
            matches = bool(r["number_matches"])

            flist = files_by_title.get(r["title_id"], [])

            total_files = len(flist)

            atypical = [
                f for f in flist
                if not is_typical_av_ext(f["filepath"])
            ]

            untrusted = [
                f for f in flist
                if not is_trusted_recognition(f["source"], f["confidence"])
            ]

            if not tier:
                batch = manual = False
                reason = "not_evaluated"
            elif tier == "极低":
                batch = manual = False
                reason = "tier_too_low"
            elif tier == "低":
                batch = False
                manual = True
                reason = "tier_low_manual_only"
            elif not matches:
                batch = False
                manual = True
                reason = "number_mismatch"
            elif total_files and atypical:
                # 该番号下有非典型格式的文件（`.webm`、无扩展名、
                # 游戏内视频段……）。批量删会一并删掉它，所以整条转手动。
                batch = False
                manual = True
                reason = "atypical_extension"
            elif total_files and untrusted:
                # 有文件不是从高置信主路径识别的（无分隔符 / 裸数字 /
                # 剥噪音兜底）—— 这正是「识别成另一个热门码」的高发形态。
                batch = False
                manual = True
                reason = "untrusted_recognition"
            elif not total_files:
                # 番号还在但文件已删（D13 档案）—— 没有可删的文件，
                # 不算批量删候选。
                batch = False
                manual = False
                reason = "no_files"
            else:
                batch = True
                manual = True
                reason = ""

            out.append({
                "title_id": r["title_id"],
                "number": r["number"],
                "tier": tier,
                "correct_magnets": r["correct_magnets"],
                "number_matches": matches,
                "batch": batch,
                "manual": manual,
                "blocked_reason": reason,
                "bytes": r["bytes"],
                "files": total_files,
                "atypical_files": len(atypical),
                "untrusted_files": len(untrusted),
                "file_list": flist,
            })

        return out

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
                  AND COALESCE(media_files.local_deleted, 0) = 0
            ) AS file_count,
            (
                SELECT COALESCE(SUM(COALESCE(media_files.size, 0)), 0)
                FROM media_files
                WHERE media_files.title_id = titles.id
                  AND COALESCE(media_files.local_deleted, 0) = 0
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
              AND COALESCE(local_deleted, 0) = 0
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
