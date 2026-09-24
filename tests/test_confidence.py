"""core.confidence 的测试。

核心命题：**服务器侧证据再强，也不能替本地文件背书。**

FH-27 是真实案例（2026-09-24 实测）：15 条正确磁力、番号核对通过、
来源可信、评论数 1 —— 旧口径 `evidence_strong=1`。但本地 25 个文件
全是 9~28 分钟的短片，元数据 130 分钟，ratio 0.78~0.93。
"""

import math

import pytest

from core import confidence
from core.magnet_judge import TRUSTED_MATCH_SOURCES


# ── 因子 ───────────────────────────────────────────────────────

def test_magnet_factor_monotonic_and_saturates():

    assert confidence.magnet_factor(0) == 0.0
    assert confidence.magnet_factor(1) > 0

    vals = [confidence.magnet_factor(n) for n in (1, 3, 7, 15, 40)]

    assert vals == sorted(vals), "磁力越多分越高"

    assert confidence.magnet_factor(15) == pytest.approx(1.0)
    assert confidence.magnet_factor(100) == pytest.approx(1.0)


def test_magnet_factor_three_is_half():
    """3 条（高档门槛）应当约在半分 —— 门槛够用，但不是满分。"""

    assert confidence.magnet_factor(3) == pytest.approx(0.5, abs=0.03)


def test_magnet_factor_negative_is_zero():
    assert confidence.magnet_factor(-5) == 0.0
    assert confidence.magnet_factor(None) == 0.0


def test_comment_factor_missing_is_zero_not_crash():
    """评论数缺失按下限计，不能抛异常 —— javdb 有时不返回评论数。"""

    assert confidence.comment_factor(None) == 0.0
    assert confidence.comment_factor("bad") == 0.0
    assert confidence.comment_factor(0) == 0.0
    assert confidence.comment_factor(50) == pytest.approx(1.0)
    assert confidence.comment_factor(500) == pytest.approx(1.0)


def test_source_factor_uses_shared_whitelist():
    """来源白名单必须与 magnet_judge 共用一份，防止字面量再次漂移。"""

    for s in TRUSTED_MATCH_SOURCES:
        assert confidence.source_factor(s) == confidence.SOURCE_TRUSTED

    for s in ("nosep", "bare", "fallback", "", None):
        assert confidence.source_factor(s) == confidence.SOURCE_UNTRUSTED


def test_source_factor_unknown_source_is_untrusted():
    """没见过的来源按不可信处理 —— 默认保守。"""

    assert confidence.source_factor("brand-new-source") == confidence.SOURCE_UNTRUSTED


# ── base ───────────────────────────────────────────────────────

def test_base_score_perfect_is_100():

    assert confidence.base_score(
        correct=15, comments=50, matches=True, source="std"
    ) == 100


def test_base_score_zero_evidence_is_low():

    assert confidence.base_score(
        correct=0, comments=0, matches=False, source="bare"
    ) == 0


def test_base_score_no_magnets_but_number_confirmed():
    """有号无磁力不是零证据：javdb 认得这个号，只是没查到磁力。

    这类要拿到中等分（不该跟「查无此号」同分），因为后续还能补抓。
    """

    base = confidence.base_score(
        correct=0, comments=0, matches=True, source="std"
    )

    assert 40 <= base <= 60, base


def test_base_score_strong_server_side():
    """FH-27 的服务器侧数据：15 磁力 / 1 评论 / 核对通过 / 来源可信。"""

    base = confidence.base_score(
        correct=15, comments=1, matches=True, source="dictionary"
    )

    assert base >= 80, "服务器侧证据确实强，base 高是对的"


def test_base_score_matches_false_costs_the_match_weight():
    a = confidence.base_score(15, 1, True, "dictionary")
    b = confidence.base_score(15, 1, False, "dictionary")

    assert a - b == pytest.approx(round(confidence.W_MATCH * 100), abs=1)


# ── 一致性 ─────────────────────────────────────────────────────

def test_consistency_unknown_is_none():
    """没数据返回 None —— 与「数据不合格」必须区分开。"""

    assert confidence.consistency_from_ratios([]) is None
    assert confidence.consistency_from_ratios(None) is None
    assert confidence.consistency_from_ratios([None, (None, True)]) is None


def test_consistency_perfect_when_ratios_zero():

    assert confidence.consistency_from_ratios([(0.0, False)]) == pytest.approx(1.0)
    assert confidence.consistency_from_ratios([(0.0, False)] * 5) == pytest.approx(1.0)


def test_consistency_uses_median_not_mean():
    """中位数：少数极端文件不该拖垮整部片。

    19 个吻合 + 1 个极短，中位数仍在 1.0 附近（平均值会被拉到 0.9）。
    """

    ratios = [(0.01, False)] * 19 + [(0.95, True)]

    cons = confidence.consistency_from_ratios(ratios)

    assert cons > 0.9, "中位数应当忽略单个离群点"


def test_consistency_short_hurts_more_than_long():
    """同样偏差，短了扣得多 —— duration_check.score 的不对称性。"""

    short = confidence.consistency_from_ratios([(0.30, True)])
    long_ = confidence.consistency_from_ratios([(0.30, False)])

    assert short < long_


