"""C3 番号识别引擎验收测试 —— 逐条断言任务卡的 7 行输入表 + 回归防线。

覆盖三层：
  1. 任务卡指定的 7 行断言表（**逐条独立测试**，便于复核）
  2. 捕获组缺陷的**类**回归（不是 FC2 特例）—— 任何含捕获组的规则都不得再中标
  3. 全量 26,230 语料实测暴露的假阳性/假阴性模式，用真实文件名固化为回归线
"""

import json
import os
import re

import pytest

from core.matcher_v2 import NumberMatcher, canonical, is_non_jav, strip_noise
from core.normalizer import Normalizer
from core.parser_v2 import Parser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICTIONARY = os.path.join(REPO_ROOT, "data", "dictionary.json")


@pytest.fixture(scope="module")
def parser():
    with open(DICTIONARY, encoding="utf-8") as f:
        rules = json.load(f)["rules"]
    return Parser(rules)


def numbers(parser, filename):
    return [item["number"] for item in parser.parse(filename)]


# ────────────────────────────────── 1. 任务卡 7 行断言表（逐条）

def test_table_ssis(parser):
    """`SSIS-001.mkv` -> 含 `SSIS-001`。"""
    assert "SSIS-001" in numbers(parser, "SSIS-001.mkv")


def test_table_ipx(parser):
    """`IPX-123.mp4` -> 含 `IPX-123`。"""
    assert "IPX-123" in numbers(parser, "IPX-123.mp4")


def test_table_fc2_ppv_not_empty_and_not_ppv(parser):
    """`FC2-PPV-1234567.mp4` -> 非空、非 `'PPV'`（捕获组缺陷的原始症状）。"""
    found = numbers(parser, "FC2-PPV-1234567.mp4")
    assert found != []
    assert all(n != "PPV" for n in found)


def test_table_fc2_bare_not_empty(parser):
    """`FC2-1234567.mp4` -> 非空（v2 此处返回 `''`）。"""
    found = numbers(parser, "FC2-1234567.mp4")
    assert found != []
    assert all(n != "" for n in found)


def test_table_miaa(parser):
    """`MIAA-123.mp4` -> 含 `MIAA-123`（v2 字典无 MIAA 规则，直接漏）。"""
    assert "MIAA-123" in numbers(parser, "MIAA-123.mp4")


def test_table_bare_numeric(parser):
    """`2728927.mp4` -> 非空（无厂牌数字番号）。"""
    assert numbers(parser, "2728927.mp4") != []


def test_table_random_name_is_empty(parser):
    """`随机视频名字.mp4` -> 空列表（不得乱抓）。"""
    assert parser.parse("随机视频名字.mp4") == []


# ────────────────────────────────── 2. 捕获组缺陷的「类」回归

def test_capture_group_rule_never_leaks_group_content():
    """任务卡根因：pattern 含捕获组时 `re.findall` 只返回组内容。

    用一条**人为**含捕获组的规则验证 —— 说明这是类缺陷修复不是 FC2 特例。
    """

    rules = [{
        "code": "FAKE",
        "pattern": "FAKE[-_ ]?(XXX)?[-_ ]?\\d{3,6}",
        "priority": 100,
    }]
    dialect = Parser(rules)

    # 老实现会得到 'XXX' / ''；现在必须是整段匹配
    assert numbers(dialect, "FAKE-XXX-123456.mkv") == ["FAKE-XXX-123456"]
    assert numbers(dialect, "FAKE-123456.mkv") == ["FAKE-123456"]


def test_no_candidate_is_empty_or_whitespace(parser):
    """任何输入都不许产出空串/纯空白番号（v2 的 `FC2-1234567` -> `''` 就是这类）。"""

    for name in ["FC2-1234567.mp4", "FC2-PPV-1234567.mp4", "SSIS-001.mkv",
                 "ABP 001.avi", "MIDE-789.mkv"]:
        for item in parser.parse(name):
            assert item["number"] == item["number"].strip()
            assert item["number"] != ""


