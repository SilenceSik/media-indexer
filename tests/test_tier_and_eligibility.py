# -*- coding: utf-8 -*-
"""档位落库 + 删除资格（D9/D10/D11）测试。

这段是删除门控的唯一数据来源。**判错就会误删真实文件**，
所以用例围绕「什么情况下必须拒绝」写。
"""

import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                        # noqa: E402


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "lib.db"))


def seed(db, number, size=1024, magnet="magnet:?xt=urn:btih:x", match_source="std"):
    """建一个番号 + 一个文件 + 一条磁力，返回 title_id。

    ``match_source`` 默认 ``std``（结构化形态匹配 = 可信）。
    传 None 可模拟「没记录来源」的老数据；传 ``nosep``/``bare`` 模拟弱形态。
    """

    db.add_file(
        number,
        "C:/x/{}.mp4".format(number),
        match_source=match_source,
    )

    row = db.conn.execute(
        "SELECT id FROM titles WHERE number = ?", (number,)
    ).fetchone()

    tid = row[0]

    db.conn.execute(
        "UPDATE media_files SET size = ? WHERE title_id = ?", (size, tid)
    )

    if magnet:
        db.add_magnet(number, magnet, verified=1)

    db.conn.commit()

    return tid


def evidence(number, correct, comments=None, javdb_number=None, matches=True):
    from core.magnet_judge import tier_of

    if javdb_number is None:
        javdb_number = number

    return {
        "number": number,
        "correct": correct,
        "comments": comments,
        "tier": tier_of(correct, comments),
        "javdb_number": javdb_number,
        "matches": matches,
        "magnets": [],
    }


# ─────────────────────── 迁移

def test_migrate_adds_tier_columns(db):
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(titles)")}

    for c in ("tier", "correct_magnets", "comments_count",
              "javdb_number", "norm_number", "evidence_strong",
              "number_matches"):
        assert c in cols, "缺列 {}".format(c)

    mcols = {r[1] for r in db.conn.execute("PRAGMA table_info(magnets)")}

    assert "is_correct" in mcols


def test_migrate_is_idempotent(tmp_path):
    """重复迁移不能报 duplicate column name。"""

    p = str(tmp_path / "lib.db")

    Database(p)

    Database(p)          # 第二次

    Database(p)          # 第三次


# ─────────────────────── 落库

def test_save_tier_evidence_writes_fields(db):
    tid = seed(db, "ABP-171")

    db.save_tier_evidence(tid, evidence("ABP-171", 12, comments=3))

    r = db.conn.execute(
        "SELECT tier, correct_magnets, comments_count, evidence_strong, "
        "number_matches FROM titles WHERE id = ?", (tid,)
    ).fetchone()

    assert r[0] == "高"
    assert r[1] == 12
    assert r[2] == 3
    assert r[3] == 1, "12 条 >= 10，应标 evidence_strong"
    assert r[4] == 1


def test_save_tier_evidence_stores_norm_number(db):
    """归一化键要落库 —— 前面补零的写法要能对上。"""

    tid = seed(db, "ABP-0041")

    db.save_tier_evidence(tid, evidence("ABP-0041", 5))

    r = db.conn.execute(
        "SELECT norm_number FROM titles WHERE id = ?", (tid,)
    ).fetchone()

    assert r[0] == "ABP-41"


def test_save_tier_evidence_never_lifts_strong_below_ten(db):
    tid = seed(db, "ABP-1")

    db.save_tier_evidence(tid, evidence("ABP-1", 9))

    r = db.conn.execute(
        "SELECT evidence_strong FROM titles WHERE id = ?", (tid,)
    ).fetchone()

    assert r[0] == 0, "9 条不够 strong"


# ─────────────────────── 删除资格：必须拒绝的情形

