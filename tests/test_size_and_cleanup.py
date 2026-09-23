# -*- coding: utf-8 -*-
"""文件大小门槛 + 低分卡片清理 + 手动加入 测试。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from services.file_service import FileService                  # noqa: E402
from services.scan_service import ScanService                  # noqa: E402
from web import app as web_app                                 # noqa: E402

RULES = [{"pattern": r"ABP[-_ ]?\d{3,6}", "priority": 100}]


def make_svc(tmp_path):
    db = Database(str(tmp_path / "lib.db"))

    svc = ScanService(
        str(tmp_path / "idx.db"),
        RULES,
        db=db,
        excluded_segments=[],
    )

    return svc, db


def write(path, mb):
    path.write_bytes(b"x" * int(mb * 1024 * 1024))


# ─────────────────────── 文件大小门槛

def test_size_filter_skips_small_files(tmp_path):
    """小文件（预告片）被挡下，不落库也不进索引。"""

    svc, db = make_svc(tmp_path)

    write(tmp_path / "ABP-171.mp4", 0.05)      # 50 KB
    write(tmp_path / "ABP-172.mp4", 2)         # 2 MB

    svc.scan(str(tmp_path), persist=True, min_size=1024 * 1024)

    numbers = {
        r[0] for r in db.conn.execute(
            "SELECT t.number FROM media_files m JOIN titles t ON t.id=m.title_id"
        )
    }

    assert numbers == {"ABP-172"}, f"小文件应被挡下，实际 {numbers}"


def test_size_filter_max(tmp_path):
    """上限：超大文件被挡下。"""

    svc, db = make_svc(tmp_path)

    write(tmp_path / "ABP-171.mp4", 2)
    write(tmp_path / "ABP-172.mp4", 10)

    svc.scan(str(tmp_path), persist=True, max_size=5 * 1024 * 1024)

    numbers = {
        r[0] for r in db.conn.execute(
            "SELECT t.number FROM media_files m JOIN titles t ON t.id=m.title_id"
        )
    }

    assert numbers == {"ABP-171"}


def test_size_zero_means_unlimited(tmp_path):
    """0 = 该端不限制（滑块划到头）。"""

    svc, db = make_svc(tmp_path)

    write(tmp_path / "ABP-171.mp4", 0.05)

    svc.scan(str(tmp_path), persist=True, min_size=0, max_size=0)

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1


def test_size_filtered_file_not_indexed(tmp_path):
    """被大小挡下的文件不推进索引 —— 放宽门槛后还能再扫进来。"""

    svc, db = make_svc(tmp_path)

    write(tmp_path / "ABP-171.mp4", 0.05)

    svc.scan(str(tmp_path), persist=True, min_size=10 * 1024 * 1024)

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 0

    # 放宽门槛重扫，应能进来
    svc.scan(str(tmp_path), persist=True, min_size=0)

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1


def test_size_and_conf_filters_compose(tmp_path):
    """大小与置信度两道门槛同时生效。"""

    svc, db = make_svc(tmp_path)

    write(tmp_path / "ABP-171.mp4", 2)
    write(tmp_path / "kcf9.com-3 (1)_(new)_amq13.mp4", 2)

    svc.scan(
        str(tmp_path), persist=True,
        min_conf=90, min_size=1024 * 1024,
    )

    numbers = {
        r[0] for r in db.conn.execute(
            "SELECT t.number FROM media_files m JOIN titles t ON t.id=m.title_id"
        )
    }

    assert numbers == {"ABP-171"}


# ─────────────────────── 低分卡片清理

def test_low_score_lists_entries(tmp_path):
    """低分条目能被列出，带分数。"""

    db = Database(str(tmp_path / "l.db"))

    db.add_file("AMQ-13", str(tmp_path / "kcf9.com-3 (1)_(new)_amq13.mp4"))
    db.add_file("ABP-171", str(tmp_path / "ABP-171.mp4"))

    svc = FileService(db, str(tmp_path / "t"), rules=RULES)

    entries = svc.low_score_entries(79)

    nums = {e["number"] for e in entries}

    assert nums == {"AMQ-13"}, f"只该列出低分的，实际 {nums}"

    assert entries[0]["confidence"] == 70


def test_low_score_threshold_respected(tmp_path):
    """阈值越低，列出的越少。"""

    db = Database(str(tmp_path / "l2.db"))

    db.add_file("AMQ-13", str(tmp_path / "kcf9.com-3 (1)_(new)_amq13.mp4"))

    svc = FileService(db, str(tmp_path / "t"), rules=RULES)

    assert len(svc.low_score_entries(79)) == 1
    assert len(svc.low_score_entries(50)) == 0


def test_remove_entries_deletes_db_rows_only(tmp_path):
    """摘除只删库记录，磁盘文件必须原封不动。"""

    db = Database(str(tmp_path / "r.db"))

    video = tmp_path / "kcf9.com-3 (1)_(new)_amq13.mp4"
    video.write_bytes(b"content")

    db.add_file("AMQ-13", str(video))

    svc = FileService(db, str(tmp_path / "t"), rules=RULES)

    removed, orphans = svc.remove_entries([str(video)])

    assert removed == 1
    assert orphans == 1, "孤立标题也该清掉"

    assert video.exists(), "磁盘文件绝不能删"

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 0


def test_remove_entries_keeps_shared_title(tmp_path):
    """同一番号还有别的文件时，标题不能被清掉。"""

    db = Database(str(tmp_path / "s.db"))

    a = tmp_path / "ABP-171-a.mp4"
    b = tmp_path / "ABP-171-b.mp4"

    db.add_file("ABP-171", str(a))
    db.add_file("ABP-171", str(b))

    svc = FileService(db, str(tmp_path / "t"), rules=RULES)

    removed, orphans = svc.remove_entries([str(a)])

    assert removed == 1
    assert orphans == 0
    assert db.conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 1


# ─────────────────────── 路由注册

def test_new_routes_registered():
    paths = {r.path for r in web_app.app.routes}

    assert "/api/low-score" in paths
    assert "/api/low-score/remove" in paths
    assert "/api/scan/import" in paths


def test_import_rejects_missing_dir(tmp_path):
    """手动加入：目录不存在要明确报错。"""

    import asyncio

    class FakeReq:

        async def json(self):

            return {"path": str(tmp_path / "nope")}

    res = asyncio.run(web_app.api_scan_import(FakeReq()))

    assert res.status_code == 400
