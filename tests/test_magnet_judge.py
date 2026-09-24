# -*- coding: utf-8 -*-
"""正确番号磁力判定器 + D9 分档 + D11 双条件 测试。

样本全部来自 library.db 的**真实磁力名**（252 部 / 3848 条），不是编的。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.magnet_judge import (                              # noqa: E402
    COMMENTS_FOR_TOP,
    MAGNETS_FOR_HIGH,
    MAGNETS_FOR_STRONG,
    candidates,
    deletable,
    is_batch_deletable,
    is_correct_magnet,
    is_evidence_strong,
    is_trusted_recognition,
    judge,
    manual_only,
    normalize_number,
    tier_of,
)


# ─────────────────────── 归一化

def test_normalize_strips_leading_zeros():
    """实测：`blor00156` 这类站点补零写法要和 `BLOR-156` 视为同一个番号。"""

    assert normalize_number("BLOR-00156") == normalize_number("BLOR-156")
    assert normalize_number("SDMF-00016") == normalize_number("SDMF-16")
    assert normalize_number("FSDSS-00194") == normalize_number("FSDSS-194")
    assert normalize_number("GVH-00135") == normalize_number("GVH-135")


def test_normalize_strips_zeros_on_digit_bearing_prefix():
    """前缀自带数字的厂牌（T28 / 300MAAN）同样要去前导零。

    这是自查抓到的真 bug：原正则头段只允许 `[A-Z]{1,8}`，导致
    `T28-0571` 与 `T28-571` 归一化后**不相等**，同一批种子会被拆成两个番号。
    """

    assert normalize_number("T28-0571") == normalize_number("T28-571")
    assert normalize_number("300MAAN-0403") == normalize_number("300MAAN-403")


def test_normalize_rejects_pure_digits():
    """头段必须至少含一个字母 —— 别把纯数字串当番号。"""

    assert normalize_number("123-456") == "123-456"      # 原样返回，不做去零
    assert normalize_number("0012-34") != normalize_number("12-34")


def test_normalize_case_and_separator():
    assert normalize_number("abp-41") == "ABP-41"
    assert normalize_number("ABP_171") == "ABP-171"
    assert normalize_number("ABP 171") == "ABP-171"
    assert normalize_number("ABP--171") == "ABP-171"


def test_normalize_fc2_forms_unify():
    """ROADMAP：`FC2-PPV-1234567` 和 `FC2-1234567` 是同一个番号。"""

    assert normalize_number("FC2-PPV-1234567") == normalize_number("FC2-1234567")
    assert normalize_number("FC2PPV-1234567") == normalize_number("FC2-1234567")


def test_normalize_keeps_distinct_numbers_distinct():
    """归一化**不能**把不同番号压成一个 —— 这是最危险的方向。"""

    assert normalize_number("ABP-454") != normalize_number("ABP-4540")
    assert normalize_number("ABP-45") != normalize_number("ABP-454")
    assert normalize_number("ABP-171") != normalize_number("IPX-171")


def test_normalize_empty():
    assert normalize_number("") == ""
    assert normalize_number(None) == ""


# ─────────────────────── 边界安全（防误判，最要紧）

def test_longer_number_is_not_a_match():
    """`ABP-4540` 不是 `ABP-454` —— 只比前缀就会错。"""

    assert not is_correct_magnet("ABP-4540.mp4", "ABP-454")


def test_prefixed_number_is_not_a_match():
    """`XABP-454` 不是 `ABP-454`。"""

    assert not is_correct_magnet("XABP-454.mp4", "ABP-454")


def test_different_studio_not_a_match():
    assert not is_correct_magnet("IPX-454.mp4", "ABP-454")


# ─────────────────────── ROADMAP 指定的样本名（必须全部认出）

@pytest.mark.parametrize("name", [
    "ABP-454-UC.torrent.非同厂版本",
    "[FHD]abp-454.mp4",
    "0320-abp-454-FHD",
    "第一會所新片@SIS001@(PRESTIGE)(ABP-454)xxx",
    "ABP-454-C.torrent",
    "ABP-454-UC.非同厂版本.torrent",
    "[HD]ABP-454.mp4",
])
def test_roadmap_sample_names_recognized(name):
    """ROADMAP 第 1 节点名要求判定器能认出这些形态。"""

    assert is_correct_magnet(name, "ABP-454"), name


# ─────────────────────── 兜底：三类会让 matcher 整条丢弃的形态

def test_release_index_prefix():
    """`09.abp-041` —— 实测 26 条。命中 PURE_NUM 会被整条丢弃。"""

    assert is_correct_magnet("09.abp-041", "ABP-041")
    assert is_correct_magnet("31.abp-145", "ABP-145")


def test_site_domain_tail():
    """`ipz-319-gojav.net.mp4` —— 域名+扩展名像三段点分名，会被当西方人名丢弃。"""

    assert is_correct_magnet("ipz-319-gojav.net.mp4", "IPZ-319")


def test_combined_fallback_paths():
    """需要**同时**剥前导数字与去 CJK 的形态（兜底链最后一条）。"""

    assert is_correct_magnet(
        "1ABP861 title-a 08",
        "ABP-861",
    )


def test_no_fabricated_number_from_cjk_removal():
    """**去 CJK 不能缝合造码** —— 这是最危险的方向。

    专家审查 + 实测发现：`ABP女454` 去掉中间的 `女` 后左右拼成 `ABP 454`，
    就会判出 `ABP-454`。而 `ABP-454` 是真实且磁力很多的热门番号 ——
    凭空造出来的匹配会把它推进批量删。

    守法是 `_contiguous_in`：候选必须**原样连续**出现在原串里。
    原串 `ABP女454` 里 `ABP454` 不连续 -> 拒绝。
    """

    assert not is_correct_magnet("ABP女454.mp4", "ABP-454")
    assert not is_correct_magnet("ABP女454", "ABP-454")
    assert not is_correct_magnet("IPXあ123.mp4", "IPX-123")


def test_cjk_titles_still_recognized_after_the_guard():
    """堵造码的同时**不能丢掉**带中日文标题的召回（实测 22 条）。

    这些串里番号是连续的（`ABP861` 挨着），只是后面跟着 CJK 标题。
    """

    assert is_correct_magnet(
        "ABP861 title-a 08 title-b",
        "ABP-861",
    )
    assert is_correct_magnet("IPX292 巨乳若妻は元彼ダメ男に嫌なほどイカされて", "IPX-292")
    assert is_correct_magnet("HND737 男の子みたいな女の子は男の子とのイチャラブ", "HND-737")


def test_contiguity_guard_is_used_only_on_fallback():
    """主路径不受守卫影响 —— 否则会把主路径本来认得的形态一起挡掉。"""

    from core.magnet_judge import candidates

    # 主路径能提出候选时，直接返回，不过守卫
    main = candidates("ABP-454-UC.torrent.非同厂版本", with_fallback=False)

    assert "ABP-454" in main


def test_no_separator_with_leading_digit():
    """`1stars-248-c.mp4` —— 前缀带数字，形态守卫挡掉。"""

    assert is_correct_magnet("1stars-248-c.mp4", "STARS-248")


def test_fallback_does_not_loosen_boundary():
    """兜底路径**不能**放松边界 —— 剥噪音后仍要区分 454 / 4540。"""

    assert not is_correct_magnet("09.abp-4540", "ABP-454")


def test_cjk_heavy_name_still_recognized():
    """中日文长标题的磁力名（实测 22 条被判 amateur 丢弃）。"""

    assert is_correct_magnet(
        "ABP861 title-a 08 title-b",
        "ABP-861",
    )
    assert is_correct_magnet("IPX292 巨乳若妻は元彼ダメ男に嫌なほどイカされて", "IPX-292")


# ─────────────────────── 去重与统计

def test_judge_dedupes_by_hash():
    """同一条种子被反复贴，只能算一条。"""

    mags = [
        {"name": "ABP-454.mp4", "hash": "aaa"},
        {"name": "ABP-454.mp4", "hash": "aaa"},      # 同 hash，重复
        {"name": "[FHD]abp-454.mp4", "hash": "bbb"},
    ]

    res = judge(mags, "ABP-454")

    assert res["total"] == 2, "同 hash 应去重"
    assert res["correct"] == 2


def test_judge_counts_only_correct():
    """混着别的番号的磁力，只数正确的。"""

    mags = [
        {"name": "ABP-454.mp4", "hash": "a"},
        {"name": "IPX-999.mp4", "hash": "b"},        # 别的番号
        {"name": "ABP-4540.mp4", "hash": "c"},       # 边界
        {"name": "ABP-454-C.torrent", "hash": "d"},
    ]

    res = judge(mags, "ABP-454")

    assert res["total"] == 4
    assert res["correct"] == 2


def test_digit_prefix_studio_both_forms_match():
    """数字前缀厂牌：目标与磁力名形态不同时也必须算。

    这是自查抓到的第四个 bug：`300MAAN-403` 在磁力名里会被 matcher 的
    `P_NUMPFX` 归一成 `MAAN-403`，而目标番号是 `300MAAN-403` —— 只比一种
    形态会把这类番号的磁力**全部漏掉**（反之亦然）。
    修法：目标番号自己也走一遍同一条提取管线，取两种形态的并集。
    """

    assert is_correct_magnet("300MAAN-403.mp4", "300MAAN-403")
    assert is_correct_magnet("MAAN-403.mp4", "300MAAN-403")
    assert is_correct_magnet("261ARA-462.mp4", "261ARA-462")


def test_digit_prefix_fix_did_not_loosen_boundary():
    """修上面的 bug 时**不能**顺带放松边界 —— 这是最危险的回归。"""

    assert not is_correct_magnet("ABP-4540.mp4", "ABP-454")
    assert not is_correct_magnet("XABP-454.mp4", "ABP-454")
    assert not is_correct_magnet("300MAAN-4030.mp4", "300MAAN-403")
    assert not is_correct_magnet("MAAN-4031.mp4", "300MAAN-403")


def test_target_keys_are_minimal_for_plain_numbers():
    """普通番号不该被展开出多余形态（否则匹配面会被无谓放大）。"""

    from core.magnet_judge import _target_keys

    assert _target_keys("ABP-454") == {"ABP-454"}
    assert _target_keys("T28-571") == {"T28-571"}


def test_judge_handles_empty():
    res = judge([], "ABP-454")

    assert res["total"] == 0
    assert res["correct"] == 0


def test_judge_handles_missing_name():
    """javdb 偶尔返回没名字的条目，不能崩。"""

    res = judge([{"hash": "x"}, {"name": None, "hash": "y"}], "ABP-454")

    assert res["correct"] == 0


# ─────────────────────── D9 分档

def test_tier_boundaries():
    """四档边界：0 / 1-2 / >=3 / >=3+评论。"""

    assert tier_of(0) == "极低"
    assert tier_of(1) == "低"
    assert tier_of(2) == "低"
    assert tier_of(3) == "高"
    assert tier_of(9) == "高"
    assert tier_of(100) == "高"


def test_tier_top_requires_comments():
    """「极高」= 磁力够 **且** 评论够。"""

    assert tier_of(3, comments=COMMENTS_FOR_TOP) == "极高"
    assert tier_of(3, comments=COMMENTS_FOR_TOP - 1) == "高"
    assert tier_of(50, comments=0) == "高"
    assert tier_of(50, comments=None) == "高"


def test_tier_comments_do_not_lift_thin_magnets():
    """评论再多也不能把磁力不足的片升档 —— 两个条件都要。"""

    assert tier_of(1, comments=9999) == "低"
    assert tier_of(0, comments=9999) == "极低"


def test_tier_handles_none():
    assert tier_of(None) == "极低"


def test_tier_survives_non_numeric_comments():
    """javdb 的 `comments_count` 可能是空串/非数字 —— 不能崩。

    这是自查抓到的真 bug：原实现直接 `int(comments)`，`comments=''`
    抛 ValueError 把整个定档流程打断。
    """

    assert tier_of(5, comments="") == "高"          # 空串 -> 当没数据
    assert tier_of(5, comments="   ") == "高"
    assert tier_of(5, comments="abc") == "高"       # 非数字 -> 当没数据
    assert tier_of(5, comments="1,234") == "极高"   # 带千分位要能认
    assert tier_of(5, comments=49.9) == "高"
    assert tier_of(5, comments="0") == "高"
    assert tier_of(5, comments=0) == "高"


def test_high_threshold_is_three():
    """锁住门槛值 —— 改了要同时更新这里的分布断言。"""

    assert MAGNETS_FOR_HIGH == 3


def test_strong_threshold_is_display_only():
    """`strong` 只是展示档，**不能**混进删除门控。

    如果有人把它接到 `is_batch_deletable` 上，门槛会从 97.6% 收紧到 76.2%，
    相当于偷偷改了 D10 的删除规则 —— 这条测试守住这个边界。
    """

    assert MAGNETS_FOR_STRONG == 10

    assert is_evidence_strong(9) is False
    assert is_evidence_strong(10) is True
    assert is_evidence_strong(0) is False

    # 9 条虽然不够「强」，但档位仍 >= 高；批量门控**不看** strong，
    # 只看 D11 的三条件（这里给齐可信参数）
    assert tier_of(9) == "高"
    assert is_batch_deletable(
        "高", True, confidence=90, source="std", filepath="C:/x/ABP-171.mp4"
    ) is True


# ─────────────────────── D11 双条件

def test_batch_requires_all_three_conditions():
    """D11 三条件：档位 + number 核对 + 高置信识别/常见格式。"""

    trusted = dict(confidence=90, source="std", filepath="C:/x/ABP-171.mp4")

    assert is_batch_deletable("高", True, **trusted) is True
    assert is_batch_deletable("极高", True, **trusted) is True

    # 档位够，但 number 核对不过
    assert is_batch_deletable("高", False, **trusted) is False

    # 档位不够
    assert is_batch_deletable("低", True, **trusted) is False
    assert is_batch_deletable("极低", True, **trusted) is False

    # 第三条：识别来源不可信
    assert is_batch_deletable(
        "高", True, confidence=50, source="nosep", filepath="C:/x/ABP171.mp4"
    ) is False
    assert is_batch_deletable(
        "高", True, confidence=40, source="bare", filepath="C:/x/454.mp4"
    ) is False

    # 第三条：格式不是常见 AV 格式
    assert is_batch_deletable(
        "高", True, confidence=90, source="std", filepath="C:/x/ABP-171.webm"
    ) is False


def test_batch_requires_params_missing_means_refuse():
    """第三条的参数缺省即拒绝 —— 老数据绝不能批量删（宁可漏删）。"""

    assert is_batch_deletable("高", True) is False
    assert is_batch_deletable("高", True, source="std") is False
    assert is_batch_deletable("高", True, filepath="C:/x/ABP-171.mp4") is False


def test_trusted_sources_match_matcher_output():
    """**把白名单与 matcher 的实际输出钉在一起。**

    这里踩过一次真 bug：白名单写 `"dict"`，而 matcher 实际产出
    `"dictionary"` —— 208 个完全可信的字典命中被判成「不可信」，
    批量删候选从 280+ 部掉到 99 部。

    做法：拿一组**覆盖每种来源**的样本喂给真 matcher，收集它实际产出的
    `source`，再检查每个都落在「可信 ∪ 不可信」里。以后 matcher 新增一种
    来源而白名单没跟上，这条会红。
    """

    from core.matcher_v2 import NumberMatcher

    from core.magnet_judge import (
        TRUSTED_MATCH_SOURCES,
        UNTRUSTED_MATCH_SOURCES,
    )

    m = NumberMatcher([])

    # 每种来源各挑一个能触发它的样本
    samples = [
        "ABP-171.mp4",                  # std
        "T28-571.mp4",                  # tnum
        "300MAAN-403.mp4",              # numpfx
        "FC2-PPV-1234567.mp4",          # fc2
        "ABP171.mp4",                   # nosep
        "118abp00171hhb.mp4",           # 118
    ]

    produced = set()

    for s in samples:

        for c in m.match(s):
            produced.add(c.get("source"))

    produced.discard(None)

    # 字典来源要单独触发（走 rules，不在通用引擎里）
    dict_matcher = NumberMatcher([{"pattern": r"ZZTEST[-_ ]?\d{3}", "priority": 100}])

    for c in dict_matcher.match("ZZTEST-001.mp4"):
        produced.add(c.get("source"))

    assert produced, "样本没触发任何来源，测试本身失效了"

    known = TRUSTED_MATCH_SOURCES | UNTRUSTED_MATCH_SOURCES

    unknown = produced - known

    assert not unknown, (
        "matcher 产出了白名单里没有的来源：{}。\n"
        "可信的加进 TRUSTED_MATCH_SOURCES，不可信的加进 "
        "UNTRUSTED_MATCH_SOURCES —— 别让它悄悄漏过去。".format(sorted(unknown))
    )

    # 关键的那一个字面量：dictionary 必须被认作可信
    assert "dictionary" in TRUSTED_MATCH_SOURCES
    assert "dictionary" in produced


def test_trusted_and_untrusted_are_disjoint():
    """两个集合不能重叠 —— 否则同一种来源既是可信又不可信。"""

    from core.magnet_judge import (
        TRUSTED_MATCH_SOURCES,
        UNTRUSTED_MATCH_SOURCES,
    )

    assert not (TRUSTED_MATCH_SOURCES & UNTRUSTED_MATCH_SOURCES)


def test_dictionary_source_is_trusted():
    """字典命中必须是可信的（它是最强的来源）。"""

    assert is_trusted_recognition("dictionary") is True


def test_bare_and_nosep_are_untrusted():
    """形态弱的来源不可信。"""

    assert is_trusted_recognition("nosep") is False
    assert is_trusted_recognition("bare") is False


def test_trusted_sources_classification():
    """结构化形态匹配可信；无分隔符与裸数字不可信。"""

    assert is_trusted_recognition("dictionary") is True
    assert is_trusted_recognition("fc2") is True
    assert is_trusted_recognition("std") is True
    assert is_trusted_recognition("numpfx") is True
    assert is_trusted_recognition("tnum") is True

    assert is_trusted_recognition("nosep") is False, "无分隔符形态弱"
    assert is_trusted_recognition("bare") is False, "裸数字最容易撞"
    assert is_trusted_recognition(None) is False
    assert is_trusted_recognition("") is False


def test_low_tier_is_manual_only():
    assert manual_only("低") is True
    assert manual_only("高") is False
    assert manual_only("极低") is False


def test_very_low_cannot_be_deleted():
    """极低档不能删，只存档。"""

    assert deletable("极低") is False

    assert deletable("低") is True
    assert deletable("高") is True
    assert deletable("极高") is True


def test_batch_never_allows_very_low():
    for matches in (True, False):
        assert is_batch_deletable("极低", matches) is False


# ─────────────────────── 分布断言（门槛的取值依据）

def test_real_corpus_distribution_documented():
    """用真实语料断言分布形状，防止门槛被盲目改动。

    实测（library.db 252 部有磁力的片，按**正确**磁力条数）：

        0 条      2 部  ( 0.8%)
        1-2 条    4 部  ( 1.6%)
        3-4 条    9 部  ( 3.6%)
        5-9 条   47 部  (18.7%)
        10-19 条 146 部  (57.9%)
        20-49 条 37 部  (14.7%)
        50+ 条    7 部  ( 2.8%)

    ⚠️ **关键结论**：>=3 条就占 97.6% —— 「高」档在这个语料里几乎不筛选。
    它作为**删除闸门**（要保守）没问题，但**没有区分力**。真要用档位区分
    「强证据」得提到 10 条（75.4%）。

    这条测试在 library.db 可用时跑真实数据；不可用时跳过。
    """

    db = os.environ.get("LMM_LEGACY_LIBRARY_DB", "")

    if not os.path.exists(db):
        pytest.skip("library.db 不可用")

    import collections
    import json
    import sqlite3

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        "select number, magnets from movies where magnets is not null"
    ).fetchall()

    if not rows:
        pytest.skip("库里没有磁力数据")

    counts = []

    for r in rows:
        res = judge(json.loads(r["magnets"]) or [], r["number"])
        counts.append(res["correct"])

    total = len(counts)

    thin = sum(1 for n in counts if n <= 2)
    strong = sum(1 for n in counts if n >= MAGNETS_FOR_HIGH)

    # 薄的（极低+低）是少数 —— 这条稳
    assert thin / total < 0.10, (
        f"薄档占比 {thin / total:.1%}，与实测 2.4% 差太多，判定器可能变松了"
    )

    # >=3 覆盖绝大多数 —— 这条记录「高」档没区分力这个事实
    assert strong / total > 0.90, (
        f">=3 条只占 {strong / total:.1%}（实测 97.6%）—— "
        f"要么语料变了，要么判定器变严了，两者都要重新定门槛"
    )
