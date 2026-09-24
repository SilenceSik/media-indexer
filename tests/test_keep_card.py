# -*- coding: utf-8 -*-
"""「删文件保卡」的回归护栏 + 全项目口径自检。

主人 2026-09-24 定的口径：
  * 卡是这部片的档案，删源文件只做磁盘管理 -> **卡要留着**
  * 一个番号一张卡（不是一文件一张）
  * 所有「本地文件」相关口径只认**存活**文件

这里面最容易再犯的错，是**新增/修改查询时忘了过滤 `local_deleted`** ——
它不会报错，只会让「已删的文件」在某些页面上阴魂不散（详情页列出来、
占用算进去、置信分被它带偏）。所以除了断言行为，下面还有一条
**源码级自检**：把项目里查 media_files 的语句扫一遍，凡是统计/展示/判定
用途的，都必须在场或在 SQL 里排除已删。
"""

import os
import re
import shutil
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys

sys.path.insert(0, ROOT)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """临时库 + 一个有两份文件的番号（好验证「删一份」的中间态）。"""

    import web.app as A

    from core.database_v2 import Database

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    db.conn.execute(
        "INSERT INTO titles (number, tier, correct_magnets, comments_count, "
        "number_matches) VALUES ('TEST-001', '高', 12, 40, 1)"
    )

    for i in (1, 2):

        db.conn.execute(
            "INSERT INTO media_files (title_id, filepath, filename, size, "
            "match_source, match_confidence) "
            "SELECT id, ?, ?, 1000000, 'std', 90 FROM titles "
            "WHERE number='TEST-001'",
            ("D:/x/TEST-001-{}.mp4".format(i), "TEST-001-{}.mp4".format(i)),
        )

    db.conn.commit()

    monkeypatch.setattr(A, "DB_PATH", db_path)
    monkeypatch.setattr(A, "PREVIEWS_DIR", str(tmp_path / "previews"))
    os.makedirs(str(tmp_path / "previews"), exist_ok=True)

    from fastapi.testclient import TestClient

    return TestClient(A.app), db, A


def _mark_deleted(db, one=True):
    """把 TEST-001 的文件标记为已删（模拟送过回收站）。

    ⚠️ `one=True` 时**明确指定 -2**，不靠 rowid 顺序猜 ——
    早前写 `rowid ... LIMIT 1`，实际删的是 -1，于是断言删掉的那个
    在候选里找不到，测试假失败。
    """

    if one:
        db.conn.execute(
            "UPDATE media_files SET local_deleted = 1, deleted_time = 1 "
            "WHERE filepath LIKE '%TEST-001-2%'"
        )
    else:
        db.conn.execute(
            "UPDATE media_files SET local_deleted = 1, deleted_time = 1 "
            "WHERE filepath LIKE 'D:/x/TEST-001%'"
        )

    db.conn.commit()


# ═══════════════════ 一题一卡

def test_one_card_per_title(lib):
    """首页卡数 == 番号数（不是一个文件一张）。"""

    client, _, _ = lib

    html = client.get("/").text

    nums = re.findall(r'<a class="num" href="/detail/([^"]+)"', html)

    assert nums.count("TEST-001") == 1, "同一番号出了多张卡"


def test_card_lists_all_live_files(lib):
    """多文件时卡上标出数量。"""

    client, _, _ = lib

    html = client.get("/").text

    assert "等 2 个" in html


# ═══════════════════ 删一半：卡在，只算存活

def test_partial_delete_keeps_card(lib):
    client, db, _ = lib

    _mark_deleted(db, one=True)

    html = client.get("/").text

    assert "TEST-001" in html
    assert "等 1 个" not in html, "只剩 1 个存活时不该再说「等 N 个」"
    # ⚠️ 认**卡片上的标记**（`class="fn gone"`），不要认那句话 ——
    # 「本地文件已删」也出现在 JS 注释与禁用按钮的 title 里，
    # 那些本来就总在页面上，断言会假红（反之亦然）。
    assert 'class="fn gone"' not in html, "还有存活文件，不该标全删"