# ────────────────────────────────── 3. 返回契约（下游 C1/C4 依赖）

def test_return_shape_is_list_of_number_confidence_source(parser):
    """`Parser.parse()` 返回 `[{number, confidence, source}]` —— 形态不得变。"""

    result = parser.parse("SSIS-001.mkv")

    assert isinstance(result, list)
    assert len(result) >= 1

    for item in result:
        assert set(item) == {"number", "confidence", "source"}
        assert isinstance(item["number"], str)
        # confidence 必须是 int：D2 靠「取最大」选入库番号，
        # 字符串刻度（'high'/'medium'/'low'）的字典序与语义相反，会坑 D2。
        assert isinstance(item["confidence"], int)
        assert isinstance(item["source"], str)


def test_confidence_orders_known_studio_above_generic(parser):
    """已知厂牌（字典命中）置信度必须高于通用兜底，D2 才选得对。"""

    known = parser.parse("SSIS-001.mkv")[0]["confidence"]
    generic = parser.parse("ZZZ-123.mp4")[0]["confidence"]

    assert known > generic


def test_multiple_candidates_are_sorted_desc_by_confidence(parser):
    """多候选时按置信度降序 —— 下游取 [0] 即最高。"""

    found = parser.parse("15 91制片厂91CM-101-朋友的妹妹-杨柳主演.mp4")
    confs = [item["confidence"] for item in found]

    assert confs == sorted(confs, reverse=True)


def test_padding_is_preserved(parser):
    """零填充保留：`SSIS-001` 不得被压成 `SSIS-1`（源实现的 lstrip('0') 有意不移植）。"""

    assert numbers(parser, "SSIS-001.mkv") == ["SSIS-001"]


# ────────────────────────────────── 4. FC2 双形态最终规则

def test_fc2_ppv_canonical_form(parser):
    """`FC2-PPV-{n}` 归一到规范形。"""
    assert numbers(parser, "FC2-PPV-1234567.mp4") == ["FC2-PPV-1234567"]


def test_fc2_bare_form_stays_bare(parser):
    """`FC2-{n}` 保持裸数字形，**不补 PPV**（补了会和规范形撞成同一个号）。"""
    assert numbers(parser, "FC2-1234567.mp4") == ["FC2-1234567"]


@pytest.mark.parametrize("variant", [
    "FC2PPV-1234567.mp4",
    "FC2_PPV_1234567.mp4",
    "FC2-PPV_1234567.mp4",
    "fc2-ppv-1234567.mp4",
])
def test_fc2_prefix_variants_all_normalize(parser, variant):
    """`FC2PPV-` / `FC2_PPV_` / 小写 等写法一律归一到 `FC2-PPV-{n}`。"""
    assert numbers(parser, variant) == ["FC2-PPV-1234567"]


def test_fc2_forms_are_distinct(parser):
    """两种形态是**不同**番号，不得互相归并。"""
    assert numbers(parser, "FC2-PPV-1234567.mp4") != numbers(parser, "FC2-1234567.mp4")


def test_canonical_helper():
    assert canonical("ssis_001") == "SSIS-001"
    assert canonical("FC2PPV-1234567") == "FC2-PPV-1234567"
    assert canonical("FC2_PPV_1234567") == "FC2-PPV-1234567"
    assert canonical("FC2-1234567") == "FC2-1234567"
    assert canonical("ABP  001") == "ABP-001"
    assert canonical("-SSIS-001-") == "SSIS-001"


# ────────────────────────────────── 5. 常用厂牌覆盖（任务卡要求补的规则）

