"""把「已实机验证的磁力清单」灌进框架 v2 的 `magnets` 表（C5 / D5）。

数据源（只读引用，不修改）：

  X:\\hermes\\library\\verify_cache.json
      番号 → {status, magnets, magnet_n, matched, movie_id, ...}
      ⚠️ magnets 字段历史上出现过三种写入格式，本脚本全部处理：
        (a) list  —— 每项含 title / size / cnsub / hd / uri          （当前 271 条）
        (b) int   —— 只有数量，无明细，需回落到 library.db 取 hash   （当前 17 条）
        (c) None  —— status='error' 查不到的番号                     （当前 32 条）

  X:\\hermes\\library\\library.db（可选，仅当显式传 --library-db 时启用）
      movies.magnets 列存 JSON 数组，每项含 hash / size / name ...
      hash 即 btih → 拼成 magnet:?xt=urn:btih:<hash>

解析优先级（逐条）：
  1. verify_cache 里该项自带非空 uri          → 直接用（当前 0 条，将来填了自动生效）
  2. 显式给了 --library-db 且能在其中对上番号 → 用 hash 拼 btih
  3. 都拿不到                                  → 计入 unresolved，分类统计后打印，不静默丢弃

幂等：落库走 Database.add_magnet()，同一 (title_id, magnet) 重复运行不新增行。

用法：
    python tools/import_verified_magnets.py --db storage/library_v2.db
    python tools/import_verified_magnets.py --db X.db --cache <path> --library-db <path>
    python tools/import_verified_magnets.py --db X.db --dry-run
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if ROOT not in sys.path:

    sys.path.insert(0, ROOT)

from core.database_v2 import Database

DEFAULT_CACHE = r"X:\corpus\verify_cache.json"

DEFAULT_LIBRARY_DB = r"X:\corpus\library.db"

SOURCE = "verify_cache"

MB = 1024.0


def format_size_text(size):
    """把来源里的 MB 数值归一到 D5 示例的文本形态（如 4.2GB）。"""

    if size is None or size == "":

        return None

    try:

        mb = float(size)

    except (TypeError, ValueError):

        return str(size)

    if mb <= 0:

        return None

    if mb >= MB:

        return "%.1fGB" % (mb / MB)

    return "%dMB" % int(round(mb))


def btih_magnet(hash_value):
    """btih hash → 标准 magnet 链接。"""

    if not hash_value:

        return None

    h = str(hash_value).strip()

    if h.lower().startswith("magnet:"):

        return h

    return "magnet:?xt=urn:btih:%s" % h


class LibraryIndex:
    """library.db（只读）→ 番号 / movie_id 到 magnets JSON 的映射。"""

    def __init__(self, path):

        self.conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)

        self.conn.row_factory = sqlite3.Row

        self.by_number = {}

        self.by_id = {}

        for row in self.conn.execute(
            "SELECT number, javdb_id, magnets FROM movies"
        ):

            if row["magnets"]:

                self.by_number[row["number"]] = row["magnets"]

                if row["javdb_id"]:

                    self.by_id[row["javdb_id"]] = row["magnets"]

    def lookup(self, entry, code):
        """按 matched → number → code → movie_id 依次找 magnets JSON。"""

        for key in (
            entry.get("matched"),
            entry.get("number"),
            code,
        ):

            if key and key in self.by_number:

                return self.by_number[key]

        movie_id = entry.get("movie_id")

        if movie_id and movie_id in self.by_id:

            return self.by_id[movie_id]

        return None

    def items(self, entry, code):
        """取该番号的磁力明细列表（含 hash / size / name）。"""

        raw = self.lookup(entry, code)

        if not raw:

            return []

        try:

            parsed = json.loads(raw)

        except (TypeError, ValueError):

            return []

        if not isinstance(parsed, list):

            return []

        return [x for x in parsed if isinstance(x, dict) and x.get("hash")]


def extract(code, entry, library):
    """从一条 verify_cache 记录里抽出可落库的磁力。

    返回 (magnets, reason)
      magnets = [{"magnet", "size_text", "name"}, ...]（可能为空）
      reason  = 抽不出时的分类标签，抽出来时为 None
    """

    status = entry.get("status")

    if status != "found":

        return [], "status=%s" % status

    field = entry.get("magnets")

    # 格式 (c)：字段缺失 / None
    if field is None:

        return [], "magnets_field_missing"

    # 格式 (a)：list
    if isinstance(field, list):

        out = []

        for item in field:

            if not isinstance(item, dict):

                continue

            uri = (item.get("uri") or "").strip()

            if not uri:

                continue

            out.append({
                "magnet": uri,
                "size_text": format_size_text(item.get("size")),
                "name": (item.get("title") or "").strip() or None,
            })

        if out:

            return out, None

        # list 存在但 uri 全空 → 回落 library.db
        if library is not None:

            items = library.items(entry, code)

            if items:

                return [
                    {
                        "magnet": btih_magnet(x.get("hash")),
                        "size_text": format_size_text(x.get("size")),
                        "name": (x.get("name") or "").strip() or None,
                    }
                    for x in items
                ], None

            return [], "uri_empty_and_no_library_row"

        return [], "uri_empty(no_library_db)"

    # 格式 (b)：int，只有数量
    if isinstance(field, int):

        if library is None:

            return [], "count_only(no_library_db)"

        items = library.items(entry, code)

        if not items:

            return [], "count_only_no_library_row"

        return [
            {
                "magnet": btih_magnet(x.get("hash")),
                "size_text": format_size_text(x.get("size")),
                "name": (x.get("name") or "").strip() or None,
            }
            for x in items
        ], None

    return [], "unknown_type=%s" % type(field).__name__


def load_final_list(path):
    """读 final_list.tsv（312 条可删文件清单：code / GB / magnets / date / path）。

    ⚠️ tsv 的 code 是上一轮已核实好的番号，本脚本不再用框架 parser 二次解析
    （框架 dictionary.json 目前只有 13 条规则、覆盖不了这批番号 —— 那是 P1-2/C3 的范围）。
    """

    import csv

    rows = []

    with open(path, encoding="utf-8", newline="") as fh:

        for row in csv.DictReader(fh, delimiter="\t"):

            if not row.get("code") or not row.get("path"):

                continue

            rows.append(row)

    return rows


def run(db_path, cache_path, library_path, dry_run=False, verified=1, final_list=None):
    """执行一次导入。返回统计 dict（不打印，便于测试断言）。"""

    with open(cache_path, encoding="utf-8") as fh:

        cache = json.load(fh)

    library = LibraryIndex(library_path) if library_path else None

    db = None if dry_run else Database(db_path)

    stats = Counter()

    reasons = Counter()

    per_number = {}

    for code, entry in cache.items():

        if not isinstance(entry, dict):

            stats["bad_entry"] += 1

            continue

        magnets, reason = extract(code, entry, library)

        if not magnets:

            stats["unresolved"] += 1

            reasons[reason or "unknown"] += 1

            continue

        number = entry.get("matched") or entry.get("number") or code

        created_here = 0

        for item in magnets:

            stats["parsed"] += 1

            if dry_run:

                continue

            result = db.add_magnet(
                number,
                item["magnet"],
                source=SOURCE,
                size_text=item["size_text"],
                verified=verified,
                commit=False,
            )

            if result["created"]:

                created_here += 1

                stats["inserted"] += 1

            else:

                stats["duplicate"] += 1

        per_number[number] = created_here

        stats["numbers_resolved"] += 1

    if not dry_run:

        # 批量导入统一提交（逐行 commit 在 Windows 上要几十秒）
        db.conn.commit()

    stats["numbers_total"] = len(cache)

    # ── 可选：把本地可删文件也登记进 media_files ──
    # 没有这一步，deletable_titles() 只能返回番号 + 0 字节；
    # 有了这一步才能算出「312 个文件 / 942.6 GB」这类真实占用。
    if final_list:

        files = load_final_list(final_list)

        stats["list_files"] = len(files)

        registered = 0

        gb_total = 0.0

        for row in files:

            number = number_for_code(row["code"], cache)

            try:

                gb_total += float(row.get("GB") or 0)

            except ValueError:

                pass

            if dry_run:

                continue

            if db.conn.execute(
                "SELECT 1 FROM media_files WHERE filepath=?",
                (row["path"],)
            ).fetchone():

                continue

            size = None

            try:

                size = int(round(float(row.get("GB") or 0) * 1e9))

            except ValueError:

                size = None

            db.add_file(number, row["path"], size=size)

            registered += 1

        if not dry_run:

            db.conn.commit()

        stats["list_registered"] = registered

        stats["list_gb"] = round(gb_total, 1)

    return {
        "stats": dict(stats),
        "reasons": dict(reasons),
        "per_number": per_number,
        "db_path": db_path,
        "dry_run": dry_run,
    }


def number_for_code(code, cache):
    """verify_cache 的 key(code) → 实际落库用的番号。

    与 run() 里给磁力取 number 的规则完全一致，
    保证同一个番号的磁力与本地文件挂到同一个 title 上。
    """

    entry = cache.get(code)

    if isinstance(entry, dict):

        return entry.get("matched") or entry.get("number") or code

    return code


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="把已验证磁力清单灌进 v2 magnets 表（幂等）"
    )

    parser.add_argument("--db", required=True, help="v2 库路径，如 storage/library_v2.db")

    parser.add_argument("--cache", default=DEFAULT_CACHE, help="verify_cache.json 路径")

    parser.add_argument(
        "--library-db",
        default=None,
        help="可选：上一轮 javdb_enrich 产出的 library.db（含 btih hash）。"
             "不传则只吃 verify_cache 自带的 uri。",
    )

    parser.add_argument("--verified", type=int, default=1, help="写入的 verified 值，默认 1")

    parser.add_argument(
        "--final-list",
        default=None,
        help="可选：final_list.tsv（312 条可删文件清单）。给了就把这些本地文件登记进 "
             "media_files，deletable_titles() 才能算出真实总占用。",
    )

    parser.add_argument("--dry-run", action="store_true", help="只统计不落库")

    args = parser.parse_args(argv)

    library_path = args.library_db

    if library_path and not os.path.exists(library_path):

        print("!! --library-db 不存在：%s" % library_path)

        return 2

    result = run(
        args.db,
        args.cache,
        library_path,
        dry_run=args.dry_run,
        verified=args.verified,
        final_list=args.final_list,
    )

    stats = result["stats"]

    print("=== 导入结果%s ===" % ("（dry-run，未落库）" if args.dry_run else ""))

    print("  数据源      : %s" % args.cache)

    print("  library.db  : %s" % (library_path or "未启用"))

    print("  目标库      : %s" % args.db)

    print("  番号总数    : %d" % stats.get("numbers_total", 0))

    print("  解析成功番号: %d" % stats.get("numbers_resolved", 0))

    print("  解析出磁力条: %d" % stats.get("parsed", 0))

    print("  新插入行    : %d" % stats.get("inserted", 0))

    print("  已存在跳过  : %d" % stats.get("duplicate", 0))

    print("  无法解析番号: %d" % stats.get("unresolved", 0))

    if result["reasons"]:

        print("  ── 无法解析的分类 ──")

        for reason, count in sorted(
            result["reasons"].items(),
            key=lambda x: -x[1],
        ):

            print("     %-32s %d" % (reason, count))

    if not args.dry_run:

        conn = sqlite3.connect(args.db)

        total = conn.execute("SELECT COUNT(*) FROM magnets").fetchone()[0]

        conn.close()

        print("  SELECT COUNT(*) FROM magnets = %d" % total)

    return 0


if __name__ == "__main__":

    sys.exit(main())
