# -*- coding: utf-8 -*-
"""Web UI 辅助函数与查询层测试。

只测纯函数与路由可导入性 —— 不启真实 HTTP 服务（那是端到端验证的事）。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import app as web_app                               # noqa: E402


# ─────────────────────────── cover_filename

def test_cover_filename_handles_windows_backslash():
    """Windows 反斜杠路径必须能取到文件名。

    模板里 `split('/')` 对反斜杠无效会返回整条路径 → 封面 404。
    这就是把它挪到服务端的原因。
    """

    got = web_app.cover_filename(
        r"storage\images\covers\ABP-171-image-002.jpg"
    )

    assert got == "ABP-171-image-002.jpg"


def test_cover_filename_handles_forward_slash():
    got = web_app.cover_filename("storage/images/covers/x.jpg")

    assert got == "x.jpg"


def test_cover_filename_none_and_empty():
    assert web_app.cover_filename(None) is None
    assert web_app.cover_filename("") is None


# ─────────────────────────── human_size

@pytest.mark.parametrize("n,expected", [
    (0, "0 B"),
    (512, "512 B"),
    (1024, "1.0 KB"),
    (1024 * 1024, "1.0 MB"),
    (1024 ** 3 * 5, "5.0 GB"),
    (int(827.3 * 1024 ** 3), "827.3 GB"),
])
def test_human_size(n, expected):
    assert web_app.human_size(n) == expected


def test_human_size_none_is_zero():
    assert web_app.human_size(None) == "0 B"


# ─────────────────────────── names_of

def test_names_of_extracts_names_from_javdb_json():
    """库里存的是 JavDB 原始 JSON，不能把整坨 JSON 显示给用户。"""

    raw = json.dumps(
        [
            {"id": "1", "name": "桃谷エリカ", "avatar_url": "http://x/a.jpg"},
            {"id": "2", "name": "加賀美シュナ", "avatar_url": None},
        ]
    )

    assert web_app.names_of(raw) == "桃谷エリカ / 加賀美シュナ"


def test_names_of_plain_text_passthrough():
    assert web_app.names_of("プレステージ") == "プレステージ"


def test_names_of_broken_json_returns_raw():
    assert web_app.names_of("[not json") == "[not json"


def test_names_of_empty():
    assert web_app.names_of(None) == ""
    assert web_app.names_of("") == ""


def test_names_of_handles_bare_list():
    assert web_app.names_of(json.dumps(["a", "b"])) == "a / b"


# ─────────────────────────── 查询层

def test_query_on_missing_db_returns_empty(tmp_path, monkeypatch):
    """库不存在时返回空结果，不能 500（首次使用还没扫过）。"""

    monkeypatch.setattr(web_app, "DB_PATH", str(tmp_path / "nope.db"))

    assert web_app.query("SELECT 1") == []


def test_query_reads_real_rows(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "t.db"

    c = sqlite3.connect(db)

    c.execute("CREATE TABLE t (x INTEGER)")

    c.execute("INSERT INTO t VALUES (42)")

    c.commit()
    c.close()

    monkeypatch.setattr(web_app, "DB_PATH", str(db))

    rows = web_app.query("SELECT x FROM t")

    assert rows[0]["x"] == 42


def test_library_stats_on_empty_db(tmp_path, monkeypatch):
    """库不存在时统计全 0，不崩。"""

    monkeypatch.setattr(web_app, "DB_PATH", str(tmp_path / "nope.db"))

    stats = web_app.library_stats()

    assert stats["titles"] == 0
    assert stats["files"] == 0
    assert stats["size_h"] == "0 B"
    assert stats["magnets"] == 0


def test_accepts_scan_paths_routes_exist():
    """扫描路由必须注册（否则前端拿 404）。"""

    paths = {r.path for r in web_app.app.routes}

    assert "/scan" in paths
    assert "/api/scan/start" in paths
    assert "/api/scan/status" in paths
    assert "/detail/{number}" in paths
    assert "/search" in paths