def test_consistency_outlier_cluster():
    """FH-27 形态：绝大多数文件远短于元数据。"""

    ratios = [(r, True) for r in (0.93, 0.88, 0.12, 0.78, 0.92, 0.91, 0.85, 0.90, 0.86)]

    cons = confidence.consistency_from_ratios(ratios)

    assert cons < 0.3, "全片都是短片，一致性必须低"


def test_file_ratios_recomputes_direction():
    """shorter 要按本地 vs 参考现算，不能只信存的绝对值。"""

    files = [
        {"duration_local": 600, "duration_ref": 130},    # 10 分 vs 130 分 -> 短
        {"duration_local": 8000, "duration_ref": 130},   # 133 分 vs 130 分 -> 长
    ]

    out = confidence.file_ratios(files)

    assert len(out) == 2
    assert out[0][1] is True, "600s < 7800s，短"
    assert out[1][1] is False, "8000s > 7800s，长"


def test_file_ratios_skips_incomplete_rows():
    """缺时长/参考时长的行跳过，不算成 0 偏差。"""

    files = [
        {"duration_local": None, "duration_ref": 130},
        {"duration_local": 600, "duration_ref": None},
        {"duration_local": 600, "duration_ref": 0},
        {"duration_local": "bad", "duration_ref": 130},
        {"duration_local": 7800, "duration_ref": 130},
    ]

    assert len(confidence.file_ratios(files)) == 1


def test_file_ratios_tolerates_non_dict():
    assert confidence.file_ratios(["nonsense", None]) == []


# ── 端到端：FH-27 必须被压下来 ─────────────────────────────────

def test_fh27_real_case_score_drops():
    """真实案例：服务器侧满分，本地全是短片 -> 分数必须塌掉。"""

    ratios = [(r, True) for r in
              (0.93, 0.88, 0.12, 0.78, 0.92, 0.91, 0.85, 0.90, 0.86, 0.80)]

    res = confidence.score(
        correct=15, comments=1, matches=True, source="dictionary",
        ratios=ratios,
    )

    assert res["base"] >= 80, "base 高：服务器侧证据确实强"
    assert res["duration_known"] is True
    assert res["score"] < 30, "但总置信分必须被打下来"
    assert res["score"] < res["base"], "时长必须真的扣分"


def test_fh27_versus_healthy_title():
    """对照组：真片（时长吻合）应当远高于 FH-27 这类。

    注意 good 这里 comments=1 —— 评论权重拿不满，base 上限约 83。
    真片证据强但无人评论，83 分是合理的，不是缺陷。
    """

    bad = confidence.score(
        correct=15, comments=1, matches=True, source="dictionary",
        ratios=[(0.90, True)] * 10,
    )

    good = confidence.score(
        correct=15, comments=1, matches=True, source="dictionary",
        ratios=[(0.03, False)] * 10,
    )

    assert good["score"] >= 80, good
    assert bad["score"] < 30, bad
    assert good["score"] - bad["score"] > 50, "两者必须拉开明显差距"


def test_perfect_title_reaches_100():
    """证据满配 + 时长完全吻合 = 100。"""

    res = confidence.score(
        correct=15, comments=50, matches=True, source="std",
        ratios=[(0.0, False)] * 5,
    )

    assert res["score"] == 100


def test_near_perfect_title_is_near_100():
    """2% 偏长只扣一点点（不对称曲线：长了正常）。"""

    res = confidence.score(
        correct=15, comments=50, matches=True, source="std",
        ratios=[(0.02, False)] * 5,
    )

    assert res["score"] >= 98


def test_score_unknown_duration_not_penalized():
    """没跑过时长核对：不扣分，但必须标明未验。"""

    res = confidence.score(15, 1, True, "dictionary", ratios=None)

    assert res["duration_known"] is False
    assert res["score"] == res["base"]


def test_score_floor_prevents_wipeout():
    """时长严重不符也留底，避免 ffprobe 偶发错误把分一把清光。"""

    res = confidence.score(15, 50, True, "std", ratios=[(5.0, True)] * 5)

    assert res["score"] > 0
    assert res["score"] <= 20


def test_score_never_exceeds_100_or_goes_negative():
    """边界：任何输入都不能越界。"""

    cases = [
        (1000, 10000, True, "std", [(0, False)] * 50),
        (-5, -5, False, "junk", [(-3, True)] * 10),
        (0, None, None, None, None),
    ]

    for c, cm, m, s, r in cases:

        res = confidence.score(c, cm, m, s, r)

        assert 0 <= res["score"] <= 100, "{} 越界".format(res)


def test_score_is_deterministic():
    """同输入同输出 —— 排序要稳定，不能每次刷新换位置。"""

    args = (15, 1, True, "dictionary", [(0.9, True)] * 4)

    assert confidence.score(*args)["score"] == confidence.score(*args)["score"]


def test_score_monotonic_in_magnets():
    """其它不变时，磁力越多分越高。"""

    scores = [
        confidence.score(n, 10, True, "std", [(0.02, False)] * 3)["score"]
        for n in (1, 2, 3, 5, 10, 15)
    ]

    assert scores == sorted(scores), scores
