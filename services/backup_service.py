"""媒体库导出 / 导入（压缩包）。

导出的是**库本身**：SQLite 主库 + 封面 + 内容截图 + 种子文本档，
打成一个 zip 便于搬机器或留底。

三条设计约束：

1. **截图占大头**（实测 276MB vs 数据库 1.5MB），所以导出必须支持
   「不含素材」—— 只要库和元数据时不必背几百兆。
2. **导入前先备份现有库**：导入是覆盖性操作，出错要能退回去。
3. **导入用 merge 而不是覆盖**：库是累积资产，合并才符合预期；
   同番号不重复建，文件按路径去重。
"""

import json
import os
import shutil
import sqlite3
import time
import zipfile

MANIFEST = "manifest.json"

VERSION = 1


class BackupService:

    def __init__(
        self,
        db_path,
        covers_dir,
        screenshots_dir,
        torrent_dir,
        export_dir
    ):

        self.db_path = db_path

        self.covers_dir = covers_dir

        self.screenshots_dir = screenshots_dir

        self.torrent_dir = torrent_dir

        self.export_dir = export_dir

    # ------------------------------------------------------------ 导出

    def export(self, include_images=True, name=None):
        """把媒体库打成 zip，返回 (zip 路径, 统计信息)。"""

        os.makedirs(self.export_dir, exist_ok=True)

        stamp = time.strftime("%Y%m%d-%H%M%S")

        base = name or f"media-library-{stamp}"

        out = os.path.join(self.export_dir, f"{base}.zip")

        stats = {
            "db": 0,
            "covers": 0,
            "screenshots": 0,
            "torrents": 0,
            "images": include_images,
        }

        # 一致性快照：WAL 模式下直接拷文件可能漏掉未 checkpoint 的页，
        # 用 sqlite 自己的 backup API 落一个干净副本。
        snap = os.path.join(self.export_dir, f".{base}.snapshot.db")

        self._snapshot(snap)

        try:

            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:

                z.write(snap, "library.db")

                stats["db"] = os.path.getsize(snap)

                if include_images:

                    stats["covers"] = self._add_tree(z, self.covers_dir, "covers")

                    stats["screenshots"] = self._add_tree(
                        z, self.screenshots_dir, "screenshots"
                    )

                stats["torrents"] = self._add_tree(z, self.torrent_dir, "torrents")

                z.writestr(
                    MANIFEST,
                    json.dumps(
                        {
                            "version": VERSION,
                            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "include_images": include_images,
                            "counts": self._counts(),
                            "files": stats,
                        },
                        ensure_ascii=False,
                        indent=1,
                    ),
                )

        finally:

            if os.path.exists(snap):

                os.remove(snap)

        return out, stats

    def _snapshot(self, target):
        """用 SQLite backup API 落一个一致副本。"""

        src = sqlite3.connect(self.db_path)

        try:

            dst = sqlite3.connect(target)

            try:

                src.backup(dst)

            finally:

                dst.close()

        finally:

            src.close()

    def _counts(self):
        """库里各类条数，写进 manifest 供导入后核对。"""

        try:

            c = sqlite3.connect(self.db_path)

            try:

                out = {}

                for table in ("titles", "media_files", "metadata", "magnets"):

                    try:

                        out[table] = c.execute(
                            f"SELECT COUNT(*) FROM {table}"                    # noqa: S608
                        ).fetchone()[0]

                    except sqlite3.Error:

                        out[table] = 0

                return out

            finally:

                c.close()

        except sqlite3.Error:

            return {}

    @staticmethod
    def _add_tree(z, root, prefix):
        """把一个目录树加进 zip，返回文件数。"""

        if not root or not os.path.isdir(root):

            return 0

        n = 0

        for current, _dirs, files in os.walk(root):

            for name in files:

                full = os.path.join(current, name)

                rel = os.path.relpath(full, root).replace("\\", "/")

                z.write(full, f"{prefix}/{rel}")

                n += 1

        return n

    # ------------------------------------------------------------ 导入

    def import_zip(self, zip_path, include_images=True):
        """把 zip 合并进现有库，返回 (统计信息, 备份路径)。

        合并语义：
          * 库记录按 title_id 重建 —— 用 `titles.number` 作稳定标识，
            已存在的番号不重复建；`media_files.filepath` 唯一，按路径去重。
          * 素材按文件名落到对应目录，同名不覆盖（已有的优先）。
        """

        if not os.path.exists(zip_path):

            raise FileNotFoundError(zip_path)

        backup = self._backup_current()

        stats = {
            "titles_added": 0,
            "files_added": 0,
            "magnets_added": 0,
            "metadata_added": 0,
            "covers": 0,
            "screenshots": 0,
            "torrents": 0,
            "backup": backup,
        }

        work = os.path.join(
            self.export_dir,
            f".import-{int(time.time())}",
        )

        os.makedirs(work, exist_ok=True)

        try:

            with zipfile.ZipFile(zip_path) as z:

                names = z.namelist()

                if "library.db" not in names:

                    raise ValueError("压缩包里没有 library.db，不是本工具的导出包")

                z.extract("library.db", work)

                if include_images:

                    for prefix, target, key in (
                        ("covers", self.covers_dir, "covers"),
                        ("screenshots", self.screenshots_dir, "screenshots"),
                        ("torrents", self.torrent_dir, "torrents"),
                    ):

                        stats[key] = self._extract_tree(z, names, prefix, target, work)

                else:

                    stats["torrents"] = self._extract_tree(
                        z, names, "torrents", self.torrent_dir, work
                    )

                self._merge_db(os.path.join(work, "library.db"), stats)

        finally:

            shutil.rmtree(work, ignore_errors=True)

        return stats, backup

    def _backup_current(self):
        """导入前把现有库另存一份（覆盖性操作的退路）。"""

        if not os.path.exists(self.db_path):

            return None

        stamp = time.strftime("%Y%m%d-%H%M%S")

        dst = f"{self.db_path}.before-import-{stamp}.bak"

        shutil.copy2(self.db_path, dst)

        return dst

    @staticmethod
    def _extract_tree(z, names, prefix, target, work):
        """把 zip 里某个前缀下的文件落到目标目录，同名不覆盖。"""

        if not target:

            return 0

        os.makedirs(target, exist_ok=True)

        n = 0

        head = f"{prefix}/"

        for name in names:

            if not name.startswith(head) or name.endswith("/"):

                continue

            rel = name[len(head):]

            if not rel:

                continue

            dst = os.path.join(target, rel.replace("/", os.sep))

            # 目录穿越防护：解析后必须仍在 target 内
            if not os.path.abspath(dst).startswith(os.path.abspath(target)):

                continue

            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if os.path.exists(dst):

                continue

            with z.open(name) as src, open(dst, "wb") as out:

                shutil.copyfileobj(src, out)

            n += 1

        return n

    def _merge_db(self, incoming_path, stats):
        """把导入库的记录合并进主库。"""

        dst = sqlite3.connect(self.db_path)

        dst.row_factory = sqlite3.Row

        src = sqlite3.connect(incoming_path)

        src.row_factory = sqlite3.Row

        try:

            for row in src.execute(
                """
                SELECT id, number, title, maker FROM titles
                """
            ):

                cur = dst.execute(
                    "SELECT id FROM titles WHERE number=?",
                    (row["number"],),
                ).fetchone()

                if cur:

                    new_id = cur["id"]

                else:

                    c = dst.execute(
                        "INSERT INTO titles (number, title, maker) VALUES (?,?,?)",
                        (row["number"], row["title"], row["maker"]),
                    )

                    new_id = c.lastrowid

                    stats["titles_added"] += 1

                stats["files_added"] += self._merge_files(
                    dst, src, row["id"], new_id
                )

                stats["magnets_added"] += self._merge_magnets(
                    dst, src, row["id"], new_id
                )

                stats["metadata_added"] += self._merge_metadata(
                    dst, src, row["id"], new_id
                )

            dst.commit()

        finally:

            src.close()

            dst.close()

    @staticmethod
    def _merge_files(dst, src, src_tid, dst_tid):
        """按 filepath 去重合并文件记录。"""

        n = 0

        for f in src.execute(
            "SELECT filepath, filename, size, local_deleted, deleted_time "
            "FROM media_files WHERE title_id=?",
            (src_tid,),
        ):

            exists = dst.execute(
                "SELECT 1 FROM media_files WHERE filepath=?",
                (f["filepath"],),
            ).fetchone()

            if exists:

                continue

            dst.execute(
                # 带上 local_deleted —— 「哪些已送回收站」是**人工策展的
                # 决定**，导出/导入一趟丢了它就等于把卡的状态改回「有文件」。
                "INSERT INTO media_files "
                "(title_id, filepath, filename, size, local_deleted, deleted_time)"
                " VALUES (?,?,?,?,?,?)",
                (dst_tid, f["filepath"], f["filename"], f["size"],
                 f["local_deleted"], f["deleted_time"]),
            )

            n += 1

        return n

    @staticmethod
    def _merge_magnets(dst, src, src_tid, dst_tid):
        """按 (title_id, magnet) 去重合并磁力。"""

        n = 0

        for m in src.execute(
            "SELECT magnet, source, size_text, verified FROM magnets WHERE title_id=?",
            (src_tid,),
        ):

            exists = dst.execute(
                "SELECT 1 FROM magnets WHERE title_id=? AND magnet=?",
                (dst_tid, m["magnet"]),
            ).fetchone()

            if exists:

                continue

            dst.execute(
                "INSERT INTO magnets (title_id, magnet, source, size_text, verified)"
                " VALUES (?,?,?,?,?)",
                (dst_tid, m["magnet"], m["source"], m["size_text"], m["verified"]),
            )

            n += 1

        return n

    @staticmethod
    def _merge_metadata(dst, src, src_tid, dst_tid):
        """元数据：只在目标没有时补，不覆盖现有。"""

        exists = dst.execute(
            "SELECT 1 FROM metadata WHERE title_id=?",
            (dst_tid,),
        ).fetchone()

        if exists:

            return 0

        row = src.execute(
            """
            SELECT title, cover, cover_local, release_date, maker,
                   actresses, tags, screenshots
            FROM metadata WHERE title_id=?
            """,
            (src_tid,),
        ).fetchone()

        if not row:

            return 0

        dst.execute(
            """
            INSERT INTO metadata
            (title_id, title, cover, cover_local, release_date, maker,
             actresses, tags, screenshots)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                dst_tid, row["title"], row["cover"], row["cover_local"],
                row["release_date"], row["maker"], row["actresses"],
                row["tags"], row["screenshots"],
            ),
        )

        return 1
