"""文件操作服务：打开目录 / 送回收站 / 保存种子 / 扫空目录。

**删除的硬约束（README 明文的契约，不得绕过）**：删除动作只消费
`Database.deletable_titles()` 的产出 —— 即「有 verified=1 磁力记录」的番号。
没磁力的条目即使文件存在也一律拒绝，因为「错的条目拿不到磁力」，
磁力是否存在比文件名像不像更能证明这条记录是真的、且可重新获取。

一切删除走 send2trash（回收站），**绝不永久删除**。
"""

import os
import subprocess
import sys


class FileService:

    def __init__(
        self,
        db,
        torrent_dir
    ):

        self.db = db

        self.torrent_dir = torrent_dir

    # ------------------------------------------------------------ 打开目录

    @staticmethod
    def open_in_explorer(path):
        """在资源管理器里定位文件；路径不存在时退到父目录。

        只在**文件确实存在**时才打开 —— 对不存在的路径一路向上找祖先目录
        会开到一个与用户预期无关的地方（实测会把测试临时目录当"最近上级"打开），
        所以这里明确失败而不是猜。
        """

        if not path:

            return False, "路径为空"

        norm = os.path.normpath(path)

        if os.path.isfile(norm):

            # /select 会打开父目录并选中该文件
            subprocess.Popen(["explorer", "/select,", norm])

            return True, "已在资源管理器中定位文件"

        if os.path.isdir(norm):

            subprocess.Popen(["explorer", norm])

            return True, "已打开目录"

        return False, f"路径不存在：{path}"

    # ------------------------------------------------------------ 删除

    def deletable_index(self):
        """当前允许删除的番号集合（消费 deletable_titles，唯一门槛）。"""

        rows = self.db.deletable_titles()

        return {
            row["number"]: row
            for row in rows
        }

    def files_of(self, number):
        """该番号在库里的全部本地文件路径。"""

        title_id = self.db.get_or_create_title(number)

        rows = self.db.conn.execute(
            """
            SELECT filepath FROM media_files WHERE title_id=?
            """,
            (
                title_id,
            )
        ).fetchall()

        return [r["filepath"] for r in rows]

    def delete_title(self, number):
        """把该番号的本地文件送回收站。返回 (是否成功, 消息, 明细)。

        **只消费 deletable_titles()**：不在可删清单里的直接拒绝，不做例外。
        """

        allowed = self.deletable_index()

        if number not in allowed:

            return False, "该番号没有已验证磁力，按门控规则不可删除", {
                "deleted": [],
                "missing": [],
                "failed": [],
            }

        try:

            from send2trash import send2trash

        except ImportError:

            return False, "缺少 send2trash 依赖（pip install Send2Trash）", {
                "deleted": [],
                "missing": [],
                "failed": [],
            }

        detail = {
            "deleted": [],
            "missing": [],
            "failed": [],
        }

        for path in self.files_of(number):

            if not os.path.exists(path):

                detail["missing"].append(path)

                continue

            try:

                send2trash(os.path.normpath(path))

                detail["deleted"].append(path)

            except Exception as exc:                            # noqa: BLE001

                detail["failed"].append(f"{path}: {exc}")

        # 文件已不在磁盘 -> 从库里摘掉关联，避免下次扫描前一直挂着
        for path in detail["deleted"]:

            self.db.conn.execute(
                "DELETE FROM media_files WHERE filepath=?",
                (
                    path,
                )
            )

        self.db.conn.commit()

        if detail["failed"]:

            return False, f"部分失败（{len(detail['failed'])} 个）", detail

        return True, f"已送回收站 {len(detail['deleted'])} 个文件", detail

    # ------------------------------------------------------------ 保存种子

    def save_torrent(self, number):
        """把该番号的磁力写成一个 .torrent 文件。

        `.torrent` 是已下载文件的**备份**（磁力本身只是 hash，丢了就得重找），
        所以保存成文件放项目目录里，方便再分发或留底。
        """

        # ⚠️ magnets_for_title 收的是**番号字符串**（精确匹配），不是 title_id
        magnets = self.db.magnets_for_title(number)

        if not magnets:

            return False, "该番号没有磁力记录", None

        os.makedirs(self.torrent_dir, exist_ok=True)

        out = os.path.join(self.torrent_dir, f"{number}.txt")

        lines = []

        for m in magnets:

            tag = "已验证" if m.get("verified") else "候选"

            size = m.get("size_text") or "未知"

            src = m.get("source") or ""

            lines.append(f"# {tag} · {size} · {src}\n{m['magnet']}\n")

        with open(out, "w", encoding="utf-8") as fh:

            fh.write(f"# {number} 磁力 {len(magnets)} 条\n\n")

            fh.write("\n".join(lines))

        return True, f"已保存 {len(magnets)} 条磁力", out

    def save_all_torrents(self):
        """把库里所有有磁力的番号各存一份。返回 (成功数, 失败数, 目录)。"""

        rows = self.db.conn.execute(
            """
            SELECT DISTINCT titles.number AS number
            FROM titles
            JOIN magnets ON magnets.title_id = titles.id
            ORDER BY titles.number
            """
        ).fetchall()

        ok = fail = 0

        for r in rows:

            good, _, _ = self.save_torrent(r["number"])

            if good:

                ok += 1

            else:

                fail += 1

        return ok, fail, self.torrent_dir

    # ------------------------------------------------------------ 空目录

    def scan_empty_dirs(self, roots):
        """找出「不含任何文件」的目录（自底向上）。

        只读：仅返回候选清单，不删。判定用「树内一个文件都没有」，
        即整棵子树都是空目录 —— 单个空目录但有非空子目录的不算。

        **扫描根自身永不入选** —— 把用户配置的扫描根送回收站是灾难性的，
        即便它当前为空（可能只是还没放东西）。只清它下面的层级。
        """

        empties = []

        empty_set = set()

        roots_abs = {
            os.path.normcase(os.path.abspath(r))
            for r in roots
            if r
        }

        for root in roots:

            if not os.path.isdir(root):

                continue

            for current, dirs, files in os.walk(root, topdown=False):

                if os.path.normcase(os.path.abspath(current)) in roots_abs:

                    continue

                # 该目录自身没有文件，且**所有子目录都判过是空的** -> 整棵空。
                # 必须查 empty_set 而不是「访问过的目录」：后者对任何目录都成立
                # （父目录总会看到子目录被走过），实测会把
                # `a/game/images/x.webm` 的 a/game 误判为空。
                if files:

                    continue

                children = [
                    os.path.join(current, d)
                    for d in dirs
                ]

                if all(child in empty_set for child in children):

                    empties.append(current)

                    empty_set.add(current)

        return sorted(empties)

    def delete_empty_dirs(self, dirs):
        """把空目录送回收站。返回 (成功数, 失败列表)。

        走 send2trash 而非 os.rmdir —— 与项目「不永久删除」的约束一致。
        """

        try:

            from send2trash import send2trash

        except ImportError:

            return 0, ["缺少 send2trash 依赖（pip install Send2Trash）"]

        ok = 0

        failed = []

        # 深的先删，避免父目录还非空
        for path in sorted(dirs, key=len, reverse=True):

            if not os.path.isdir(path):

                continue

            try:

                # 只删仍然为空的（扫描到删除之间可能有变动）
                if os.listdir(path):

                    continue

                send2trash(os.path.normpath(path))

                ok += 1

            except Exception as exc:                            # noqa: BLE001

                failed.append(f"{path}: {exc}")

        return ok, failed