def test_full_delete_keeps_card_and_marks_it(lib):
    client, db, _ = lib

    _mark_deleted(db, one=False)

    html = client.get("/").text

    assert "TEST-001" in html, "全删后卡必须还在（保卡）"
    assert 'class="fn gone"' in html, "全删后卡上应标出来"
    assert "已送回收站" in html


def test_deleted_files_not_counted_in_stats(lib):
    """文件数与占用只算存活 —— 否则删完占用不变，像没删掉。"""

    client, db, _ = lib

    def stat(name):
        html = client.get("/").text
        m = re.search(
            r'<div class="stat"><b>([^<]+)</b><span>{}</span>'.format(name),
            html,
        )
        return m.group(1) if m else None

    assert stat("文件") == "2"

    _mark_deleted(db, one=True)

    assert stat("文件") == "1", "删一个就该少一个"
    assert stat("部已编号") == "1", "番号数不该变"


# ═══════════════════ 详情页

def test_detail_hides_deleted_files_and_stays_consistent(lib):
    """详情页不列已删文件，且分数与首页一致（两者都只看存活）。"""

    client, db, _ = lib

    # 给两个文件不同的时长，让「用哪些算」能影响分数
    db.conn.execute(
        "UPDATE media_files SET duration_local = 7200, duration_ref = 120 "
        "WHERE filepath LIKE '%TEST-001-1%'"
    )
    db.conn.execute(
        "UPDATE media_files SET duration_local = 100, duration_ref = 120 "
        "WHERE filepath LIKE '%TEST-001-2%'"
    )
    db.conn.commit()

    def score(html):
        m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', html)
        return int(m.group(1)) if m else None

    def home_score(number):
        """按番号取那张卡的分（首页是多卡列表，不能拿第一个冒充）。"""

        for c in re.split(r'<div class="shell">', client.get("/").text)[1:]:

            if '<a class="num" href="/detail/{}"'.format(number) in c:

                return score(c)

        return None

    home_before = home_score("TEST-001")
    detail_before = score(client.get("/detail/TEST-001").text)

    assert home_before == detail_before, "删除前就该一致"

    # 把那个「时长离谱」的文件标掉 -> 分数应回升
    db.conn.execute(
        "UPDATE media_files SET local_deleted = 1, deleted_time = 1 "
        "WHERE filepath LIKE '%TEST-001-2%'"
    )
    db.conn.commit()

    home_after = home_score("TEST-001")
    detail_after = score(client.get("/detail/TEST-001").text)

    assert home_after == detail_after, \
        "删除后两处口径不一致：首页 {} / 详情 {}".format(home_after, detail_after)

    # 详情页不再列出已删文件
    d = client.get("/detail/TEST-001").text
    assert "TEST-001-2.mp4" not in d, "已删文件不该出现在本地文件列表里"
    assert "TEST-001-1.mp4" in d, "存活文件要显示"


def test_all_deleted_keeps_scores_consistent(lib):
    """**全删**后两处仍要同分。

    这是实跑抓到的真 bug：详情页把「识别形态（match_source）」也跟着
    存活过滤一起砍了，而识别形态是**整部片**的属性、不该因为删了文件
    而改变 —— 于是首页 95 / 详情 80 分叉。

    正确切法：识别形态取**全部行**，时长配对只取**存活行**。
    """

    client, db, _ = lib

    db.conn.execute(
        "UPDATE media_files SET duration_local = 7200, duration_ref = 120 "
        "WHERE filepath LIKE '%TEST-001-1%'"
    )
    db.conn.execute(
        "UPDATE media_files SET duration_local = 100, duration_ref = 120 "
        "WHERE filepath LIKE '%TEST-001-2%'"
    )
    db.conn.commit()

    _mark_deleted(db, one=False)

    def score(html):
        m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', html)
        return int(m.group(1)) if m else None

    for c in re.split(r'<div class="shell">', client.get("/").text)[1:]:

        if '<a class="num" href="/detail/TEST-001"' in c:

            home = score(c)
            break
    else:
        raise AssertionError("全删后卡不见了")

    detail = score(client.get("/detail/TEST-001").text)

    assert home == detail, \
        "全删后口径分叉：首页 {} / 详情 {}".format(home, detail)


