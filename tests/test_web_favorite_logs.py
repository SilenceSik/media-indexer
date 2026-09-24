# -*- coding: utf-8 -*-
"""web 层新增功能的测试：❤️ 收藏 / 筛选日志页 / 评论 / 宣传视频。

全部在临时库上跑，不碰生产库。

⚠️ 这几个功能有个共同的坑：`web/app.py` 大量用**裸 `query()` 直连**，而
`migrate()` 只在 `Database()` 构造时跑。所以任何新增列（`favorite`、
`metadata.preview_video`）在没迁过的库上会让页面 500
（`no such column`）。`ensure_schema()` 就是为此存在的 —— 下面
`test_query_self_heals_missing_columns` 专门钉住它。
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from core import filter_log                                   # noqa: E402
from core.database_v2 import Database                         # noqa: E402


@pytest.fixture
def web(tmp_path, monkeypatch):
    """临时库 + 挂好 DB_PATH 的 TestClient。"""

    import web.app as A

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    # ⚠️ 必须连 media_files 一起插：`LIST_SQL` 是
    # `FROM media_files JOIN titles`（内连接），只有 titles 的番号
    # **不会**出现在列表页 —— 首页是「本体是文件，番号是标签」。
    db.conn.execute("INSERT INTO titles (number) VALUES ('TEST-001')")
    db.conn.execute("INSERT INTO titles (number) VALUES ('OTHER-002')")
    db.conn.execute(
        "INSERT INTO media_files (title_id, filepath, filename, size) "
        "SELECT id, 'D:/x/TEST-001.mp4', 'TEST-001.mp4', 1000000 "
        "FROM titles WHERE number='TEST-001'"
    )
    db.conn.execute(
        "INSERT INTO media_files (title_id, filepath, filename, size) "
        "SELECT id, 'D:/x/OTHER-002.mp4', 'OTHER-002.mp4', 1000000 "
        "FROM titles WHERE number='OTHER-002'"
    )
    db.conn.commit()

    monkeypatch.setattr(A, "DB_PATH", db_path)

    # ⚠️ 宣传视频目录也必须指到 tmp，否则 `save_preview_video` 会：
    #   ① 往真实的 storage/previews/ 写文件；
    #   ② 命中残留缓存 -> 测试「假通过」（在干净克隆里就红）。
    #     这条是在公开克隆实跑时抓出来的。
    previews = str(tmp_path / "previews")

    os.makedirs(previews, exist_ok=True)

    monkeypatch.setattr(A, "PREVIEWS_DIR", previews)

    from fastapi.testclient import TestClient

    return TestClient(A.app), db, db_path


# ═══════════════════════ ❤️ 收藏

def test_favorite_toggles_and_persists(web):
    client, db, _ = web

    r = client.post("/api/favorite/TEST-001")

    assert r.status_code == 200
    assert r.json()["favorite"] is True

    got = db.conn.execute(
        "SELECT favorite FROM titles WHERE number='TEST-001'"
    ).fetchone()[0]

    assert got == 1, "接口说成功但库里没落盘"

    # 再点一次 -> 取消
    r2 = client.post("/api/favorite/TEST-001")

    assert r2.json()["favorite"] is False

    assert db.conn.execute(
        "SELECT favorite FROM titles WHERE number='TEST-001'"
    ).fetchone()[0] == 0


def test_favorite_unknown_number_404(web):
    client, _, _ = web

    r = client.post("/api/favorite/NOPE-9999")

    assert r.status_code == 404


def test_favorite_does_not_touch_gating_columns(web):
    """收藏是**纯人工标记**：不许改档位/磁力/核对位，也不许解锁删除。"""

    client, db, _ = web

    db.conn.execute(
        "UPDATE titles SET tier='极低', correct_magnets=0, favorite=0 "
        "WHERE number='TEST-001'"
    )
    db.conn.commit()

    client.post("/api/favorite/TEST-001")

    row = db.conn.execute(
        "SELECT tier, correct_magnets, favorite FROM titles "
        "WHERE number='TEST-001'"
    ).fetchone()

    assert row["favorite"] == 1
    assert row["tier"] == "极低", "收藏不该抬档位"
    assert row["correct_magnets"] == 0, "收藏不该造磁力"


def test_favorite_filter_only_shows_favorites(web):
    client, db, _ = web

    db.conn.execute("UPDATE titles SET favorite=1 WHERE number='TEST-001'")
    db.conn.commit()

    page = client.get("/?favorite=1")

    assert page.status_code == 200
    assert 'data-fav-only="1"' in page.text
    assert "TEST-001" in page.text
    assert "OTHER-002" not in page.text, "收藏筛选没生效"


def test_favorites_sort_first(web):
    """默认列表里收藏排前面。"""

    client, db, _ = web

    db.conn.execute("UPDATE titles SET favorite=1 WHERE number='OTHER-002'")
    db.conn.commit()

    page = client.get("/")

    assert page.text.index("OTHER-002") < page.text.index("TEST-001"), \
        "收藏的没排在前面"


# ═══════════════════════ 筛选日志页

@pytest.fixture
def with_logs(web):
    _, db, _ = web

    for i in range(3):
        filter_log.record(db, path="P{}.mp4".format(i), filename="P{}.mp4".format(i),
                          reason="size_below_min", size=123456)

    filter_log.record(db, path="Q.mp4", filename="Q.mp4",
                      reason="not_recognized", number=None)

    filter_log.flush(db)

    return web


def test_logs_page_renders(with_logs):
    client, _, _ = with_logs

    r = client.get("/logs")

    assert r.status_code == 200
    assert "筛选日志" in r.text
    assert "size_below_min" in r.text or "小于设定的文件大小下限" in r.text


def test_logs_page_filter_narrows(with_logs):
    client, _, _ = with_logs

    page = client.get("/logs?reason=not_recognized")

    assert page.status_code == 200

    api = client.get("/api/filter-log?reason=not_recognized").json()

    assert api["ok"] is True

    assert all(e["reason"] == "not_recognized" for e in api["entries"])

    assert len(api["entries"]) == 1


def test_logs_summary_matches_db(with_logs):
    client, db, _ = with_logs

    api = client.get("/api/filter-log").json()

    counts = {s["reason"]: s["count"] for s in api["summary"]}

    assert counts.get("size_below_min") == 3
    assert counts.get("not_recognized") == 1


def test_logs_export_csv_has_bom_and_filters(with_logs):
    client, _, _ = with_logs

    r = client.get("/api/filter-log/export?reason=size_below_min")

    assert r.status_code == 200
    assert r.content[:3] == b"\xef\xbb\xbf", "缺 BOM，Excel 会乱码"

    text = r.content.decode("utf-8-sig")

    lines = [l for l in text.strip().splitlines() if l]

    assert lines[0].startswith("原因码"), "表头不对"

    assert len(lines) == 4, "3 条数据 + 1 行表头"

    assert all("size_below_min" in l for l in lines[1:])


def test_logs_page_survives_empty_db(web):
    """库里没日志也不该 500（首次使用）。"""

    client, _, _ = web

    r = client.get("/logs")

    assert r.status_code == 200


# ═══════════════════════ 评论 / 宣传视频（打桩，不联网）

def test_comments_endpoint_persists(web, monkeypatch):
    client, db, _ = web

    import adapters.javdb_adapter as M

    class Fake:
        def reviews(self, number, limit=20, page=1):
            return [
                {"id": 1, "content": "第一条", "score": 5,
                 "likes_count": 10, "username": "u1",
                 "created_at": "2026-01-01T00:00:00.000Z"},
                {"id": 2, "content": "第二条", "score": 4,
                 "likes_count": 3, "username": "u2",
                 "created_at": "2026-01-02T00:00:00.000Z"},
            ]

    monkeypatch.setattr(M, "JavDBCLIClient", Fake)

    # ⚠️ **必须同时补丁 services.enrich_service 里的那个名字**。
    #
    # 那里是模块加载时 `from adapters.javdb_adapter import JavDBCLIClient`
    # 绑定的 —— 只改 `adapters.javdb_adapter` 的属性，对**已经导入过**的
    # `enrich_service` 无效。于是出现「单跑绿、全量红」的顺序依赖：
    # 单跑时 enrich_service 是首次导入（补丁赶在前面生效），
    # 全量里它早被别的模块导入过（补丁落空）-> 服务用真客户端去下载 -> 失败。
    #
    # 这类顺序依赖不会自己消失，必须显式钉住。
    import services.enrich_service as _es

    monkeypatch.setattr(_es, "JavDBCLIClient", Fake)

    r = client.post("/api/comments/TEST-001")

    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["added"] == 2

    n = db.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]

    assert n == 2, "接口说成功但库里没评论"

    # 幂等：同一条评论重复抓不翻倍（靠 javdb_id 唯一索引）
    client.post("/api/comments/TEST-001")

    assert db.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 2


def test_comments_endpoint_reports_failure_honestly(web, monkeypatch):
    """抓不到要**说抓不到**，不能伪装成「没有评论」。"""

    client, _, _ = web

    import adapters.javdb_adapter as M

    class Fake:
        def reviews(self, number, limit=20, page=1):
            return []

    monkeypatch.setattr(M, "JavDBCLIClient", Fake)

    r = client.post("/api/comments/TEST-001")

    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"]


def test_comments_unknown_number_404(web):
    client, _, _ = web

    assert client.post("/api/comments/NOPE-9999").status_code == 404


def test_preview_video_endpoint_persists(web, monkeypatch):
    """接口要把宣传视频**存成可播的 mp4**，并把链接记进库。

    ⚠️ 这条曾经「假通过」：Fake 没实现 `download_assets`，而当时
    `storage/previews/TEST-001/preview.mp4` 有**残留缓存**，下载被短路了。
    在干净克隆里跑就红（`Fake has no attribute download_assets`）。
    现在 fixture 把 PREVIEWS_DIR 指到 tmp，缓存不再跨次残留。
    """

    client, db, _ = web

    db.conn.execute(
        "INSERT INTO metadata (title_id, preview_video) "
        "SELECT id, NULL FROM titles WHERE number='TEST-001'"
    )
    db.conn.commit()

    import adapters.javdb_adapter as M

    class Fake:
        def preview_video_url(self, number):
            return "https://x/preview.m3u8?sign=abc"

        def download_assets(self, lines, directory, timeout=300, filename=None):
            # 模拟官方管道：把流存成 mp4
            path = os.path.join(directory, filename or "out.mp4")

            with open(path, "wb") as f:
                f.write(b"fake-mp4")

            return [path]

    monkeypatch.setattr(M, "JavDBCLIClient", Fake)

    # ⚠️ **必须同时补丁 services.enrich_service 里的那个名字**。
    #
    # 那里是模块加载时 `from adapters.javdb_adapter import JavDBCLIClient`
    # 绑定的 —— 只改 `adapters.javdb_adapter` 的属性，对**已经导入过**的
    # `enrich_service` 无效。于是出现「单跑绿、全量红」的顺序依赖：
    # 单跑时 enrich_service 是首次导入（补丁赶在前面生效），
    # 全量里它早被别的模块导入过（补丁落空）-> 服务用真客户端去下载 -> 失败。
    # 这类顺序依赖不会自己消失，必须显式钉住。
    import services.enrich_service as _es

    monkeypatch.setattr(_es, "JavDBCLIClient", Fake)

    r = client.post("/api/preview-video/TEST-001")

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, r.json()   # 失败时把原因带出来
    assert r.json()["play"].endswith("/preview.mp4"), "没给出可播地址"

    got = db.conn.execute(
        "SELECT preview_video FROM metadata m JOIN titles t ON t.id=m.title_id "
        "WHERE t.number='TEST-001'"
    ).fetchone()[0]

    assert got == "https://x/preview.m3u8?sign=abc", "链接没记进库"


def test_preview_video_absent_reports_clearly(web, monkeypatch):
    client, _, _ = web

    import adapters.javdb_adapter as M

    class Fake:
        def preview_video_url(self, number):
            return None

    monkeypatch.setattr(M, "JavDBCLIClient", Fake)

    # 同上：enrich_service 在模块加载时绑定了这个类，两处都要补丁
    import services.enrich_service as _es

    monkeypatch.setattr(_es, "JavDBCLIClient", Fake)

    r = client.post("/api/preview-video/TEST-001")

    assert r.status_code == 200
    assert r.json()["ok"] is False


# ═══════════════════════ 结构自愈

def test_query_self_heals_missing_columns(tmp_path, monkeypatch):
    """没迁过的老库上，`query()` 要自己把结构补上。

    没有 `ensure_schema()` 时，这是 `sqlite3.OperationalError:
    no such column: favorite` —— 首页直接 500。
    """

    import sqlite3

    import web.app as A

    old = str(tmp_path / "old.db")

    # 手搓一个「老库」：只有 titles，且没有 favorite 列
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE titles (id INTEGER PRIMARY KEY, "
                 "number TEXT UNIQUE, title TEXT)")
    conn.execute("INSERT INTO titles (number) VALUES ('OLD-001')")
    conn.commit()
    conn.close()

    cols = {r[1] for r in sqlite3.connect(old).execute(
        "PRAGMA table_info(titles)")}

    assert "favorite" not in cols, "构造的老库不该已经有 favorite"

    monkeypatch.setattr(A, "DB_PATH", old)
    A._MIGRATED.discard(old)

    rows = A.query("SELECT number, favorite FROM titles")

    assert len(rows) == 1
    assert rows[0]["favorite"] in (0, None), "补列后默认值应为 0"
