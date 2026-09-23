# -*- coding: utf-8 -*-
"""metadata 扩展（cover_local / screenshots）与 Web 截图辅助测试。"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from web import app as web_app                                 # noqa: E402


# ─────────────────────────── schema 迁移

def test_new_db_has_screenshots_column(tmp_path):
    """新库建表即含 screenshots 列。"""

    db = Database(str(tmp_path / "new.db"))

    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(metadata)")}

    assert "screenshots" in cols


def test_old_db_gets_screenshots_column(tmp_path):
    """老库（无 screenshots 列）打开时应自动补列，不能崩。"""

    path = tmp_path / "old.db"

    con = sqlite3.connect(path)

    con.execute("""
        CREATE TABLE metadata (
            id INTEGER PRIMARY KEY,
            title_id INTEGER UNIQUE,
            title TEXT,
            cover TEXT,
            release_date TEXT,
            maker TEXT,
            actresses TEXT,
            tags TEXT,
            updated_time REAL
        )
    """)

    con.commit()
    con.close()

    db = Database(str(path))       # 触发 migrate

    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(metadata)")}

    assert "screenshots" in cols


def test_migrate_is_idempotent(tmp_path):
    """重复打开不得报 duplicate column name。"""

    path = str(tmp_path / "twice.db")

    Database(path)

    Database(path)                 # 第二次不应抛异常

    cols = {
        r[1]
        for r in sqlite3.connect(path).execute("PRAGMA table_info(metadata)")
    }

    assert "screenshots" in cols


# ─────────────────────────── save_metadata 落库

def test_save_metadata_persists_cover_local_and_screenshots(tmp_path):
    """cover_local 与 screenshots 都必须真的写进库。

    历史缺陷：save_metadata 的 INSERT 列里**没有 cover_local**，
    所以框架永远存不下本地封面路径（截图同理）。
    """

    db = Database(str(tmp_path / "m.db"))

    db.save_metadata({
        "number": "ABP-171",
        "title": "t",
        "cover": "http://x/c.jpg",
        "cover_local": "covers/ABP-171.jpg",
        "release_date": "2014-07-19",
        "maker": "Prestige",
        "actresses": [{"name": "桃谷エリカ"}],
        "tags": [{"name": "多P"}],
        "screenshots": ["s/ABP-171-1.jpg", "s/ABP-171-2.jpg"],
    })

    row = db.conn.execute(
        "SELECT cover_local, screenshots, actresses FROM metadata"
    ).fetchone()

    assert row[0] == "covers/ABP-171.jpg"
    assert json.loads(row[1]) == ["s/ABP-171-1.jpg", "s/ABP-171-2.jpg"]
    assert json.loads(row[2]) == [{"name": "桃谷エリカ"}]


def test_save_metadata_without_screenshots(tmp_path):
    """没给 screenshots 时不崩，存空数组。"""

    db = Database(str(tmp_path / "n.db"))

    db.save_metadata({"number": "SSIS-001", "title": "t"})

    row = db.conn.execute("SELECT screenshots FROM metadata").fetchone()

    assert json.loads(row[0]) == []


# ─────────────────────────── screenshot_files

def test_screenshot_files_from_json_array():
    raw = json.dumps([r"covers\ABP-171-image-001.jpg", "x/ABP-171-image-002.jpg"])

    assert web_app.screenshot_files(raw) == [
        "ABP-171-image-001.jpg",
        "ABP-171-image-002.jpg",
    ]


def test_screenshot_files_single_string():
    assert web_app.screenshot_files(r"a\b\c.jpg") == ["c.jpg"]


def test_screenshot_files_empty_and_broken():
    assert web_app.screenshot_files(None) == []
    assert web_app.screenshot_files("") == []
    assert web_app.screenshot_files("[broken") == []


def test_screenshot_files_dedupes():
    raw = json.dumps(["a/x.jpg", "b/x.jpg"])

    assert web_app.screenshot_files(raw) == ["x.jpg"]


# ─────────────────────────── screenshot_url

def test_screenshot_url_matches_template_prefix(monkeypatch, tmp_path):
    """截图目录在 image_root/screenshots 下时，URL 用 /images/screenshots/。"""

    root = tmp_path / "images"

    monkeypatch.setattr(web_app, "IMAGE_ROOT", str(root))
    monkeypatch.setattr(web_app, "SCREENSHOTS_DIR", str(root / "screenshots"))

    assert web_app.screenshot_url("x.jpg") == "/images/screenshots/x.jpg"


def test_screenshot_url_falls_back_to_shots(monkeypatch, tmp_path):
    """截图在别处时用 /shots/ 前缀（因为 /images 挂的是封面根）。"""

    monkeypatch.setattr(web_app, "IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setattr(web_app, "SCREENSHOTS_DIR", str(tmp_path / "elsewhere"))

    assert web_app.screenshot_url("x.jpg") == "/shots/x.jpg"
