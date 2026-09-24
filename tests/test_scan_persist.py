"""P0-1 主干链路 —— 「扫描 → 解析 → 落库」验收测试

对应的三条断言：
  ① 扫描后 titles > 0
  ② media_files 行数 = 识别出番号的文件数
  ③ persist=False 时两表均不增
"""

import json
import os
import sqlite3

import pytest

from core.database_v2 import Database
from services.scan_service import ScanService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES_PATH = os.path.join(ROOT, "data", "dictionary.json")


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as fh:
        return json.load(fh)["rules"]


@pytest.fixture()
def library_db(tmp_path):
    """v2 主库路径（本用例内每测一份，互不干扰）"""
    return str(tmp_path / "library_v2.db")


def make_media(tmp_path, names):
    """在 tmp_path/media 下造假视频文件，内容随意"""
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)

    for name in names:
        (folder / name).write_bytes(b"\x00" * 16)

    return str(folder)


def service_for(tmp_path, library_db, index_name="index.db", with_db=True):
    db = Database(library_db) if with_db else None

    return ScanService(
        str(tmp_path / index_name),
        load_rules(),
        db=db
    )


def count_rows(path, table):
    conn = sqlite3.connect(path)

    try:
        return conn.execute(
            "SELECT COUNT(*) FROM %s" % table
        ).fetchone()[0]

    finally:
        conn.close()


def stored_numbers(path):
    conn = sqlite3.connect(path)

    try:
        return [
            row[0] for row in conn.execute(
                """
                SELECT titles.number
                FROM media_files
                JOIN titles
                ON titles.id = media_files.title_id
                ORDER BY titles.number
                """
            )
        ]

    finally:
        conn.close()


# ------------------------------------------------- ① ② 扫描即落库 + 返回结构

def test_scan_persists_titles_and_files(tmp_path, library_db):
    folder = make_media(
        tmp_path,
        ["ABP-123.mp4", "IPX-456.mp4", "no_number_clip.mp4"]
    )

    result = service_for(tmp_path, library_db).scan(folder)

    assert result["success"] is True
    assert result["error"] is None
    assert result["trace_id"]

    rows = result["data"]
    assert len(rows) == 3

    for row in rows:
        # skipped 是置信度门槛挡下的记录（默认全收时为 None）
        assert set(row) == {"file", "numbers", "persisted", "skipped"}
        assert isinstance(row["persisted"], bool)

    by_name = {
        os.path.basename(row["file"]): row
        for row in rows
    }

    # 有番号 → 落库；无番号 → 照样返回、不落库
    assert by_name["ABP-123.mp4"]["persisted"] is True
    assert by_name["IPX-456.mp4"]["persisted"] is True
    assert by_name["no_number_clip.mp4"]["persisted"] is False
    assert by_name["no_number_clip.mp4"]["numbers"] == []

    # ① titles > 0
    assert count_rows(library_db, "titles") > 0

    # ② media_files 行数 = 识别出番号的文件数
    matched = sum(
        1 for row in rows if row["numbers"]
    )

    assert matched == 2
    assert count_rows(library_db, "media_files") == matched

    assert stored_numbers(library_db) == ["ABP-123", "IPX-456"]


# ---------------------------------------------------------- ③ persist=False

def test_persist_false_writes_nothing(tmp_path, library_db):
    folder = make_media(
        tmp_path,
        ["ABP-123.mp4", "IPX-456.mp4"]
    )

    result = service_for(tmp_path, library_db).scan(
        folder,
        persist=False
    )

    rows = result["data"]

    assert len(rows) == 2
    assert [row["persisted"] for row in rows] == [False, False]

    # 解析结果照常返回
    assert all(row["numbers"] for row in rows)

    # ③ 两表均不增
    assert count_rows(library_db, "titles") == 0
    assert count_rows(library_db, "media_files") == 0


def test_scan_without_db_does_not_touch_library(tmp_path, library_db):
    """旧调用 ScanService(index_db, rules) 仍可用，只是不落库"""
    folder = make_media(
        tmp_path,
        ["ABP-123.mp4"]
    )

    result = service_for(
        tmp_path,
        library_db,
        with_db=False
    ).scan(folder)

    assert len(result["data"]) == 1
    assert result["data"][0]["persisted"] is False
    assert not os.path.exists(library_db)


# ------------------------------------------------------- 重复扫描不产生重复行

def test_rescan_is_idempotent(tmp_path, library_db):
    folder = make_media(
        tmp_path,
        ["ABP-123.mp4", "IPX-456.mp4"]
    )

    service_for(tmp_path, library_db, index_name="index1.db").scan(folder)

    assert count_rows(library_db, "media_files") == 2

    # 换一个 file_index 强制重扫同一批文件（文件未变，原 index 会跳过）
    again = service_for(
        tmp_path,
        library_db,
        index_name="index2.db"
    ).scan(folder)

    assert len(again["data"]) == 2
    assert all(row["persisted"] for row in again["data"])

    assert count_rows(library_db, "media_files") == 2
    assert count_rows(library_db, "titles") == 2


# --------------------------------------------------- 多候选番号取最高置信度

def test_highest_confidence_number_wins(tmp_path, library_db):
    # ABP=100 分，SABA=80 分 → 只入 ABP-123
    folder = make_media(
        tmp_path,
        ["ABP-123 SABA-456.mp4"]
    )

    result = service_for(tmp_path, library_db).scan(folder)

    row = result["data"][0]

    assert len(row["numbers"]) == 2
    assert row["persisted"] is True

    assert count_rows(library_db, "titles") == 1
    assert count_rows(library_db, "media_files") == 1
    assert stored_numbers(library_db) == ["ABP-123"]
