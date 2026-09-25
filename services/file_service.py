"""文件操作服务：打开目录 / 送回收站 / 保存种子 / 扫空目录。

**删除的硬约束（README 明文的契约，不得绕过）**：删除动作只消费
`Database.deletable_titles()` 的产出 —— 即「有 verified=1 磁力记录」的番号。
没磁力的条目即使文件存在也一律拒绝，因为「错的条目拿不到磁力」，
磁力是否存在比文件名像不像更能证明这条记录是真的、且可重新获取。

一切删除走 send2trash（回收站），**绝不永久删除**。
"""

import os
import subprocess

from core.trash_guard import recyclable_reason


class FileService:

    def __init__(
        self,
        db,
        torrent_dir,
        rules=None
    ):

        self.db = db

        self.torrent_dir = torrent_dir

        # 番号规则：低分卡片清理要用它重放识别分数（分数不落库）
        self.rules = rules or []

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
        """该番号在库里的**存活**本地文件路径（不含已送回收站的）。"""

        title_id = self.db.get_or_create_title(number)

        rows = self.db.conn.execute(
            """
            SELECT filepath FROM media_files
            WHERE title_id=? AND COALESCE(local_deleted, 0) = 0
            """,
            (
                title_id,
            )
        ).fetchall()

        return [r["filepath"] for r in rows]

    def batch_candidates(self):
        """批量送回收站的候选（`deletion_eligibility()` 里 `batch=True` 的）。

        **只读**，给弹窗预览用 —— 主人要的「一键」必须先看得见要动什么：
        哪几部、几个文件、腾出多少空间。

        复用门控的结论，不另立判据：`batch=True` 已经要求
        档位>=高 + 番号核对通过 + 可信识别形态 + 格式典型。
        """

        out = []

        try:

            rows = self.db.deletion_eligibility()

        except Exception:                                       # noqa: BLE001

            return out

        for e in rows:

            if not e.get("batch"):

                continue

            if not e.get("files"):

                # 番号还在但文件已删 -> 没有可删的
                continue

            out.append({
                "number": e.get("number"),
                "tier": e.get("tier"),
                "files": e.get("files") or 0,
                "bytes": e.get("bytes") or 0,
            })

        out.sort(key=lambda x: x["bytes"], reverse=True)

        return out

    def delete_batch(self):
        """把「高置信可批量删」的番号文件**全部送回收站**（卡保留）。

        主人 2026-09-24 要的「一键」—— 但**不是无确认的一键**：
        界面会先弹窗列出「N 部 / X 个文件 / Y GB」，用户点确定才走到这里。

        与 `delete_title` 走**同一条**落盘与标记路径（`_trash_and_mark`），
        所以两边的行为保证一致：送回收站 + `local_deleted=1` + 卡不删。

        返回汇总 `{titles, files, bytes, failed, missing}`。
        """

        cands = self.batch_candidates()

        summary = {"titles": [], "files": 0, "bytes": 0,
                   "failed": [], "missing": []}

        for c in cands:

            ok, _msg, detail = self.delete_title(c["number"])

            if not ok:

                summary["failed"].extend(detail.get("failed") or [c["number"]])

            summary["titles"].append({
                "number": c["number"],
                "files": len(detail.get("deleted") or []),
            })

            summary["files"] += len(detail.get("deleted") or [])
            summary["bytes"] += c["bytes"]
            summary["missing"].extend(detail.get("missing") or [])

        return summary

    def _trash_and_mark(self, paths):
        """把一组路径送回收站，并在库里标记 `local_deleted`。

        抽出来给 `delete_title` / `delete_batch` 共用 —— 两边各写一份
        迟早会漂移（一边标记一边真删），那正是「保卡」最怕的。
        """

        detail = {"deleted": [], "missing": [], "failed": [], "blocked": {}}

        try:

            from send2trash import send2trash

        except ImportError:

            detail["failed"].append("缺少 send2trash 依赖（pip install Send2Trash）")

            return detail

        for path in paths:

            if not os.path.exists(path):

                detail["missing"].append(path)

                continue

            # ── 卷安全检查：进不了回收站的位置，不动手 ──
            # 对外承诺是「走回收站，随时可恢复」，那就得先确认这个位置
            # 真的有回收站。移动盘 / 网络盘 / exFAT 卷上 send2trash
            # 做不到「回收」这件事 —— 宁可漏删，也不能让「可恢复」
            # 变成一句空话。同一个卷的原因只报一次，免得刷屏。
            safe, why = recyclable_reason(path)

            if not safe:

                detail.setdefault("blocked", {}).setdefault(why, []).append(path)

                continue

            try:

                send2trash(os.path.normpath(path))

                detail["deleted"].append(path)

            except Exception as exc:                            # noqa: BLE001

                detail["failed"].append(f"{path}: {exc}")

        # ── 文件已不在磁盘 -> **标记，不删行** ──
        #
        # 主人 2026-09-24 定：这个工具的用处是「保卡」—— 卡是这部片的档案
        # （番号/元数据/磁力/截图），删源文件只是做磁盘管理。
        #
        # 早前这里是 `DELETE FROM media_files WHERE filepath=?`，而首页卡片
        # 是 `FROM media_files JOIN titles` 驱动的 —— 行一删，**卡跟着没了**，
        # 与意图正好相反。现在只打标记：卡留着，但本地文件数与占用归零。
        import time as _time

        for path in detail["deleted"]:

            self.db.conn.execute(
                """
                UPDATE media_files
                SET local_deleted = 1, deleted_time = ?
                WHERE filepath = ?
                """,
                (_time.time(), path),
            )

        self.db.conn.commit()

        return detail

    def delete_title(self, number):
        """把该番号的本地文件送回收站。返回 (是否成功, 消息, 明细)。

        **只消费 `deletable_titles()`**：不在可删清单里的直接拒绝，不做例外。
        ⚠️ 只送文件，**卡保留**（见 `_trash_and_mark` 的说明）。
        """

        allowed = self.deletable_index()

        if number not in allowed:

            return False, "该番号没有已验证磁力，按门控规则不可删除", {
                "deleted": [],
                "missing": [],
                "failed": [],
                "blocked": {},
            }

        detail = self._trash_and_mark(self.files_of(number))

        if detail["failed"]:

            return False, f"部分失败（{len(detail['failed'])} 个）", detail

        # 卷安全检查拦下的：文件还在，只是没动。要讲清为什么没动 ——
        # 否则用户看到「已删 0 个」会以为程序坏了。
        if detail.get("blocked"):

            reasons = "；".join(
                "%s（%d 个文件）" % (why, len(files))
                for why, files in detail["blocked"].items()
            )

            return False, "为保护数据未删除：%s" % reasons, detail

        return True, f"已送回收站 {len(detail['deleted'])} 个文件（卡片保留）", detail

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

    # ------------------------------------------------------------ 低分卡片清理

    def low_score_entries(self, max_score, parser=None):
        """找出库内识别分低于 max_score 的条目。

        ⚠️ 分数**不在库里存** —— media_files 没有 confidence 列。所以这里
        按存下来的文件名重新解析一遍取分。识别是纯函数，重放结果一致。

        返回 [{filepath, filename, number, confidence, score}...]，只读不删。
        """

        from core.parser_v2 import Parser

        if parser is None:

            parser = Parser(self.rules)

        rows = self.db.conn.execute(
            """
            SELECT m.filepath, m.filename, t.number
            FROM media_files m
            JOIN titles t ON t.id = m.title_id
            WHERE COALESCE(m.local_deleted, 0) = 0
            """
        ).fetchall()

        out = []

        for r in rows:

            # 与 scan_service.parse_file 同序：先文件名，无命中再退父目录
            nums = parser.parse(os.path.basename(r["filepath"] or ""))

            if not nums:

                parent = os.path.basename(
                    os.path.dirname(r["filepath"] or "")
                )

                if parent:

                    nums = parser.parse(parent)

            best = max(
                nums,
                key=lambda x: x.get("confidence", 0),
            ) if nums else None

            score = best["confidence"] if best else 0

            if score <= max_score:

                out.append(
                    {
                        "filepath": r["filepath"],
                        "filename": r["filename"],
                        "number": r["number"],
                        "parsed": best["number"] if best else None,
                        "confidence": score,
                    }
                )

        out.sort(key=lambda x: x["confidence"], reverse=True)

        return out

    def remove_entries(self, filepaths):
        """把指定文件**从媒体库摘除**（只删库记录，绝不碰磁盘上的文件）。

        摘除后孤立无文件的 titles 一并清掉，否则标题列表里会留空壳。
        """

        removed = 0

        for path in filepaths:

            cur = self.db.conn.execute(
                "DELETE FROM media_files WHERE filepath=?",
                (path,),
            )

            removed += cur.rowcount or 0

        # 清掉没有任何文件挂靠的 titles（含其后延的 metadata/magnets）
        orphans = [
            r[0]
            for r in self.db.conn.execute(
                """
                SELECT id FROM titles
                WHERE id NOT IN (SELECT title_id FROM media_files)
                """
            )
        ]

        for tid in orphans:

            for table in ("metadata", "magnets"):

                self.db.conn.execute(
                    f"DELETE FROM {table} WHERE title_id=?",               # noqa: S608
                    (tid,),
                )

            self.db.conn.execute("DELETE FROM titles WHERE id=?", (tid,))

        self.db.conn.commit()

        return removed, len(orphans)

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

                # 同一条卷安全检查 —— 空目录也在源文件所在卷上，
                # 进不了回收站就不动手（理由与 _trash_and_mark 一致）
                safe, why = recyclable_reason(path)

                if not safe:

                    failed.append(f"{path}: 为保护数据未删除 —— {why}")

                    continue

                send2trash(os.path.normpath(path))

                ok += 1

            except Exception as exc:                            # noqa: BLE001

                failed.append(f"{path}: {exc}")

        return ok, failed