@pytest.mark.parametrize("filename,expected", [
    ("MIAA-123.mp4", "MIAA-123"),
    ("MIAE-123.mp4", "MIAE-123"),
    ("MIRD-234.mp4", "MIRD-234"),
    ("MIDV-123.mp4", "MIDV-123"),
    ("MIGD-123.mp4", "MIGD-123"),
    ("MIMK-123.mp4", "MIMK-123"),
    ("FSDSS-123.mp4", "FSDSS-123"),
    ("IPZZ-266.mp4", "IPZZ-266"),
    ("DLDSS-123.mp4", "DLDSS-123"),
    ("SSNI-123.mp4", "SSNI-123"),
    ("STARS-456.mp4", "STARS-456"),
    ("PRED-123.mp4", "PRED-123"),
    ("CAWD-123.mp4", "CAWD-123"),
    ("SONE-123.mp4", "SONE-123"),
    ("HUNTC-123.mp4", "HUNTC-123"),
    ("NHDTB-123.mp4", "NHDTB-123"),
])
def test_dictionary_covers_common_studios(parser, filename, expected):
    assert expected in numbers(parser, filename)


def test_generic_fallback_catches_unknown_studio(parser):
    """未收录厂牌走通用兜底 —— 任务卡描述的 `ABC-123`。"""
    assert "ABC-123" in numbers(parser, "ABC-123.mp4")


def test_number_prefixed_studio(parser):
    """数字前缀形态：`300MAAN-403` / `91CM-101`。"""
    assert "MAAN-403" in numbers(parser, "300MAAN-403.mp4")
    assert "CM-101" in numbers(parser, "15 91制片厂91CM-101-朋友的妹妹-杨柳主演.mp4")


def test_alnum_prefixed_studio(parser):
    """字母+数字前缀：`T28-571`。"""
    assert "T28-571" in numbers(parser, "T28-571.mp4")


@pytest.mark.parametrize("filename,expected", [
    # 118 站点的 5 位零填充是站点 ID 编码，须还原成番号本体数字。
    # 三重实测佐证：三个文件所在目录名 + JavDB 收录形态都是去零后的结果。
    ("118abp00171hhb_000^WM.mp4", "ABP-171"),          # 目录 ABP-171\
    ("[NoDRM]-118abp00108hhb.wmv", "ABP-108"),        # 目录 [HD]ABP-108\
    ("118ppt00016hhb1.mkv", "PPT-016"),                # 目录 PPT-016\
    # >=1000 原样（不得被补成 5 位）
    ("118abp01234hhb.mp4", "ABP-1234"),
])
def test_118_site_padding_is_site_id_not_number(parser, filename, expected):
    assert expected in numbers(parser, filename)


@pytest.mark.parametrize("filename", [
    "118abp00171hhb_000^WM.mp4",
    "118ppt00016hhb1.mkv",
    "[NoDRM]-118abp00108hhb.wmv",
])
def test_118_does_not_emit_synonym_key(parser, filename):
    """118 站点 token 只能产出**一个**番号键。

    `118abp00171hhb` 里的 `118`+字母+数字会被 P_NUMPFX 再匹配一次，产出
    同义异形键（ABP-171 vs ABP-00171）→ 同一部片两条记录 → 去重失效。
    修 118 零填充之前两条规则恰好都吐同一个键（靠巧合去重），修完才显形。
    """

    found = numbers(parser, filename)

    assert len(found) == 1, f"应只产出一个键，实际 {found}"


# ────────────────────────────────── 6. 噪音剥除（真实语料回归线）

@pytest.mark.parametrize("filename,expected", [
    # 站名 / uncensored / 分辨率 混杂
    ("SSIS-001-uncensored-nyap2p.com.mp4", "SSIS-001"),
    ("IPX-123-C.c-CDI.uncensored.leak.mp4", "IPX-123"),
    ("TSDS-46026.HD720p-www.52iv.net.mkv", "TSDS-46026"),
    ("[ThZu.Cc]果冻传媒91CM-190少女的悔悟-潘甜甜.mp4", "CM-190"),
    # 括号内是番号 -> 保留
    ("[SSIS-001] 标题.mp4", "SSIS-001"),
    ("【IPX-123】标题.mp4", "IPX-123"),
])
def test_noise_stripping_keeps_the_number(parser, filename, expected):
    assert expected in numbers(parser, filename)


