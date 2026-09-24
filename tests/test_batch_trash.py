# -*- coding: utf-8 -*-
"""高置信「一键送回收站」+ 删除确认。

主人 2026-09-24 要的：高置信度一键送文件进回收站，**弹窗确认**，
**卡片不删除**。

三条边界（与单卡删除共用同一条路径，靠 `_trash_and_mark` 保证一致）：
  * 只送文件，卡保留（`local_deleted` 标记）
  * 一律走回收站
  * 判据复用 `deletion_eligibility()` 的 `batch`，不另立一套
"""

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """临时库 + 打桩 send2trash（绝不让测试碰真文件）。"""

    import send2trash

    trashed = []

    monkeypatch.setattr(send2trash, "send2trash",
                        lambda p: trashed.append(p))

    import web.app as A

    from core.database_v2 import Database

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    # 高置信：档位「高」+ 核对通过 + 可信形态 + 典型格式 -> 可批量删
    db.conn.execute(
        "INSERT INTO titles (number, tier, correct_magnets, comments_count, "
        "number_matches) VALUES ('FULL-001', '高', 12, 5, 1)"
    )
    # ⚠️ 文件必须**真实存在**：`_trash_and_mark` 对不存在的路径记 `missing`
    # 而不是 `deleted`（这是对的 —— 不存在的文件没什么可删的）。
    # 用合成路径 `D:/x/...` 会让测试看到「删了 0 个」，误以为是 bug。
    real = tmp_path / "FULL-001.mp4"
    real.write_bytes(b"x" * 1024)

    db.conn.execute(
        "INSERT INTO media_files (title_id, filepath, filename, size, "
        "match_source, match_confidence) "
        "SELECT id, ?, 'FULL-001.mp4', 1024, "
        "'dictionary', 95 FROM titles WHERE number='FULL-001'",
        (str(real),),
    )

    # ⚠️ 必须同时给一条**已验证磁力**：批量候选走
    # `deletion_eligibility()`（只看档位/核对/形态/格式），
    # 但真正动手的 `delete_title` 还过一层 `deletable_titles()`
    # —— 那层要求 verified=1 的磁力。缺了它，候选有、执行却没删，
    # 报「该番号没有已验证磁力，按门控规则不可删除」。
    db.conn.execute(
        "INSERT INTO magnets (title_id, magnet, verified, is_correct) "
        "SELECT id, 'magnet:?xt=urn:btih:test', 1, 1 FROM titles "
        "WHERE number='FULL-001'"
    )

    db.conn.commit()

    monkeypatch.setattr(A, "DB_PATH", db_path)
    monkeypatch.setattr(A, "PREVIEWS_DIR", str(tmp_path / "previews"))
    os.makedirs(str(tmp_path / "previews"), exist_ok=True)

    from fastapi.testclient import TestClient

    return TestClient(A.app), db, trashed


# ═══════════════════ 候选与预览

def test_preview_lists_candidates(lib):
    client, _, _ = lib

    j = client.get("/api/delete-batch/preview").json()

    assert j["ok"] is True
    assert j["count"] == 1
    assert j["files"] == 1
    assert j["titles"][0]["number"] == "FULL-001"


def test_preview_excludes_low_confidence(lib):
    """档位不够高的不该进候选 —— 判据复用 batch，不另立一套。"""

    client, db, _ = lib

    db.conn.execute("UPDATE titles SET tier='低' WHERE number='FULL-001'")
    db.conn.commit()

    j = client.get("/api/delete-batch/preview").json()

    assert j["count"] == 0, "「低」档不该进批量删候选"


def test_index_shows_button_only_with_candidates(lib):
    """按钮按候选数显隐。

    ⚠️ 断言要认**按钮的属性**（`onclick="openBatchModal()"`），不能只认
    函数名或「一键送回收站」字样 —— 弹窗模板与 JS 的 `function openBatchModal()`
    本来就总在页面里，那些字样一直都在，断言会永远通过（假绿）。
    """

    client, db, _ = lib

    assert 'onclick="openBatchModal()"' in client.get("/").text

    db.conn.execute("UPDATE titles SET tier='低' WHERE number='FULL-001'")
    db.conn.commit()

    assert 'onclick="openBatchModal()"' not in client.get("/").text, \
        "没有候选时不该出现一键按钮"


# ═══════════════════ 确认（主人明确要求）

def test_index_delete_has_confirm(lib):
    """首页卡片删除必须先确认，并写明「卡片不会删除」。"""

    client, _, _ = lib

    h = client.get("/").text

    assert "confirm(" in h, "删除没有确认弹窗"
    assert "卡片不会删除" in h, "确认文案没说清卡片保留"
    assert "回收站" in h


def test_detail_delete_has_confirm(lib):
    client, _, _ = lib

    d = client.get("/detail/FULL-001").text

    assert "confirm(" in d
    assert "卡片不会删除" in d


def test_batch_modal_explains_boundaries(lib):
    """一键弹窗要写明：动手前看得见、送文件不删卡、走回收站。"""

    client, _, _ = lib

    h = client.get("/").text

    assert 'id="modal-batch"' in h
    assert "高置信可批量删" in h
    assert "卡片不会删除" in h
    assert "回收站" in h


# ═══════════════════ 执行

def test_batch_trashes_files_and_keeps_cards(lib):
    client, db, trashed = lib

    r = client.post("/api/delete-batch")

    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(trashed) == 1, "应送 1 个文件进回收站"

    # 卡（番号行）还在 —— 这是「保卡」的核心
    assert db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='FULL-001'"
    ).fetchone()[0] == 1, "!! 番号被删了"

    # 文件行保留但被标记（不是 DELETE）
    row = db.conn.execute(
        "SELECT local_deleted FROM media_files WHERE filename='FULL-001.mp4'"
    ).fetchone()

    assert row is not None, "文件行不该被物理删除"
    assert row[0] == 1, "应标记 local_deleted"

    # 卡还在首页，且标出已删
    h = client.get("/").text
    assert "FULL-001" in h
    assert "本地文件已删" in h


def test_batch_is_idempotent(lib):
    """跑过一轮后候选必须清空。

    这是实测抓到的 bug：`deletion_eligibility` 的 `files_by_title` 当时
    没排除已删文件，于是候选一直是 285 部，弹窗第二轮仍声称能腾 852 GB
    —— 文件早就不在了，**误导**。
    """

    client, _, _ = lib

    assert client.get("/api/delete-batch/preview").json()["count"] == 1

    client.post("/api/delete-batch")

    j = client.get("/api/delete-batch/preview").json()

    assert j["count"] == 0, "删过一轮后候选没清空（会重复声称可删）"
    assert j["files"] == 0


def test_batch_reports_what_it_did(lib):
    """要如实回报删了什么（不能只说「成功」）。"""

    client, _, _ = lib

    j = client.post("/api/delete-batch").json()

    assert j["titles"] == 1
    assert j["files"] == 1
    assert "已送回收站" in j["message"]
    assert "卡片保留" in j["message"]


def test_batch_never_uses_hard_delete(lib):
    """批量删也必须走回收站 —— 不许出现 os.remove / rmtree。"""

    import io

    root = ROOT

    for rel in ("services/file_service.py",):

        src = io.open(os.path.join(root, rel), encoding="utf-8").read()

        # 只看 send2trash 之外有没有硬删文件的手段
        for bad in ("os.remove(", "os.unlink(", "shutil.rmtree("):

            assert bad not in src, \
                "{} 里出现硬删：{}（删除必须一律走回收站）".format(rel, bad)
