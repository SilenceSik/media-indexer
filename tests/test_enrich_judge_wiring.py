# -*- coding: utf-8 -*-
"""抓取流程里的判定接线（P0-1 / P0-2 的收口测试）。

这两条 P0 是 ROADMAP 里「不修不能真删」的前两条：

  P0-1 旧代码把拿到的**所有**磁力无条件写成 verified=1，门控只要求
       「至少 1 条」—— 1 条挂错的磁力就能放行删除。
  P0-2 不核对 javdb 返回的 number —— 搜错时会把别的片的磁力挂上来，
       还会被错误地定成高档。

这里用假 client 走**真实的 enrich()**，不测仿真。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                        # noqa: E402
from services.enrich_service import EnrichService            # noqa: E402


class FakeClient:
    """假 JavDB client：detail() 返回预设报文。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def detail(self, number):
        self.calls.append(number)
        return self.payload


def build(tmp_path, payload):
    db = Database(str(tmp_path / "lib.db"))

    svc = EnrichService(
        db,
        covers_dir=str(tmp_path / "covers"),
        screenshots_dir=str(tmp_path / "shots"),
        client=FakeClient(payload),
    )

    return svc, db


def payload_for(number, magnet_names, comments=0, returned_number=None):
    return {
        "number": number if returned_number is None else returned_number,
        "title": "T",
        "cover_url": "",
        "release_date": "2020-01-01",
        "maker_name": "M",
        "actors": [],
        "tags": [],
        "screenshots": [],
        "comments_count": comments,
        "magnets": [
            {"name": n, "hash": "{:040x}".format(i), "size": 1000}
            for i, n in enumerate(magnet_names)
        ],
    }


def verified_count(db, number):
    return db.conn.execute(
        """
        SELECT COUNT(*) FROM magnets m
        JOIN titles t ON t.id = m.title_id
        WHERE t.number = ? AND m.verified = 1
        """,
        (number,),
    ).fetchone()[0]


# ─────────────────────── P0-1：verified 不再无条件写 1

def test_unrelated_magnets_are_not_verified(tmp_path):
    """**P0-1 的核心**：不属于该番号的磁力不得标 verified。

    旧代码无条件写 verified=1 —— 这些别的番号的磁力会让门控放行删除。
    """

    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "IPX-999.mp4", "SSIS-001.mp4", "MIDE-002.mp4"],
    ))

    svc.enrich("ABP-171")

    assert verified_count(db, "ABP-171") == 1, (
        "只有 ABP-171.mp4 该被标 verified，其余 3 条是别的番号"
    )


def test_mixed_magnets_tier_uses_correct_count(tmp_path):
    """档位要按**正确**磁力条数算，不是按拿到的总数。"""

    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "IPX-1.mp4", "MIDE-2.mp4", "SSIS-3.mp4", "STARS-4.mp4"],
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT correct_magnets, tier FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == 1, "只有 1 条正确磁力"
    assert r[1] == "低", "1 条 -> 低档（只能手动删）"


def test_all_correct_magnets_reach_high(tmp_path):
    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "[FHD]abp-171.mp4", "ABP-171-UC.torrent.非同厂版本"],
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT correct_magnets, tier FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == 3
    assert r[1] == "高"


def test_zero_correct_magnets_is_very_low(tmp_path):
    """一条都不属于该番号 -> 极低 -> **不能删**。"""

    svc, db = build(tmp_path, payload_for(
        "ABP-171", ["IPX-999.mp4", "MIDE-888.mp4"],
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT correct_magnets, tier FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == 0
    assert r[1] == "极低"

    # 门控必须拒绝
    elig = {e["number"]: e for e in db.deletion_eligibility()}
    assert elig["ABP-171"]["batch"] is False
    assert elig["ABP-171"]["manual"] is False


# ─────────────────────── P0-2：核对返回的 number

def test_number_mismatch_recorded(tmp_path):
    """javdb 返回的 number 与查询的不一致 -> 记 mismatch，禁止批量删。"""

    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["SSIS-001.mp4", "SSIS-002.mp4", "SSIS-003.mp4"],   # 磁力是别的番号的
        returned_number="SSIS-001",                         # 站点返回了别的番号
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT javdb_number, number_matches, tier FROM titles "
        "WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == "SSIS-001"
    assert r[1] == 0, "必须记为不匹配"
    assert r[2] == "极低", "SSIS 的磁力不属于 ABP-171"

    elig = {e["number"]: e for e in db.deletion_eligibility()}

    assert elig["ABP-171"]["batch"] is False


def test_number_match_recorded(tmp_path):
    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "ABP-171.mp4", "abp-171-c.mp4"],
        returned_number="ABP-171",
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT javdb_number, number_matches FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == "ABP-171"
    assert r[1] == 1


def test_mismatch_blocks_batch_even_when_tier_high(tmp_path):
    """D11 的场景：档位够高但核对不过 -> 只能手动。"""

    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "ABP-171-c.mp4", "ABP-171-UC.torrent"],
        returned_number="ABP-999",                           # 不一致
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT tier, number_matches FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == "高"
    assert r[1] == 0

    elig = {e["number"]: e for e in db.deletion_eligibility()}

    assert elig["ABP-171"]["batch"] is False, "核对不过不得批量"
    assert elig["ABP-171"]["manual"] is True, "但可以手动"
    assert elig["ABP-171"]["blocked_reason"] == "number_mismatch"


# ─────────────────────── 评论与极高档

def test_comments_drive_very_high(tmp_path):
    svc, db = build(tmp_path, payload_for(
        "ABP-171",
        ["ABP-171.mp4", "ABP-171-c.mp4", "ABP-171-UC.torrent"],
        comments=80,
    ))

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT tier, comments_count FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == "极高"
    assert r[1] == 80


def test_missing_comments_does_not_crash(tmp_path):
    """javdb 可能不返回评论数 —— 不能因此崩掉整条抓取。"""

    payload = payload_for("ABP-171", ["ABP-171.mp4"])
    payload.pop("comments_count")
    payload["reviews_count"] = None

    svc, db = build(tmp_path, payload)

    svc.enrich("ABP-171")

    r = db.conn.execute(
        "SELECT tier FROM titles WHERE number = 'ABP-171'"
    ).fetchone()

    assert r[0] == "低", "没评论数据时按低档（保守）"


# ─────────────────────── 向后兼容

def test_old_gate_also_becomes_correct(tmp_path):
    """verified 收紧后，旧门控 `EXISTS verified=1` 也自动变正确。"""

    svc, db = build(tmp_path, payload_for(
        "ABP-171", ["IPX-999.mp4", "MIDE-888.mp4"],     # 全不属于该番号
    ))

    svc.enrich("ABP-171")

    old = db.deletable_titles()

    assert all(x["number"] != "ABP-171" for x in old), (
        "旧门控也不该再说 ABP-171 可删"
    )