def test_cjk_adjacent_number_is_matched(parser):
    """番号紧贴中文时不得漏 —— 源实现的 `\\b` 在 CJK 上会整条漏掉。"""

    found = numbers(
        parser,
        "【2048论坛@fun2048.com - 個人撮影】FC2PPV-1066192黑暗帝国S1女优大人气"
        "美少女音梓为钱当素人拍片无套内射流出.mp4",
    )
    assert "FC2-PPV-1066192" in found


@pytest.mark.parametrize("filename,expected", [
    # `_HD_` 被剥后 `ROSA--00` 连续分隔符不得失配
    ("Rosa_HD_00.webm", "ROSA-00"),
    ("h_mast_ch_001.webm", "MAST-001"),
    ("h_suck_ch_001.webm", "SUCK-001"),
    # 番号与「像西文点分名」的噪音同现时不得整条丢弃
    ("IPX-123-C.c-CDI.uncensored.leak.mp4", "IPX-123"),
    # 罗马数字判定不得吞掉真番号（MIDV 的 M/I/D/V 全在 [IVXLCDM] 里）
    ("MIDV-123.mp4", "MIDV-123"),
])
def test_non_jav_guards_do_not_swallow_real_numbers(parser, filename, expected):
    assert expected in numbers(parser, filename)


# ────────────────────────────────── 7. 假阳性防线（全量语料实测出来的模式）

@pytest.mark.parametrize("filename", [
    # 哈希/缓存名：不得从字母数字串中间起匹配
    "C10IDLEA10.webm",
    "C10IDLEA11.webm",
    "home_d30inori08a.webm",
    "park_d28keiko13b.webm",
    "2048.cc-591 - 0gz740xhkob56cku4nbvn_source.mp4",
    "2048.cc-896 - 0h64kyq4ce18lw4252gqj_source.mp4",
    # 英文常用词 + 数字
    "Strapless Dildo - 179 jia lissa gets strapon from merry pie.mp4",
    # 站名剥落后的残片
    "X2Twitter.com_51ZHJJACeekRiLEd_1280p.mp4",
    "sjhs03.com_0002.mp4",
    # 十六进制 / 日期 / 分辨率数字
    "paint-bucket-tool-378892d3.webm",
    "20250629-003709-286.mp4",
    "HiHSP2202013 (1).mp4",
    "220614_2101_1080P_8000K_409762921.mp4",
    "【每日更新1024dz.com】A_Good_Reason_-_1920low.mp4",
])
def test_false_positive_guards(parser, filename):
    """这些真实文件名**不得**产出番号（否则会污染库、白跑 JavDB）。"""
    assert parser.parse(filename) == []


@pytest.mark.parametrize("filename", [
    "894916.mp4",                 # 6 位无意义编号
    "17297746.mp4",               # 8 位 ID
    "gc2048.com-户外挑战者 7-23-1 17297746.mp4",
])
def test_bare_numeric_length_guard(parser, filename):
    """纯数字只认 7 位 —— 6 位/8 位实测全是编号、日期或 ID。"""
    assert parser.parse(filename) == []


@pytest.mark.parametrize("filename,category", [
    ("pornhub.com_video_123.mp4", "platform"),
    ("maria.pie.and.mia.sports.star.mp4", "western"),
    ("IMG_1234.mp4", "camera"),
    ("VID_20260101.mp4", "camera"),
    ("12-34-56.mp4", "number"),
    ("XLVII.mp4", "roman"),
    ("MDLXII.mp4", "roman"),
])
def test_non_jav_exclusions(filename, category):
    """nonjav 八类排除，返回 `(True, 类别)`。"""
    flagged, hit = is_non_jav(filename)

    assert flagged is True
    assert hit == category


def test_non_jav_excluded_by_parser(parser):
    """非 JAV 判定生效时 parser 直接返回空列表。"""
    assert parser.parse("pornhub.com_abcdef_123456.mp4") == []


# ────────────────────────────────── 8. Normalizer 既有行为不得被破坏

