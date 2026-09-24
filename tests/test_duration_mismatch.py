# -*- coding: utf-8 -*-
"""时长「完全不符」判定 + 扣到 0 + 踢出媒体库。

主人 2026-09-24 定：像 FH-27 那种「9 分钟 vs 130 分钟」的**超大差距**，
不是轻扣分的事 —— 要扣到 0 分，并在抓元数据时从媒体库里踢出去。

## 与「时长只作评分」的关系（两段式，别混）

`duration_check` 模块开头写着「只作置信度评分，不作硬标准」，那针对的是
**小偏差**：AV 传播时加几分钟广告片头、同目录放个番号预览片 —— 拿这个
拒绝真片是误杀。本文件管的是**另一个量级**：本地这一堆文件与元数据
根本不是同一部片。

## 两道保命的豁免（都来自实测，不是理论）

实测 296 个番号、326 个文件：
    p1  = 0.238   p10 = 0.979   p50 = 1.006   p90 = 1.043
正常片子紧堆在 1.0，坏的掉到 0.25 以下，**中间是空的**。

但有两部**真片**落在 0.25 以下 —— 因为它们是**分片**（一部片拆成几个文件）：
    KSDO-021  5 文件  中位数 0.193  **求和比 1.00**
    SDMU-960  4 文件  中位数 0.212  **求和比 0.85**
只按中位数一刀切会把这两部真片杀掉。所以必须先看求和比。
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from core import confidence                                    # noqa: E402
from core.database_v2 import Database                          # noqa: E402
from core.duration_check import (                              # noqa: E402
    MISMATCH_RATIO,
    SPLIT_SUM_HI,
    SPLIT_SUM_LO,
    mismatch_verdict,
    number_in_filename,
    times_corroborate,
)


def _pairs(locals_min, ref_min):
    """(本地分钟列表, 元数据分钟) -> mismatch_verdict 要的 (秒, 秒) 对。"""

    return [(m * 60.0, ref_min * 60.0) for m in locals_min]


# ═══════════════════════ 判定本身

def test_fh27_like_case_is_mismatch():
    """主人报的原型：25 个 9.7 分钟短片 vs 130 分钟正片。"""

    v = mismatch_verdict(_pairs([9.7] * 25, 130))

    assert v["mismatch"] is True
    assert v["reason"] == "duration_mismatch"
    assert v["median"] == pytest.approx(9.7 / 130, abs=1e-6)
    # 求和比 25*9.7/130 ≈ 1.87 -> 远超分片上限，不能被误判成分片
    assert v["sum_ratio"] > SPLIT_SUM_HI


def test_normal_episode_is_not_mismatch():
    """正常片子（比例 ~1.0）不判不符。"""

    v = mismatch_verdict(_pairs([127.8], 125))

    assert v["mismatch"] is False
    assert v["reason"] == "ratio_ok"


def test_ad_head_is_not_mismatch():
    """加了几分钟广告片头的真片 —— 这正是「不能拒绝」的那一类。

    125 分钟的片被加 5 分钟片头 -> 比例 1.04，远高于门槛。
    """

    v = mismatch_verdict(_pairs([130], 125))

    assert v["mismatch"] is False


def test_short_ad_is_not_mismatch():
    """短广告：20 分钟的片加 1 分钟。"""

    v = mismatch_verdict(_pairs([21], 20))

    assert v["mismatch"] is False


# ═══════════════════════ 分片豁免（防误杀真片）

def test_split_across_files_is_exempt():
    """KSDO-021 实测形态：5 个文件，每个只有 19%，但加起来正好 100%。

    **没有这道豁免，这部真片会被扣到 0 并踢出库。**
    """

    # 5 段，每段 180/5 = 36 分钟，元数据 180 分钟
    v = mismatch_verdict(_pairs([36.0] * 5, 180))

    assert v["median"] < MISMATCH_RATIO, "前提：单看每文件确实很低"
    assert v["mismatch"] is False, "分片必须被豁免"
    assert v["reason"] == "split_across_files"


def test_split_sdmu960_shape_is_exempt():
    """SDMU-960 实测形态：4 文件、中位数 0.212、求和比 0.85。"""

    # 4 段，合计 0.85 * 568 = 482.8 分钟
    v = mismatch_verdict(_pairs([120.7] * 4, 568))

    assert v["median"] < MISMATCH_RATIO
    assert v["mismatch"] is False
    assert v["reason"] == "split_across_files"


def test_single_file_never_exempted_as_split():
    """一个文件永远不是分片 —— 单文件短了就是不符。"""

    # 1 个文件、比例 0.24（IPX-951 实测形态），求和比也是 0.24
    v = mismatch_verdict(_pairs([38], 160))

    assert v["mismatch"] is True, "单文件不该靠分片豁免逃掉"
    assert v["reason"] == "duration_mismatch"


def test_two_short_files_not_mistaken_for_split():
    """2 个文件但加起来仍远不够 -> 不是分片（DSVR-219 实测形态）。"""

    # 2 个文件合计 0.48 * 115 = 55.2 分钟
    v = mismatch_verdict(_pairs([27.6, 27.6], 115))

    assert v["median"] < MISMATCH_RATIO
    assert v["sum_ratio"] < SPLIT_SUM_LO, "求和比不在分片区"
    assert v["mismatch"] is True


# ═══════════════════════ 证据不足绝不杀

def test_no_data_never_mismatch():
    """没数据 = 「不知道」，不是「不合格」。"""

    for empty in (None, [], [(None, None)], [(0, 0)], [(100, 0)]):
        v = mismatch_verdict(empty)

        assert v["mismatch"] is False, "{} 不该判不符".format(empty)
        assert v["reason"] == "no_data"


def test_times_corroborate_bounds():
    assert times_corroborate(SPLIT_SUM_LO) is True
    assert times_corroborate(SPLIT_SUM_HI) is True
    assert times_corroborate(SPLIT_SUM_LO - 0.01) is False
    assert times_corroborate(SPLIT_SUM_HI + 0.01) is False
    assert times_corroborate(None) is False


# ═══════════════════════ 扣到 0

def test_confidence_zeroes_on_mismatch():
    """判了不符就归零 —— **且不走 CONSISTENCY_FLOOR 兜底**。

    这正是 FH-27 的病灶：比例 0.925 让 score() 返回 0.0，
    但 floor 把它抬回 0.15，最终显示约 12 分而不是 0。
    """

    base = confidence.score(correct=15, comments=1, matches=True,
                            source="std", ratios=[])
    assert base["base"] > 70, "前提：服务器侧证据很足"

    got = confidence.score(
        correct=15, comments=1, matches=True, source="std",
        ratios=[(0.925, True)], mismatch=True,
    )

    assert got["score"] == 0, "超大差距必须扣到 0"
    assert got["consistency"] == 0.0
    assert got["mismatch"] is True


def test_floor_still_applies_without_mismatch_flag():
    """没判不符时，兜底照旧 —— 防 ffprobe 偶发误读把分一把清光。"""

    got = confidence.score(
        correct=15, comments=1, matches=True, source="std",
        ratios=[(0.925, True)],
    )

    assert got["consistency"] == confidence.CONSISTENCY_FLOOR
    assert got["score"] > 0, "未判不符时仍走兜底，不归零"
    assert got["mismatch"] is False


# ═══════════════════════ enrich 集成：真的踢出库

class _Client:
    """假 client：detail 给 130 分钟。"""

    def __init__(self, duration=130):
        self._d = duration

    def detail(self, number):
        return {"duration": self._d, "magnets": [], "title": "T"}

    def detail_checked(self, number):
        return self.detail(number), "ok"


def _build(tmp_path, files, duration=130):
    """建库 + 塞文件 + 造 EnrichService。返回 (svc, db, title_id)。"""

    from services.enrich_service import EnrichService

    db = Database(str(tmp_path / "lib.db"))

    title_id = db.get_or_create_title("TEST-001")

    for i, path in enumerate(files):

        db.conn.execute(
            "INSERT INTO media_files (title_id, filepath, filename, size) "
            "VALUES (?,?,?,?)",
            (title_id, path, os.path.basename(path), 1000),
        )

    db.conn.commit()

    svc = EnrichService(
        db,
        covers_dir=str(tmp_path / "covers"),
        screenshots_dir=str(tmp_path / "shots"),
        client=_Client(duration),
    )

    return svc, db, title_id


def test_enrich_drops_title_on_duration_mismatch(tmp_path, monkeypatch):
    """25 个 9.7 分钟文件 vs 130 分钟 -> 抓元数据时把该番号踢出媒体库。"""

    paths = []

    for i in range(25):

        p = tmp_path / "f{:02d}.mp4".format(i)
        p.write_bytes(b"x")
        paths.append(str(p))

    svc, db, _tid = _build(tmp_path, paths)

    # ffprobe 在测试里不能用真文件（都是 1 字节），打桩成 9.7 分钟
    monkeypatch.setattr(
        "core.duration_check.probe_duration", lambda _p, timeout=30: 9.7 * 60
    )

    res = svc.enrich("TEST-001")

    assert res["duration_mismatch"] is True, res
    assert res["removed"] == 25

    # 番号已从库里消失
    assert db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='TEST-001'"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM media_files"
    ).fetchone()[0] == 0

    # 留痕，且**原因码与「查不到」分开**
    reasons = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT reason FROM filter_log"
    )]

    assert reasons == ["duration_mismatch"], reasons


def test_enrich_keeps_split_title(tmp_path, monkeypatch):
    """分片的真片**不能**被踢 —— 这是分片豁免存在的全部意义。"""

    paths = []

    for i in range(5):

        p = tmp_path / "part{}.mp4".format(i)
        p.write_bytes(b"x")
        paths.append(str(p))

    svc, db, _tid = _build(tmp_path, paths, duration=180)

    # 每段 36 分钟，元数据 180 分钟 -> 求和比 1.00
    monkeypatch.setattr(
        "core.duration_check.probe_duration", lambda _p, timeout=30: 36 * 60
    )

    res = svc.enrich("TEST-001")

    assert res["duration_mismatch"] is False, res
    assert res.get("duration_verdict") == "split_across_files"

    assert db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='TEST-001'"
    ).fetchone()[0] == 1, "分片真片被误杀了"


def test_enrich_keeps_normal_title(tmp_path, monkeypatch):
    """正常片子不受影响。"""

    p = tmp_path / "ok.mp4"
    p.write_bytes(b"x")

    svc, db, _tid = _build(tmp_path, [str(p)])

    monkeypatch.setattr(
        "core.duration_check.probe_duration", lambda _p, timeout=30: 127.8 * 60
    )

    res = svc.enrich("TEST-001")

    assert res["duration_mismatch"] is False
    assert db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='TEST-001'"
    ).fetchone()[0] == 1


def test_enrich_does_not_drop_when_probe_fails(tmp_path, monkeypatch):
    """探不到时长（ffprobe 挂/坏文件）-> **绝不剔除**。"""

    p = tmp_path / "bad.mp4"
    p.write_bytes(b"x")

    svc, db, _tid = _build(tmp_path, [str(p)])

    monkeypatch.setattr(
        "core.duration_check.probe_duration", lambda _p, timeout=30: None
    )

    res = svc.enrich("TEST-001")

    assert res["duration_mismatch"] is False
    assert db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='TEST-001'"
    ).fetchone()[0] == 1, "没数据不该杀"


# ═══════════════════════ 真数据回归（有库才跑）

_REAL_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage", "library_v2.db",
)


@pytest.mark.skipif(not os.path.exists(_REAL_DB), reason="没有真实库")
def test_real_library_flags_only_known_mismatches():
    """拿真实库跑一遍：**只**该杀那两个实测的误匹配。

    这条是防回归的护栏 —— 阈值一动，这里就会响。

    期望值来自 2026-09-24 实测（296 番号 / 326 文件）：
      * 该杀：IPX-951（0.238）、DSVR-219（0.239）
      * 该活：KSDO-021（0.193，求和比 1.00）、SDMU-960（0.212，求和比 0.85）
        —— 这两部是**分片真片**，单看每文件都极低，靠求和豁免
    """

    import sqlite3

    conn = sqlite3.connect(_REAL_DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT t.number, mf.duration_local AS local,
               mf.duration_ref AS ref
        FROM media_files mf
        JOIN titles t ON t.id = mf.title_id
        WHERE mf.duration_local IS NOT NULL
          AND mf.duration_ref IS NOT NULL AND mf.duration_ref > 0
    """).fetchall()

    conn.close()

    by_title = {}

    for r in rows:
        by_title.setdefault(r["number"], []).append(
            (float(r["local"]), float(r["ref"]) * 60.0)
        )

    # ⚠️ 期望值 2026-09-24 被主人校准过一次：
    #   * `DSVR-219` 曾是误报 —— 它是**分成了 D/E 两段**（番号就在文件名里），
    #     不是认错片。加「番号在文件名 + ≥2 文件」豁免后不再剔。
    #   * `IPX-951` 仍该剔：单片、番号只在方括号里、38 分钟 vs 160 分钟。
    def _partial(num, rows_):
        conn2 = sqlite3.connect(_REAL_DB)

        files = conn2.execute(
            "SELECT filename FROM media_files mf JOIN titles t "
            "ON t.id = mf.title_id WHERE t.number = ?", (num,)
        ).fetchall()

        conn2.close()

        return len(files) >= 2 and all(
            number_in_filename(num, f[0] or "") for f in files
        )

    flagged = {
        n for n, pairs in by_title.items()
        if mismatch_verdict(pairs, partial_ok=_partial(n, pairs))["mismatch"]
    }

    assert flagged == {"IPX-951"}, \
        "命中集合变了：{}".format(sorted(flagged))

    # 分片/分段真片必须活着
    for number in ("KSDO-021", "SDMU-960", "DSVR-219"):
        if number in by_title:
            v = mismatch_verdict(
                by_title[number], partial_ok=_partial(number, by_title[number])
            )

            assert v["mismatch"] is False, "{} 是分片真片，被误杀".format(number)
            assert v["reason"] in ("split_across_files", "partial_content")
