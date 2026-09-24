# -*- coding: utf-8 -*-
"""详情页三项：置信分 / 评论折叠（默认 3 条）/ 宣传视频存成可播 mp4。

## 第三项为什么是「存 mp4」而不是「存链接」

javdb 给的是 **HLS 播放列表**（`.m3u8`，分片还 AES-128 加密）。
浏览器原生**不支持** HLS —— 主人 2026-09-24 点「宣传视频」看到的正是
「直接转下载了」。所以把它转成 mp4 落盘，用普通 `<video>` 播：

  * **比引 hls.js 轻**：不需要额外 JS，离线也能看
  * **不受签名过期影响**：签名的 m3u8 会过期，本地文件不会
  * **走官方 CLI**（`javdb assets download -o`，本来就干这个），
    不在项目里再搓一条 ffmpeg 管线
"""

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """临时库 + 挂好路径的 TestClient。"""

    import web.app as A

    from core.database_v2 import Database

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    db.conn.execute(
        "INSERT INTO titles (number, tier, correct_magnets, comments_count, "
        "number_matches, lookup_source) VALUES "
        "('TEST-001', '高', 12, 40, 1, 'std')"
    )
    db.conn.execute(
        "INSERT INTO titles (number) VALUES ('OTHER-002')"
    )
    for num in ("TEST-001", "OTHER-002"):
        db.conn.execute(
            "INSERT INTO media_files (title_id, filepath, filename, size) "
            "SELECT id, 'D:/x/' || number || '.mp4', number || '.mp4', 1000000 "
            "FROM titles WHERE number = ?",
            (num,),
        )

    db.conn.commit()

    monkeypatch.setattr(A, "DB_PATH", db_path)

    # 宣传视频目录也得指到 tmp，别往真 storage/previews 写
    previews = str(tmp_path / "previews")

    os.makedirs(previews, exist_ok=True)
    monkeypatch.setattr(A, "PREVIEWS_DIR", previews)

    from fastapi.testclient import TestClient

    return TestClient(A.app), db, previews


# ═══════════════════════ 置信分进详情页

def test_detail_shows_confidence_score(web):
    client, db, _ = web

    r = client.get("/detail/TEST-001")

    assert r.status_code == 200

    m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', r.text)

    assert m, "详情页没有置信分徽标"
    assert int(m.group(1)) > 0

    # 档位也在（原来就有）
    assert 'class="tier' in r.text


def test_detail_confidence_matches_list(web):
    """详情页与首页**必须同分** —— 口径不一致就是自相矛盾。"""

    client, _, _ = web

    home = client.get("/").text
    detail = client.get("/detail/TEST-001").text

    def score_of(html, container=None):
        m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', html)
        return int(m.group(1)) if m else None

    # 首页只有这一条有档位，取第一个分数即可
    assert score_of(home) == score_of(detail), \
        "首页 {} 详情 {}".format(score_of(home), score_of(detail))


def test_detail_zeroes_on_duration_mismatch(web):
    """时长完全不符的番号，详情页也要是 0 并挂「时长不符」。"""

    client, db, _ = web

    # 1 个 2 分钟文件 vs 130 分钟
    db.conn.execute(
        "UPDATE media_files SET duration_local = 120, duration_ref = 130 "
        "WHERE title_id = (SELECT id FROM titles WHERE number='TEST-001')"
    )
    db.conn.commit()

    r = client.get("/detail/TEST-001")

    m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', r.text)

    assert int(m.group(1)) == 0, "时长完全不符该归零"
    assert "时长不符" in r.text


# ═══════════════════════ 评论默认 3 条 + 折叠

def _seed_comments(db, n):
    import time

    title_id = db.conn.execute(
        "SELECT id FROM titles WHERE number='TEST-001'"
    ).fetchone()[0]

    for i in range(n):
        db.conn.execute(
            "INSERT INTO comments (title_id, javdb_id, content, likes_count, "
            "username, created_at, fetched_time) VALUES (?,?,?,?,?,?,?)",
            (title_id, str(i), "评论 {}".format(i), n - i, "u{}".format(i),
             "2026-01-0{}T00:00:00.000Z".format(i % 9 + 1), time.time()),
        )

    db.conn.commit()


def test_comments_show_three_and_fold_rest(web):
    client, db, _ = web

    _seed_comments(db, 8)

    r = client.get("/detail/TEST-001")

    assert r.status_code == 200
    assert 'id="cmt-list"' in r.text
    assert 'id="cmt-toggle"' in r.text, "超过 3 条应出现展开按钮"

    folded = len(re.findall(r'class="cmt extra"', r.text))

    assert folded == 5, "8 条里应折叠 5 条，实际 {}".format(folded)

    # 前 3 条不折叠（即 3 条可见）
    visible = len(re.findall(r'class="cmt"', r.text))

    assert visible == 3, "应默认显示 3 条，实际 {}".format(visible)

    # 页面上要写清折叠了几条
    assert "其余 5 条折叠" in r.text