def test_detail_shows_no_action_for_all_deleted(lib):
    """全删后详情页的「位置」按钮该禁用。"""

    client, db, _ = lib

    _mark_deleted(db, one=False)

    d = client.get("/detail/TEST-001").text

    assert d  # 页面要能开（档案还在）
    assert "本地文件已删" in d or "disabled" in d


# ═══════════════════ 低分清理不该带走已删的卡

def test_low_score_cleanup_ignores_deleted(lib):
    """低分清理只扫存活文件 —— 否则会把保住的卡又清掉。"""

    _, db, A = lib

    svc = A.build_file_service()

    _mark_deleted(db, one=True)

    cands = svc.low_score_entries(max_score=999)

    paths = [c["filepath"] for c in cands]

    assert not any("TEST-001-2" in p for p in paths), \
        "已删文件不该出现在低分清理候选里"


# ═══════════════════ 全项目口径自检（防再犯）

# 这些查询是**故意**不过滤的，列出来免得自检误报；每处都写明理由
ALLOWLINE = [
    # 按路径认身份：文件若重现要复用同一个 title，所以必须看得到已删行
    "SELECT title_id",
    # 剔除（D8 / 时长不符）就是要连行一起清掉
    "SELECT id, filepath, filename, size",
    # 按路径的存在性检查（工具的幂等判据），不是「枚举本地文件」
    "SELECT 1",
]

ALLOWLIST = {
    "services/file_service.py",
    "services/backup_service.py",
    "services/enrich_service.py",
    "core/database_v2.py",          # 保卡靠它撑（add_file 的 upsert）
    "tools/drop_false_match.py",
}


def test_no_query_forgets_local_deleted():
    """扫一遍项目里查 media_files 的 SELECT，统计/展示用途的必须排除已删。

    这条是**防再犯**的：忘了过滤不会报错，只会让已删的文件在某个页面上
    阴魂不散（详情页列出来、占用算进去、置信分被它带偏）。

    已经用它抓到并修掉了 5 处：详情页文件列表、详情页时长因子、
    低分清理扫描、`deletable_titles` 的文件数与占用、`search_number`。
    """

    offenders = []

    for dirpath, dirnames, filenames in os.walk(ROOT):

        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "__pycache__", "node_modules", "tests",
                         "storage")
        ]

        for fn in filenames:

            if not fn.endswith(".py"):
                continue

            full = os.path.join(dirpath, fn)

            rel = os.path.relpath(full, ROOT).replace("\\", "/")

            if rel in ALLOWLIST:
                continue

            with open(full, encoding="utf-8", errors="replace") as f:
                src = f.read()

            # 合并续行，让跨行 SQL 也能被识别
            flat = re.sub(r'"\s*\n\s*"', "", src)

            for m in re.finditer(
                r'SELECT[^;]{0,700}?FROM\s+media_files[^;]{0,400}',
                flat,
                re.I,
            ):

                stmt = " ".join(m.group(0).split())

                if "local_deleted" in stmt:
                    continue

                if re.search(r"(INSERT|UPDATE|DELETE)", stmt, re.I):
                    continue

                if any(a in stmt for a in ALLOWLINE):
                    continue

                offenders.append("{}: {}".format(rel, stmt[:95]))

    assert not offenders, (
        "以下查询读了 media_files 但没排除已删（local_deleted）：\n  "
        + "\n  ".join(offenders)
    )