def test_normalizer_behavior_unchanged():
    """任务卡第 6 条：`Normalizer.clean()` 的既有行为（全角符号/下划线/空格归一 + 去网址）。

    断言的是**实测**行为，不是期望行为 —— 尤其 `WWW.\\S+` 那条会把后面的番号一起吃掉，
    属于既有行为，本次不动（见交付「未做的事」）。
    """

    assert Normalizer.clean("ssis_001.mp4") == "SSIS-001.MP4"
    assert Normalizer.clean("ssis 001.mp4") == "SSIS-001.MP4"
    assert Normalizer.clean("ssis\uFF0D001.mp4") == "SSIS-001.MP4"      # 全角连字符
    assert Normalizer.clean("ssis\u2014001.mp4") == "SSIS-001.MP4"      # em dash
    # 既有的 `WWW.\S+` 剥离（含把后面番号一起吞掉的行为）不得被本次改动破坏
    assert Normalizer.clean("www.abc.com IPX-123.mp4") == ""


def test_parser_uses_normalizer(parser):
    """下划线 / 空格 / 全角连字符 三种写法经 Normalizer 后结果一致。"""
    for name in ("SSIS_001.mkv", "SSIS 001.mkv", "SSIS\uFF0D001.mkv"):
        assert "SSIS-001" in numbers(parser, name)


# ────────────────────────────────── 9. rules 入参兼容

def test_parser_accepts_rule_list(parser):
    """原有形态：规则列表（C1/C4 走这条）。"""
    assert parser.parse("SSIS-001.mkv")[0]["number"] == "SSIS-001"


def test_parser_accepts_dictionary_path():
    """兼容 config.yaml 的 `dictionary` 路径形态。"""
    as_path = Parser(DICTIONARY)

    assert as_path.parse("SSIS-001.mkv")[0]["number"] == "SSIS-001"


def test_parser_accepts_parsed_dict():
    with open(DICTIONARY, encoding="utf-8") as f:
        as_dict = Parser(json.load(f))

    assert as_dict.parse("SSIS-001.mkv")[0]["number"] == "SSIS-001"


def test_parser_tolerates_none_rules():
    """rules 缺失不得崩，退化成纯通用引擎。"""
    assert Parser(None).parse("SSIS-001.mkv")[0]["number"] == "SSIS-001"


# ────────────────────────────────── 10. 幂等 / 稳定性（任务卡要求「稳定可复现」）

@pytest.mark.parametrize("filename", [
    "SSIS-001.mkv", "FC2-PPV-1234567.mp4", "FC2-1234567.mp4",
    "MIAA-123.mp4", "2728927.mp4", "随机视频名字.mp4",
    "15 91制片厂91CM-101-朋友的妹妹-杨柳主演.mp4",
])
def test_parse_is_deterministic(parser, filename):
    """同一输入重复解析结果必须完全一致。"""
    assert parser.parse(filename) == parser.parse(filename)


def test_match_is_stateless_across_calls(parser):
    """跨调用不串状态：连续解析不同文件互不影响。"""
    first_a = parser.parse("SSIS-001.mkv")
    parser.parse("FC2-PPV-1234567.mp4")
    parser.parse("随机视频名字.mp4")

    assert parser.parse("SSIS-001.mkv") == first_a


def test_matcher_deduplicates_same_number(parser):
    """同一番号多次命中只留一条，且保留最高置信度。"""
    found = numbers(parser, "SSIS-001_SSIS-001.mkv")

    assert found.count("SSIS-001") == 1


def test_empty_input(parser):
    assert parser.parse("") == []


def test_matcher_tolerates_none():
    """NumberMatcher 层对 None 宽容（Parser 层交给 Normalizer，行为不变）。"""
    assert NumberMatcher([]).match(None) == []


def test_matcher_rules_can_be_empty():
    assert NumberMatcher([]).match("SSIS-001.mkv")[0]["number"] == "SSIS-001"


