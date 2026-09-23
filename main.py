"""入口：扫描 config.yaml 的 scan_paths 并落 v2 库。

v1 版本（core.scanner / core.matcher / core.cache / core.database）已废弃，
v2 是唯一主线。旧实现保留在 git 历史里。

用法::

    python main.py                          # 扫 config.yaml 的 scan_paths
    python main.py "X:\\迅雷下载"            # 只扫指定目录（覆盖 scan_paths）
    python main.py "X:\\片" "X:\\下载"       # 扫多个目录
    python main.py --list                   # 只列出将要扫描的目录，不执行

**命令行传的目录同样受格式白名单与目录排除约束** —— 它们是安全防线，
不因为「临时扫一下」而打开后门。命令行只改「扫哪里」，不改「什么算影片」。
"""

import argparse
import json
import os

import yaml

from core.database_v2 import Database
from services.scan_service import ScanService


ROOT = os.path.dirname(
    os.path.abspath(__file__)
)


def resolve_path(path):

    if not path or os.path.isabs(path):

        return path

    return os.path.join(
        ROOT,
        path
    )


def load_config():

    with open(
        os.path.join(ROOT, "config.yaml"),
        encoding="utf-8"
    ) as fh:

        return yaml.safe_load(fh) or {}


def load_rules(path):

    # v2 词典格式是 {"rules": [...]}，不是 v1 的 {"codes": [...]}
    with open(
        path,
        encoding="utf-8"
    ) as fh:

        return json.load(fh)["rules"]


def parse_args(argv=None):
    """解析命令行参数。

    paths 为空 -> 用 config.yaml 的 scan_paths（保持原有行为）。
    """

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="扫描媒体目录，识别番号并落库。",
        epilog="不传路径时使用 config.yaml 的 scan_paths。",
    )

    parser.add_argument(
        "paths",
        nargs="*",
        metavar="目录",
        help="要扫描的目录（可多个）；不传则用 config.yaml 的 scan_paths",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="只列出将要扫描的目录，不执行扫描",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="真·干跑：解析并显示结果，但不写库、不推进增量索引",
    )

    parser.add_argument(
        "--min-conf",
        type=int,
        default=0,
        metavar="N",
        help="落库的最低置信度（0-100，默认 0 = 全收）。"
             "识别规则是启发式的，设 90 可只收可信档；"
             "设 70 可专门捞出待人工确认的猜测",
    )

    parser.add_argument(
        "--max-conf",
        type=int,
        default=100,
        metavar="N",
        help="落库的最高置信度（0-100，默认 100 = 不设上限）",
    )

    return parser.parse_args(argv)


def main(argv=None):

    args = parse_args(argv)

    config = load_config()

    database = resolve_path(
        config["database"]
    )

    index_db = resolve_path(
        config.get("index_db")
        or "storage/file_index_v2.db"
    )

    rules = load_rules(
        resolve_path(config["dictionary"])
    )

    # 命令行给了路径就用命令行，否则回退 config
    if args.paths:

        targets = list(args.paths)

        source = "命令行"

    else:

        targets = list(
            config.get("scan_paths") or []
        )

        source = "config.yaml"

    if not targets:

        print("没有要扫描的目录：命令行未给路径，config.yaml 的 scan_paths 也是空的。")

        return 1

    print(f"扫描目录（来源：{source}）：")

    for path in targets:

        mark = "  " if os.path.isdir(path) else "✗ "

        note = "" if os.path.isdir(path) else "   <- 目录不存在"

        print(f"{mark}{path}{note}")

    if args.list_only:

        print("\n（--list：仅列出，未执行扫描）")

        return 0

    db = Database(database)

    service = ScanService(
        index_db,
        rules,
        db=db
    )

    total_files = 0
    total_saved = 0
    total_skipped = 0

    if args.min_conf or args.max_conf != 100:

        print(f"落库置信度门槛：{args.min_conf} - {args.max_conf}")

    for path in targets:

        if not os.path.isdir(path):

            print(f"\n跳过（不是目录）：{path}")

            continue

        print(f"\nScanning: {path}")

        result = service.scan(
            path,
            persist=not args.dry_run,
            min_conf=args.min_conf,
            max_conf=args.max_conf,
        )

        rows = result["data"]

        persisted = sum(
            1 for row in rows if row["persisted"]
        )

        skipped = sum(
            1 for row in rows if row.get("skipped")
        )

        total_files += len(rows)
        total_saved += persisted
        total_skipped += skipped

        if args.dry_run:

            print(f"  文件 {len(rows)}  [dry-run：未落库]")

            for row in rows[:20]:

                nums = ", ".join(
                    f"{n['number']}({n['confidence']})"
                    for n in row["numbers"][:3]
                ) or "—"

                print(f"    {os.path.basename(row['file'])}  ->  {nums}")

            if len(rows) > 20:

                print(f"    ...另有 {len(rows) - 20} 个")

        else:

            line = f"  文件 {len(rows)} / 落库 {persisted}"

            if skipped:

                line += f" / 门槛挡下 {skipped}"

            print(line)

            for row in rows:

                if row.get("skipped"):

                    sk = row["skipped"]

                    print(f"    [跳过] {os.path.basename(row['file'])}"
                          f"  {sk['number']} ({sk['confidence']})")

    if args.dry_run:

        print(f"\n完成（dry-run）：共 {total_files} 个文件，未写库")

    else:

        tail = f"，落库 {total_saved} 条"

        if total_skipped:

            tail += f"，门槛挡下 {total_skipped} 条"

        print(f"\n完成：共 {total_files} 个文件{tail}")

    return 0


if __name__ == "__main__":

    raise SystemExit(main())