def test_comments_under_fold_has_no_button(web):
    client, db, _ = web

    _seed_comments(db, 2)

    r = client.get("/detail/TEST-001")

    assert 'id="cmt-toggle"' not in r.text, "不足 3 条不该出现展开按钮"
    assert len(re.findall(r'class="cmt extra"', r.text)) == 0


def test_comment_fold_constant_is_three():
    import web.app as A

    assert A.COMMENT_FOLD == 3


# ═══════════════════════ 宣传视频：存成可播 mp4

def test_preview_url_empty_until_saved(web):
    """没落盘就不给地址 —— 否则渲染一个 404 的 <video>，像坏了。"""

    import web.app as A

    assert A.preview_url("TEST-001") == ""


def test_preview_url_after_file_exists(web):
    import web.app as A

    _, _, previews = web

    d = os.path.join(previews, "TEST-001")
    os.makedirs(d, exist_ok=True)

    with open(os.path.join(d, "preview.mp4"), "wb") as f:
        f.write(b"x" * 100)

    assert A.preview_url("TEST-001") == "/previews/TEST-001/preview.mp4"


def test_preview_url_ignores_empty_file(web):
    import web.app as A

    _, _, previews = web

    d = os.path.join(previews, "TEST-001")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "preview.mp4"), "wb").close()

    assert A.preview_url("TEST-001") == "", "空文件不算存好"


def test_save_preview_video_writes_mp4(web):
    """把 m3u8 转成 mp4 落盘，且用固定文件名（模板要引用）。"""

    _, db, previews = web

    import web.app as A

    class Fake:
        def download_assets(self, lines, directory, timeout=300, filename=None):
            assert filename == "preview.mp4", "要点名落盘文件"
            assert any("video\t" in l for l in lines)
            path = os.path.join(directory, filename)
            with open(path, "wb") as f:
                f.write(b"y" * 500)
            return [path]

    svc = A.build_enrich_service(db)
    svc.client = Fake()

    got = svc.save_preview_video("TEST-001", "https://x/p.m3u8?sign=abc")

    assert got["ok"] is True
    assert got["file"] == os.path.join(previews, "TEST-001", "preview.mp4")
    assert os.path.getsize(got["file"]) == 500


def test_save_preview_video_is_idempotent(web):
    """已存过就直接返回缓存 —— 签名链接会过期，本地文件不会。"""

    _, db, _ = web

    import web.app as A

    calls = {"n": 0}

    class Fake:
        def download_assets(self, lines, directory, timeout=300, filename=None):
            calls["n"] += 1
            path = os.path.join(directory, filename or "out.mp4")
            with open(path, "wb") as f:
                f.write(b"y" * 100)
            return [path]

    svc = A.build_enrich_service(db)
    svc.client = Fake()

    svc.save_preview_video("TEST-001", "https://x/p.m3u8")
    svc.save_preview_video("TEST-001", "https://x/p.m3u8")

    assert calls["n"] == 1, "第二次不该重下"


def test_save_preview_video_reports_failure(web):
    """下载失败要说清原因，不能假装成功。"""

    _, db, _ = web

    import web.app as A

    class Fake:
        def download_assets(self, lines, directory, timeout=300, filename=None):
            return []

    svc = A.build_enrich_service(db)
    svc.client = Fake()

    got = svc.save_preview_video("TEST-001", "https://x/p.m3u8")

    assert got["ok"] is False
    assert "过期" in got["error"] or "失败" in got["error"]


def test_detail_renders_video_player_when_saved(web):
    """落盘后详情页要有 <video> 播放器（不是外链）。"""

    client, _, previews = web

    d = os.path.join(previews, "TEST-001")
    os.makedirs(d, exist_ok=True)

    with open(os.path.join(d, "preview.mp4"), "wb") as f:
        f.write(b"x" * 100)

    r = client.get("/detail/TEST-001")

    assert 'id="preview-player"' in r.text
    assert "<video" in r.text
    assert "/previews/TEST-001/preview.mp4" in r.text
    # 不再是跳到 m3u8 的外链
    assert 'target="_blank"' not in r.text or ".m3u8" not in r.text


def test_detail_shows_save_button_before_saved(web):
    """没存过时给「存宣传视频」入口（而不是假装能播）。"""

    client, db, _ = web

    db.conn.execute(
        "INSERT INTO metadata (title_id, preview_video) "
        "SELECT id, 'https://x/p.m3u8' FROM titles WHERE number='TEST-001'"
    )
    db.conn.commit()

    r = client.get("/detail/TEST-001")

    assert "savePreview(" in r.text
    assert 'id="preview-player"' not in r.text, "没落盘不该有播放器"


def test_video_tile_shows_without_screenshots(web):
    """宣传视频区块不能被「有没有截图」卡住。

    javdb 有宣传视频但没截图的番号是存在的 —— 那种情况下入口也得在。
    """

    client, db, _ = web

    db.conn.execute(
        "INSERT INTO metadata (title_id, preview_video) "
        "SELECT id, 'https://x/p.m3u8' FROM titles WHERE number='TEST-001'"
    )
    db.conn.commit()

    r = client.get("/detail/TEST-001")

    assert "savePreview(" in r.text, "没有截图时视频入口丢了"
