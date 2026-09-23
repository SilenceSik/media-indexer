"""常驻扫描服务：按 config.yaml 的 scan_paths 定期扫库（v2 主线）。

v1 版本（core.scanner / core.parser.NumberParser / core.database / core.logger）
已废弃，v2 是唯一主线。旧实现保留在 git 历史里。

注：core/logger.py 本身不是 v1/v2 成对模块，仍沿用；但它内部用相对路径
storage/logs，从非仓库根启动时会把日志写到别处（已知遗留问题，不在本卡边界内）。
"""

import json
import os
import time

import yaml

from core.database_v2 import Database

from core.logger import info

from services.scan_service import ScanService


ROOT = os.path.dirname(
    os.path.abspath(__file__)
)


# 每6小时一次
INTERVAL_SECONDS = 21600


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


def build_service(config):

    db = Database(
        resolve_path(
            config["database"]
        )
    )

    return ScanService(
        resolve_path(
            config.get("index_db")
            or "storage/file_index_v2.db"
        ),
        load_rules(
            resolve_path(
                config["dictionary"]
            )
        ),
        db=db
    )


def scan_task(
    service,
    config
):

    info("开始自动扫描")

    for path in config.get("scan_paths") or []:

        result = service.scan(
            path,
            persist=True
        )

        rows = result["data"]

        persisted = sum(
            1 for row in rows if row["persisted"]
        )

        info(
            f"{path}: 文件 {len(rows)} / 落库 {persisted}"
        )

    info("扫描结束")


if __name__ == "__main__":

    config = load_config()

    service = build_service(config)

    info("Media Manager Service Started")

    while True:

        scan_task(
            service,
            config
        )

        time.sleep(INTERVAL_SECONDS)