@pytest.mark.parametrize("name", [
    "random_video.mp4", "holiday_trip_2026.mp4", "screen_recording.mp4",
])
def test_plain_names_stay_empty(parser, name):
    assert parser.parse(name) == []


# ────────────────────────────────── 11. 字典完整性

def test_dictionary_rules_are_wellformed():
    """每条规则必须有能用的 regex、有 priority、有 code；pattern 编译得过。"""

    with open(DICTIONARY, encoding="utf-8") as f:
        rules = json.load(f)["rules"]

    assert len(rules) > 13, "任务卡要求补充常见厂牌，规则数应多于原有 13 条"

    codes = set()

    for rule in rules:
        assert "code" in rule
        assert isinstance(rule.get("priority"), int)
        assert re.compile(rule["pattern"])
        codes.add(rule["code"])

    # 任务卡点名要求覆盖的厂牌
    for code in ("MIAA", "MIRD", "FC2"):
        assert code in codes


def test_dictionary_fc2_rule_has_no_capture_group_exposure():
    """FC2 规则必须同时覆盖两种形态（正则里的 `|` 两支都要能过）。"""

    with open(DICTIONARY, encoding="utf-8") as f:
        rules = json.load(f)["rules"]

    fc2 = [r for r in rules if r["code"] == "FC2"]
    assert len(fc2) == 1

    pattern = fc2[0]["pattern"]
    assert re.search(pattern, "FC2-PPV-1234567")
    assert re.search(pattern, "FC2-1234567")


def test_dictionary_priority_is_int_and_bounded():
    with open(DICTIONARY, encoding="utf-8") as f:
        rules = json.load(f)["rules"]

    for rule in rules:
        assert 0 < rule["priority"] <= 100


# ────────────────────────────────── 12. 真实语料抽样子集（回归线）

REAL_CORPUS_SAMPLE = [
    # (文件名, 期望命中的番号或 None)
    ("SSIS-001.mkv", "SSIS-001"),
    ("IPX-123.mp4", "IPX-123"),
    ("MIAA-123.mp4", "MIAA-123"),
    ("ABP-001.mp4", "ABP-001"),
    ("STARS-456.mp4", "STARS-456"),
    ("MIDE-789.mp4", "MIDE-789"),
    ("FC2-PPV-1234567.mp4", "FC2-PPV-1234567"),
    ("FC2-1234567.mp4", "FC2-1234567"),
    ("FC2PPV-1066192.mp4", "FC2-PPV-1066192"),
    ("300MAAN-403.mp4", "MAAN-403"),
    ("T28-571.mp4", "T28-571"),
    ("2728927.mp4", "2728927"),
    ("SSIS-001-uncensored-nyap2p.com.mp4", "SSIS-001"),
    ("IPX-123-C.c-CDI.uncensored.leak.mp4", "IPX-123"),
    ("[ThZu.Cc]果冻传媒91CM-190少女的悔悟-潘甜甜.mp4", "CM-190"),
    ("【2048论坛@fun2048.com - 個人撮影】FC2PPV-1066192黑暗帝国S1女优大人气"
     "美少女音梓为钱当素人拍片无套内射流出.mp4", "FC2-PPV-1066192"),
    # 以下必须为空
    ("随机视频名字.mp4", None),
    ("holiday_trip_2026.mp4", None),
    ("C10IDLEA10.webm", None),
    ("home_d30inori08a.webm", None),
    ("2048.cc-591 - 0gz740xhkob56cku4nbvn_source.mp4", None),
    ("Strapless Dildo - 179 jia lissa gets strapon from merry pie.mp4", None),
    ("X2Twitter.com_51ZHJJACeekRiLEd_1280p.mp4", None),
    ("paint-bucket-tool-378892d3.webm", None),
    ("894916.mp4", None),
    ("17297746.mp4", None),
]


@pytest.mark.parametrize("filename,expected", REAL_CORPUS_SAMPLE)
def test_real_corpus_sample(parser, filename, expected):
    found = numbers(parser, filename)

    if expected is None:
        assert found == [], "假阳性：{}".format(found)
    else:
        assert expected in found