def test_unevaluated_is_never_deletable(db):
    """**没跑过判定的一律不可删** —— 宁可漏删，不可错删。

    这条最重要：现有库里 355 个番号都还没档位。如果默认放行，
    第一次跑删除就会把整个库删掉。
    """

    seed(db, "ABP-171")          # 有磁力，但没写 tier

    rows = db.deletion_eligibility()

    assert len(rows) == 1

    r = rows[0]

    assert r["tier"] is None
    assert r["batch"] is False
    assert r["manual"] is False
    assert r["blocked_reason"] == "not_evaluated"


def test_tier_too_low_blocked(db):
    """极低档：手动删也拒绝（D10）。"""

    tid = seed(db, "ABP-171")
    db.save_tier_evidence(tid, evidence("ABP-171", 0))

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "极低"
    assert r["batch"] is False
    assert r["manual"] is False
    assert r["blocked_reason"] == "tier_too_low"


def test_low_tier_manual_only(db):
    """低档：只能手动逐条删。"""

    tid = seed(db, "ABP-171")
    db.save_tier_evidence(tid, evidence("ABP-171", 2))

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "低"
    assert r["batch"] is False
    assert r["manual"] is True
    assert r["blocked_reason"] == "tier_low_manual_only"


def test_high_tier_with_mismatch_blocked_from_batch(db):
    """D11 第二条：档位够但 number 核对不过 -> 只能手动。

    这是防「文件名被识别成另一个真实存在的番号」的那道闸。
    """

    tid = seed(db, "ABP-171")
    db.save_tier_evidence(
        tid, evidence("ABP-171", 30, javdb_number="IPX-999", matches=False)
    )

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "高"
    assert r["batch"] is False, "核对不过不得批量"
    assert r["manual"] is True, "但可手动"
    assert r["blocked_reason"] == "number_mismatch"


def test_high_tier_with_match_is_batchable(db):
    tid = seed(db, "ABP-171")
    db.save_tier_evidence(tid, evidence("ABP-171", 30, matches=True))

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "高"
    assert r["batch"] is True
    assert r["blocked_reason"] == ""


def test_very_high_tier_is_batchable(db):
    tid = seed(db, "ABP-171")
    db.save_tier_evidence(tid, evidence("ABP-171", 30, comments=99))

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "极高"
    assert r["batch"] is True


# ─────────────────────── D11 第三条：识别来源 + 文件格式

def test_untrusted_recognition_blocks_batch(db):
    """识别来自弱形态（无分隔符）-> 不得批量，只能手动。

    防的是「文件名被识别成另一个真实热门码」—— 那种情况下磁力与 number
    核对都会通过，只有识别来源这一条能拦。
    """

    tid = seed(db, "ABP-171", match_source="nosep")
    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    r = db.deletion_eligibility()[0]

    assert r["tier"] == "高"
    assert r["batch"] is False
    assert r["manual"] is True
    assert r["blocked_reason"] == "untrusted_recognition"


def test_bare_digits_source_blocks_batch(db):
    """裸数字形态最容易撞号 -> 不得批量。"""

    tid = seed(db, "ABP-171", match_source="bare")
    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    assert db.deletion_eligibility()[0]["batch"] is False


def test_missing_match_source_blocks_batch(db):
    """没记录来源的老数据 -> 不得批量（宁可漏删）。"""

    tid = seed(db, "ABP-171", match_source=None)
    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    r = db.deletion_eligibility()[0]

    assert r["batch"] is False
    assert r["blocked_reason"] == "untrusted_recognition"


def test_atypical_extension_blocks_batch(db):
    """`.webm` 这类非典型后缀 -> 不得批量。

    实测背景：`.webm` 曾是白名单成员，结果把成人游戏目录里的游戏内视频段
    全收了进来。扫描可以放宽（用户的配置），删除不能跟着放宽。
    """

    db.add_file("ABP-171", "C:/x/ABP-171.webm", match_source="std")

    tid = db.conn.execute(
        "SELECT id FROM titles WHERE number = 'ABP-171'"
    ).fetchone()[0]

    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    r = db.deletion_eligibility()[0]

    assert r["batch"] is False
    assert r["blocked_reason"] == "atypical_extension"


