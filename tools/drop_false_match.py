# -*- coding: utf-8 -*-
"""清除「误匹配」的番号记录（只动库记录，**不碰磁盘文件**）。

用途：matcher 收紧之后，库里还留着收紧之前认错的条目。它们不会自己消失
（重新扫描只会新增，不会删旧），所以要显式清一次。

⚠️ 与 `lookup_notfound` 的剔除**不是一回事**：
  * `lookup_notfound`：站上**没有**这个番号 -> D8 闸门剔除
  * 本脚本 `false_match`：站上**有**，但我们把它认到了错的文件上
把两者混记会让「查不到」的统计说谎，所以原因码分开。

用法：
    python tools/drop_false_match.py --db storage/library_v2.db FH-27
    python tools/drop_false_match.py --db storage/library_v2.db --dry-run FH-27

每条被清的番号都会：
  1. 把它的每个文件写进 filter_log（留痕，reason=false_match）
  2. 删 magnets / metadata / comments / media_files / titles 里的关联行
  3. **不动磁盘文件** —— 本工具只清理媒体库记录
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import filter_log                                   # noqa: E402
from core.database_v2 import Database                         # noqa: E402


def _rows(db, number):
    return db.conn.execute(
        """
        SELECT m.id, m.filepath, m.filename, m.size
        FROM media_files m
        JOIN titles t ON t.id = m.title_id
        WHERE t.number = ?
        """,
        (number,),
    ).fetchall()


def drop(db, number, dry_run=False):
    """返回 (文件数, 是否存在)。dry_run 只报数不改库。"""

    title = db.conn.execute(
        "SELECT id FROM titles WHERE number = ?", (number,)
    ).fetchone()

    if not title:
        return 0, False

    files = _rows(db, number)

    print("  {} 个文件：".format(len(files)))

    for _fid, filepath, filename, _size in files:

        print("    {}".format(filepath))

        if not dry_run:

            # 留痕：以后「这个文件怎么不在库里了」查这里能查到
            filter_log.record(
                db,
                path=filepath,
                filename=filename,
                size=_size,
                number=number,
                reason="false_match",
                detail=(
                    "目录名误匹配 —— 识别出的 {} 并非本文件"
                    "（站上另有其片）。已从媒体库剔除，磁盘文件未动。"
                ).format(number),
                stage="lookup",
            )

    if dry_run:
        return len(files), True

    tid = title["id"]

    # comments 表没有 title_id 的级联，得自己删
    for sql in (
        "DELETE FROM comments WHERE title_id = ?",
        "DELETE FROM magnets WHERE title_id = ?",
        "DELETE FROM metadata WHERE title_id = ?",
        "DELETE FROM media_files WHERE title_id = ?",
        "DELETE FROM titles WHERE id = ?",
    ):
        db.conn.execute(sql, (tid,))

    filter_log.flush(db)
    db.conn.commit()

    return len(files), True


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("numbers", nargs="+")
    ap.add_argument("--db", required=True, help="媒体库路径")
    ap.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("找不到库：{}".format(args.db))
        return 2

    db = Database(args.db)

    print("库：{}".format(args.db))

    if args.dry_run:
        print("（dry-run：只报数，不改库）")

    total = 0
    missing = []

    for number in args.numbers:

        print("\n[{}]".format(number))

        n, found = drop(db, number, dry_run=args.dry_run)

        if not found:
            print("  库里没有这个番号")
            missing.append(number)
            continue

        total += n

        print("  -> 清理 {} 个文件记录".format(n))

    print("\n合计清理 {} 个文件记录".format(total))

    if missing:
        print("未找到：{}".format(" ".join(missing)))

    if not args.dry_run:
        print("\n磁盘文件未做任何改动。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
