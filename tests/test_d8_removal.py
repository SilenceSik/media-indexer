# -*- coding: utf-8 -*-
"""D8 剔除：查不到的番号要从媒体库消失。

实跑抓到的问题：抓元数据发现「查不到」时，代码只返回了 error，
**记录仍留在库里** —— 结果 49 个查不到的番号（AMQ-13 / CUM-60 /
DVDRIP-1024 …）照样出现在首页和卡片上，与 D8「查不到的不进媒体库」不符。

⚠️ 最要紧的一条约束：**网络故障绝不能触发剔除**。
一次超时就把真番号清掉，那是灾难。只有「确定查不到」才剔。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                        # noqa: E402
from services.enrich_service import EnrichService            # noqa: E402


class Client:
    """可控的假 client。"""

    def __init__(self, detail=None, status="notfound"):
        self._detail = detail
        self._status = status
        self.calls = 0

    def detail(self, number):
        self.calls += 1
        return self._detail

    def detail_checked(self, number):
        self.calls += 1
        return self._detail, self._status


def build(tmp_path, client, fallback=None):
    db = Database(str(tmp_path / "lib.db"))

    svc = EnrichService(
        db,
        covers_dir=str(tmp_path / "covers"),
        screenshots_dir=str(tmp_path / "shots"),
        client=client,
        fallback_client=fallback,
    )

    return svc, db


def seed(db, number, path=None):
    p = path or "C:/x/{}.mp4".format(number)
    db.add_file(number, p, match_source="std")
    db.add_magnet(number, "magnet:?xt=urn:btih:" + number, verified=1)
    db.conn.commit()
    return p


def counts(db):
    return {
        "titles": db.conn.execute("select count(*) from titles").fetchone()[0],
        "files": db.conn.execute("select count(*) from media_files").fetchone()[0],
        "magnets": db.conn.execute("select count(*) from magnets").fetchone()[0],
    }


# ─────────────────────── 该剔的

def test_notfound_removes_from_library(tmp_path):
    """确定查不到 -> 从媒体库剔除（titles / media_files / magnets 都清）。"""

    svc, db = build(tmp_path, Client(None, "notfound"))

    seed(db, "AMQ-13")

    assert counts(db)["titles"] == 1

    res = svc.enrich("AMQ-13")

    assert res["removed"] == 1, "应剔掉 1 个文件"

    c = counts(db)

    assert c["titles"] == 0, "title 要清掉"
    assert c["files"] == 0, "media_files 要清掉"
    assert c["magnets"] == 0, "magnets 要清掉（留着会挂死关联）"


def test_removal_leaves_audit_trail(tmp_path):
    """**剔除必须留痕** —— 否则用户不知道少了什么。"""

    svc, db = build(tmp_path, Client(None, "notfound"))

    seed(db, "AMQ-13")

    svc.enrich("AMQ-13")

    rows = db.conn.execute(
        "select reason, number, path, detail from filter_log"
    ).fetchall()

    assert rows, "筛选日志里必须有记录"

    reasons = {r[0] for r in rows}

    assert "lookup_notfound" in reasons

    detail = rows[0][3] or ""

    assert "未动" in detail or "磁盘" in detail, "要说明磁盘文件没动"


def test_removal_does_not_touch_disk(tmp_path):
    """剔除只动库，**磁盘文件一个不动**。

    这条很重要：D8 是「不进媒体库」，不是「删文件」。用户可能只是
    番号被识别错了，文件还是好的。
    """

    svc, db = build(tmp_path, Client(None, "notfound"))

    real = tmp_path / "AMQ-13.mp4"
    real.write_bytes(b"x" * 100)

    seed(db, "AMQ-13", str(real))

    svc.enrich("AMQ-13")

    assert real.exists(), "磁盘文件不该被动"
    assert real.read_bytes() == b"x" * 100


def test_removal_clears_metadata_too(tmp_path):
    """metadata 也要清 —— 否则留下孤儿行。"""

    svc, db = build(tmp_path, Client(None, "notfound"))

    seed(db, "AMQ-13")

    db.save_metadata({"number": "AMQ-13", "title": "T", "cover_local": None})

    svc.enrich("AMQ-13")

    n = db.conn.execute("select count(*) from metadata").fetchone()[0]

    assert n == 0


# ─────────────────────── 绝不能剔的

def test_network_error_never_removes(tmp_path):
    """**最要紧的一条**：网络故障/超时绝不能剔。

    一次抖动就把真番号清掉，是灾难。
    """

    for status in ("error:TimeoutExpired", "error:ConnectionError",
                   "error:parse", "error:unrecognized",
                   "error:ambiguous"):
        svc, db = build(tmp_path / status.replace(":", "_"), Client(None, status))

        seed(db, "SSIS-001")

        res = svc.enrich("SSIS-001")

        assert res["removed"] == 0, "{} 不该剔任何东西".format(status)

        assert counts(db)["titles"] == 1, "{} 把真番号删了！".format(status)


def test_ok_never_removes(tmp_path):
    """查到数据当然不剔。"""

    svc, db = build(tmp_path, Client({"number": "SSIS-001"}, "ok"))

    seed(db, "SSIS-001")

    svc.enrich("SSIS-001")

    assert counts(db)["titles"] == 1


def test_fallback_rescues_from_removal(tmp_path):
    """主源查不到但兜底查到了 -> **不该剔**。"""

    fb = Client({"number": "SSIS-001", "title": "T"}, "ok")

    svc, db = build(tmp_path, Client(None, "notfound"), fallback=fb)

    seed(db, "SSIS-001")

    svc.enrich("SSIS-001")

    assert counts(db)["titles"] == 1, "兜底救回来了，不该剔"


def test_fallback_crash_does_not_cause_removal(tmp_path):
    """兜底自己崩了，也**不能**因此判定番号不存在。"""

    class Boom:
        def detail(self, number):
            raise RuntimeError("服务没起")

    svc, db = build(tmp_path, Client(None, "notfound"), fallback=Boom())

    seed(db, "SSIS-001")

    svc.enrich("SSIS-001")

    assert counts(db)["titles"] == 1, "兜底崩了不等于番号不存在"


def test_client_without_detail_checked_still_safe(tmp_path):
    """老 client 没有 detail_checked -> 退化成按 None 判断。

    但这条路径**只在明确返回 None 时**才剔，语义与 notfound 一致。
    """

    class Old:
        def detail(self, number):
            return None

    svc, db = build(tmp_path, Old())

    seed(db, "AMQ-13")

    svc.enrich("AMQ-13")

    assert counts(db)["titles"] == 0


# ─────────────────────── 多文件

def test_removes_all_files_of_a_title(tmp_path):
    """一个番号挂多个文件 -> 全剔，且每个都进日志。"""

    svc, db = build(tmp_path, Client(None, "notfound"))

    db.add_file("AMQ-13", "C:/x/AMQ-13-A.mp4", match_source="std")
    db.add_file("AMQ-13", "C:/x/AMQ-13-B.mp4", match_source="std")
    db.add_file("AMQ-13", "C:/x/AMQ-13-C.mp4", match_source="std")
    db.conn.commit()

    res = svc.enrich("AMQ-13")

    assert res["removed"] == 3

    assert counts(db)["files"] == 0

    n_log = db.conn.execute(
        "select count(*) from filter_log where reason='lookup_notfound'"
    ).fetchone()[0]

    assert n_log == 3, "每个文件都要留一条痕"


def test_no_files_means_no_removal_needed(tmp_path):
    """番号不在库里 -> 剔除动作优雅返回 0，不报错。"""

    svc, db = build(tmp_path, Client(None, "notfound"))

    res = svc.enrich("NEVER-SEEN")

    assert res["removed"] == 0


# ─────────────────────── 档位：没有磁力也要判

def test_no_magnets_still_gets_tier(tmp_path):
    """**查到番号但一条磁力都没有 -> 档位必须是「极低」，不能留 NULL。**

    早前 `_judge_and_store` 被 `and magnets` 挡住，这种条目的 tier
    一直是 NULL —— 而 D11 说这种就是极低置信。NULL 在界面上看起来像
    「没抓过」，用户会以为任务漏跑了（实测 KW-7142）。
    """

    svc, db = build(
        tmp_path,
        Client({"number": "KW-7142", "magnets": []}, "ok"),
    )

    seed(db, "KW-7142")

    res = svc.enrich("KW-7142")

    assert not res.get("error"), "查到了就不该报错"

    row = db.conn.execute(
        "select tier from titles where number='KW-7142'"
    ).fetchone()

    assert row is not None, "番号该在库里"

    assert row[0] == "极低", "0 磁力 = 极低置信，实际 {}".format(row[0])


def test_judged_even_when_magnets_missing_key(tmp_path):
    """detail 里压根没有 magnets 键，也要定档。"""

    svc, db = build(tmp_path, Client({"number": "KW-7142"}, "ok"))

    seed(db, "KW-7142")

    svc.enrich("KW-7142")

    tier = db.conn.execute(
        "select tier from titles where number='KW-7142'"
    ).fetchone()[0]

    assert tier == "极低", "实际 {}".format(tier)