# ────────────────────── 13. Astra 审查核实后的修复（2026-09-23）

def test_canonical_adds_separator_to_no_sep_form():
    """无分隔形态必须补分隔符 —— 否则同一文件产出两个同义键（去重失效）。

    修复前实测：`abp001.mp4` 同时给出 `ABP-001`（P_NOSEP 路径）与
    `ABP001`（字典 group(0) 路径），8 个真实样本里 7 个有此双键。
    """

    assert canonical("ABP00171") == "ABP-00171"
    assert canonical("ABP933") == "ABP-933"
    assert canonical("STARS127") == "STARS-127"


def test_canonical_never_touches_digits():
    """补分隔符**不得改数字**（零填充是框架契约，非本函数职责）。"""

    assert canonical("SSIS-001") == "SSIS-001"
    assert canonical("SSIS-0123") == "SSIS-0123"
    assert canonical("ABP-00171") == "ABP-00171"


def test_canonical_fc2_dual_form():
    """FC2 两种写法归一，且裸数字形**不补 PPV**。"""

    assert canonical("FC2PPV1234567") == "FC2-PPV-1234567"
    assert canonical("FC2-PPV1234567") == "FC2-PPV-1234567"
    assert canonical("FC2-1234567") == "FC2-1234567"


def test_matcher_single_key_per_file_no_sep():
    """同一文件不得产出「去分隔符后相等」的两个候选（同义双键）。"""

    for name in ("abp001.mp4", "jul00634_dmb_w.mp4", "mifd009.mp4",
                 "ABP933mp4.mp4", "STARS127mp4.mp4"):
        found = NumberMatcher(_rules()).match(name)
        stripped = [c["number"].replace("-", "") for c in found]

        assert len(stripped) == len(set(stripped)), \
            "同义双键：{} -> {}".format(name, [c["number"] for c in found])


def test_unique_returns_confidence_descending():
    """`match()` 承诺按置信度降序 —— 与 `best_number()` 的 max() 语义对齐。"""

    found = NumberMatcher([]).match("ZZZ-123 ABP-456.mp4")
    scores = [c["confidence"] for c in found]

    assert scores == sorted(scores, reverse=True)
    assert scores, "夹具本身必须命中，否则测试空转"


def test_nosep_fallback_respects_year_filter():
    """P_NOSEP 兜底不得把年份形数字放进来（实测 3 条 `VDAY-2019` 假阳性）。"""

    found = NumberMatcher([]).match("Vday2019 Russian Babe Gets Orgasm.mp4")

    assert found == [], "年份形假阳性：{}".format(found)


def test_dictionary_left_boundary_blocks_inner_match():
    """字典规则不得从字母数字串中间起匹配（`XABP123` -> 假番号 `ABP-123`）。

    注意：**只挡左边界**。右边界保持宽松，C1b 自愈链路依赖
    「规则写窄 -> 截断」可复现（`ABP\\d{3,6}` 吃 `ABP1234567` 得 `ABP-123456`）。

    引擎层（P_STD）仍会把 `XABP-123` 识别为未知厂牌 `XABP` —— 那是另一条
    路径的正确行为，本测试只断言**字典路径**不吐 `ABP-123`。
    """

    rules = [{"code": "ABP", "aliases": [], "pattern": r"ABP[-_ ]?\d{3,6}",
              "priority": 100}]

    found = NumberMatcher(rules).match("XABP123.mp4")
    dictionary_hits = [c["number"] for c in found if c["source"] == "dictionary"]

    assert dictionary_hits == [], "字典从串中间取号：{}".format(dictionary_hits)
    assert "ABP-123" not in [c["number"] for c in found]

    # 右边界不挡：截断语义保留
    assert NumberMatcher(rules).match("ABP1234567.mp4")[0]["number"] == "ABP-123456"


def _rules():
    with open(DICTIONARY, encoding="utf-8") as f:
        return json.load(f)["rules"]