def test_typical_extensions_all_pass(db):
    """常见 AV 后缀都要能过第三条（不能误伤）。"""

    from core.av_format import TYPICAL_AV_EXTENSIONS

    for i, ext in enumerate(sorted(TYPICAL_AV_EXTENSIONS)):

        num = "ABP-{:03d}".format(100 + i)

        db.add_file(num, "C:/x/{}{}".format(num, ext), match_source="std")

        tid = db.conn.execute(
            "SELECT id FROM titles WHERE number = ?", (num,)
        ).fetchone()[0]

        db.save_tier_evidence(tid, evidence(num, 30))

    elig = {e["number"]: e for e in db.deletion_eligibility()}

    for num, e in elig.items():
        assert e["batch"] is True, "{} 用了典型后缀却被拒".format(num)


def test_all_three_conditions_order(db):
    """分流顺序：先看档位，再看 number 核对，最后看第三条。

    这里每条断言的是**首要**阻塞原因，顺序错了会误导排查。
    """

    # 极低 + 弱形态：首要原因是档位
    t1 = seed(db, "ABP-001", match_source="nosep")
    db.save_tier_evidence(t1, evidence("ABP-001", 0))

    # 高 + 核对不过 + 弱形态：首要原因是核对
    t2 = seed(db, "ABP-002", match_source="nosep")
    db.save_tier_evidence(
        t2, evidence("ABP-002", 30, javdb_number="X-1", matches=False)
    )

    # 高 + 核对过 + 弱形态：首要原因是识别来源
    t3 = seed(db, "ABP-003", match_source="bare")
    db.save_tier_evidence(t3, evidence("ABP-003", 30))

    reasons = {e["number"]: e["blocked_reason"] for e in db.deletion_eligibility()}

    assert reasons["ABP-001"] == "tier_too_low"
    assert reasons["ABP-002"] == "number_mismatch"
    assert reasons["ABP-003"] == "untrusted_recognition"


# ─────────────────────── 统计字段

def test_bytes_and_file_count(db):
    """省空间统计要看得出真实占用。"""

    tid = seed(db, "ABP-171", size=5 * 1024 ** 3)

    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    r = db.deletion_eligibility()[0]

    assert r["files"] == 1
    assert r["bytes"] == 5 * 1024 ** 3


def test_title_without_files_still_listed(db):
    """番号还在但文件已删 -> 记录保留（D13），字节为 0。"""

    tid = seed(db, "ABP-171")

    db.conn.execute("DELETE FROM media_files WHERE title_id = ?", (tid,))
    db.conn.commit()

    db.save_tier_evidence(tid, evidence("ABP-171", 30))

    r = db.deletion_eligibility()[0]

    assert r["files"] == 0
    assert r["bytes"] == 0
    assert r["tier"] == "高"


def test_eligibility_covers_every_title(db):
    """不能漏番号 —— 漏掉的会显得"不存在"，界面上看不见。"""

    for i in range(5):
        seed(db, "ABP-{}".format(100 + i))

    assert len(db.deletion_eligibility()) == 5


def test_old_gate_would_have_allowed_everything(db):
    """对照：旧门控（只看 verified=1）在这些场景下**全部放行**。

    这条测试用来记住 P0-1 为什么必须换掉 —— 只要有一条挂错的磁力，
    旧门控就认为可删。
    """

    for i in range(3):
        seed(db, "ABP-{}".format(100 + i))

    old = db.deletable_titles()

    assert len(old) == 3, "旧门控：3 个番号全部可删"

    new = db.deletion_eligibility()

    assert all(not r["batch"] and not r["manual"] for r in new), (
        "新门控：没跑过判定 -> 一个都不能删"
    )
