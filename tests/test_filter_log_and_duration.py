# -*- coding: utf-8 -*-
"""筛选日志 + 时长评分 测试。

两个关键约束（主人 2026-09-24 定）：

  1. **时长只作置信度评分，绝不作硬标准** ——
     AV 传播时常被加几分钟广告，同目录下还可能有几分钟的番号预览片，
     拿它当拒绝理由会误杀真片。
  2. **被筛掉的都要留痕、带原因** —— 以前只存在任务内存里，结束就没了。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import filter_log                                   # noqa: E402
from core.database_v2 import Database                         # noqa: E402
from core.duration_check import (                             # noqa: E402
    ABS_OK_SECONDS,
    RATIO_OK,
    compare,
    is_likely_fragment,
    probe_duration,
    score,
)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "lib.db"))


# ═══════════════════════ 筛选日志

def test_filter_log_table_created(db):
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(filter_log)")}

    for c in ("path", "filename", "number", "confidence",
              "reason", "detail", "stage"):
        assert c in cols, "缺列 {}".format(c)


def test_record_and_summary(db):
    filter_log.record(db, path="E:/a/x.mp4", filename="x.mp4",
                      reason="size_below_min", stage="scan")
    filter_log.record(db, path="E:/a/y.mp4", filename="y.mp4",
                      reason="size_below_min", stage="scan")
    filter_log.record(db, path="E:/b/z.mp4", filename="z.mp4",
                      reason="lookup_notfound", stage="lookup")
    filter_log.flush(db)

    s = {r["reason"]: r["count"] for r in filter_log.summary(db)}

    assert s["size_below_min"] == 2
    assert s["lookup_notfound"] == 1


def test_summary_carries_human_label(db):
    """原因码要带人话 —— 界面上直接显示，不该让用户看码。"""

    filter_log.record(db, reason="tier_too_low", stage="tier")
    filter_log.flush(db)

    r = filter_log.summary(db)[0]

    assert r["label"]
    assert r["label"] != r["reason"]


def test_entries_filter_by_reason(db):
    filter_log.record(db, path="E:/1.mp4", reason="duration_off")
    filter_log.record(db, path="E:/2.mp4", reason="lookup_notfound")
    filter_log.flush(db)

    only = filter_log.entries(db, reason="duration_off")

    assert len(only) == 1
    assert only[0]["path"] == "E:/1.mp4"


def test_record_never_raises_on_bad_db():
    """日志是旁路 —— 写失败不能把主流程带崩。"""

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("库坏了")

    filter_log.record(Broken(), path="x", reason="whatever")   # 不该抛
    filter_log.flush(Broken())


def test_unknown_reason_returns_itself():
    """不认识的原因码原样返回，不吞、不编。"""

    assert filter_log.label("some_new_code") == "some_new_code"
    assert filter_log.label(None) == ""


# ═══════════════════════ 时长比对

def test_compare_within_tolerance():
    """实测真正片偏差 0.1%~5.8%，必须判为吻合。"""

    # 125 分钟 -> 127.8 分钟（实测 ABP-171）
    c = compare(127.8 * 60, 125)
    assert c["ok"] is True
    assert c["ratio"] < 0.03

    # 210 -> 210.7（实测 ABW-228）
    assert compare(210.7 * 60, 210)["ok"] is True


def test_compare_catches_fragment():
    """实测片段：IPX-879-A.mp4 只有 0.6 分钟，元数据 120 分钟。"""

    c = compare(0.6 * 60, 120)

    assert c["ok"] is False
    assert c["ratio"] > 0.9


def test_ads_do_not_trip_the_check():
    """**核心约束**：加了广告片头的真片不能被判成不吻合。

    主人明确说过 AV 传播时会被加几分钟广告 —— 这是不让时长当硬标准的理由。
    """

    # 120 分钟的片加了 6 分钟广告
    assert compare((120 + 6) * 60, 120)["ok"] is True

    # 短一点的片：60 分钟加 5 分钟广告
    assert compare((60 + 5) * 60, 60)["ok"] is True


def test_absolute_tolerance_helps_short_works():
    """短片按纯相对算容易误伤，绝对阈值兜住。"""

    # 20 分钟的片加 4 分钟 = 20% 相对偏差，但绝对只差 4 分钟 < 5 分钟
    c = compare((20 + 4) * 60, 20)

    assert c["ratio"] > RATIO_OK, "相对确实超了"
    assert c["ok"] is True, "但绝对差在容差内，应判吻合"
    assert abs(c["delta_seconds"]) <= ABS_OK_SECONDS


def test_compare_handles_missing_data():
    """缺任一侧就当没有基准 —— 不猜、不拦。"""

    assert compare(None, 120) is None
    assert compare(7200, None) is None
    assert compare(0, 120) is None
    assert compare(7200, 0) is None


def test_score_is_bounded():
    """评分 0~1，完全吻合 -> 1。"""

    assert score(0) == 1.0
    assert score(0.20, shorter=True) == 0.0
    assert score(0.60) == 0.0
    assert score(None) is None


def test_score_is_asymmetric():
    """**长短不对称**：短了可疑，长了正常。

    实测依据：真片的偏差全是**正的**（比元数据长 3~6%，那是片头广告），
    而片段文件只有正片的 0.5%。若开对称算法，一个偏长 12% 的真片只得
    0.42 分，看着像出了问题 —— 但它完全正常。
    """

    # 同一个 12% 偏差
    longer = score(0.12)                 # 偏长
    shorter = score(0.12, shorter=True)  # 偏短

    assert longer > shorter, "偏长该比偏短得分高"
    assert longer > 0.8, "偏长 12% 是常态（广告），不该显著扣分"
    assert shorter < 0.5, "偏短 12% 值得警惕"


def test_is_likely_fragment_only_flags_shorter():
    """片段 = 明显**短于**元数据。长出来的（广告）不算片段。"""

    assert is_likely_fragment(0.6 * 60, 120) is True

    # 加了广告 -> 更长 -> 不是片段
    assert is_likely_fragment((120 + 10) * 60, 120) is False


def test_probe_duration_never_raises_on_missing_file():
    """取不到就 None，不抛 —— 这个函数在遍历几万文件的路径上。"""

    assert probe_duration("Z:/definitely/not/here.mp4") is None
    assert probe_duration("") is None
    assert probe_duration(None) is None


# ═══════════════════════ 端到端：时长绝不能拦删除

def test_duration_never_blocks_deletion(tmp_path):
    """**时长差得多也不得拦删除** —— 这是主人的明确约束。

    只作评分与提示；`deletion_eligibility()` 不看 duration 字段。
    """

    db = Database(str(tmp_path / "lib.db"))

    db.add_file("ABP-171", "C:/x/ABP-171.mp4", match_source="std")

    tid = db.conn.execute(
        "SELECT id FROM titles WHERE number='ABP-171'"
    ).fetchone()[0]

    db.save_tier_evidence(tid, {
        "number": "ABP-171", "correct": 30, "comments": None,
        "tier": "高", "javdb_number": "ABP-171", "matches": True,
        "source": "javdb", "magnets": [],
    })

    before = db.deletion_eligibility()[0]
    assert before["batch"] is True

    # 故意把时长写成差得离谱
    db.conn.execute(
        "UPDATE media_files SET duration_local=30, duration_ref=7200, "
        "duration_ratio=0.996 WHERE title_id=?", (tid,)
    )
    db.conn.commit()

    after = db.deletion_eligibility()[0]

    assert after["batch"] is True, (
        "时长差不该影响删除资格 —— 主人明确要求它只作评分"
    )
