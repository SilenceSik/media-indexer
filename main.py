"""入口：扫描 config.yaml 的 scan_paths 并落 v2 库。

v1 版本（core.scanner / core.matcher / core.cache / core.database）已废弃，
v2 是唯一主线，v1 旧实现保留在 git 历史里。
"""

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


def main():

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

    db = Database(database)

    service = ScanService(
        index_db,
        rules,
        db=db
    )

    for path in config.get("scan_paths") or []:

        print(f"\nScanning: {path}")

        result = service.scan(
            path,
            persist=True
        )

        rows = result["data"]

        persisted = sum(
            1 for row in rows if row["persisted"]
        )

        print(
            f"  文件 {len(rows)} / 落库 {persisted}"
        )

    print("\n完成")


if __name__ == "__main__":

    main()
