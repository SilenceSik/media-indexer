# -*- coding: utf-8 -*-
"""正确番号磁力判定器。

回答一个问题：**这条磁力是不是这个番号的？**

这是 D9 分档的判据来源（「正确磁力条数」），也是 D11 批量删的第二个条件。
判错的方向性后果不对称 —— 多算一条会让本该手动的片混进批量删，所以
**宁可少算**。

## 为什么不另写一套正则

`core/matcher_v2.py` 已经有成熟的分词与噪音剥离（站名、分辨率、技术标记、
括号、CJK 边界），并且被 26,230 条真实语料校准过。这里**复用它做提取**，
本模块只负责三件事：

1. **归一化比较** —— `BLOR-00156` 与 `BLOR-156` 是同一个番号
2. **补三条 matcher 在磁力名上会踩的坑**（每条都有实测条数，见下）
3. **比较** —— 归一化后相等才算「正确磁力」

## 三条兜底（实测来自 library.db 的 252 部 / 3848 条真实磁力名）

只处理「一个候选都提不出来」的情况，共 34 条误判：

| 形态 | 例 | 实测条数 | 根因 |
|---|---|---|---|
| 发布序号前缀 | `09.abp-041` | 26 | 命中 `PURE_NUM`，整条被当成纯数字丢弃 |
| 站点域名+扩展名像西方人名 | `ipz-319-gojav.net.mp4` | 少量 | `gojav.net.mp4` 命中 `WESTERN_NAME`（三段点分） |
| 无分隔符带前导数字 | `1stars-248-c.mp4` | 少量 | `1stars` 前缀有数字，`(?<![A-Za-z0-9])` 挡掉整个形态 |

做法是**先剥掉这些噪音再交给 matcher**，而不是自己写正则去认番号 ——
这样边界安全（`ABP-4540` ≠ `ABP-454`）仍然由 matcher 保证。

## 边界安全（由 matcher 的 `(?<![A-Za-z0-9])` + 全键比较共同保证）

- `ABP-4540` → 提取 `ABP-4540`，与 `ABP-454` **不相等** → 不算
- `XABP-454` → 提取 `XABP-454`，与 `ABP-454` **不相等** → 不算
- `ABP-454-UC` / `[FHD]abp-454.mp4` / `0320-abp-454-FHD` → 都提取 `ABP-454` → 算

关键：比较的是**含数字部分的完整键**，不是前缀。仅比前缀会让 `ABP-4540`
被算成 `ABP-454`。
"""

import re

from core.matcher_v2 import NumberMatcher                # noqa: E402

# ── 归一化 ───────────────────────────────────────────────────

# 三段以上形态：`ABC-123`、`FC2-PPV-1234567`。
# 头段允许含数字（`T28-571`、`300MAAN-403` 这类字母+数字厂牌），但必须
# **至少有一个字母** —— 否则纯数字串会被当番号。数字部分一律去前导零：
# `T28-0571` == `T28-571`（站点补零不止出现在纯字母厂牌上）。
_STD_KEY = re.compile(r"^((?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,8})(?:-([A-Z]{1,6}))?-(\d{1,8})$")

# FC2 的两种写法视为同一番号
_FC2 = re.compile(r"^FC2-?(?:PPV-?)?(\d{1,9})$")


def normalize_number(number):
    """番号 -> 可比较的归一化键。无法识别时返回原串的大写形式。

    处理：
      * 前导零：`BLOR-00156` == `BLOR-156`、`SDMF-00016` == `SDMF-16`
      * 分隔符：`_` / 空格 -> `-`
      * FC2：`FC2-PPV-1234567` == `FC2-1234567`
    """

    if not number:
        return ""

    s = str(number).strip().upper()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")

    if not s:
        return ""

    mo = _FC2.match(s)

    if mo:
        # FC2 的数字部分本身可能有前导零，同样去掉
        return "FC2-{}".format(int(mo.group(1)))

    mo = _STD_KEY.match(s)

    if mo:
        head, mid, num = mo.group(1), mo.group(2), mo.group(3)
        parts = [head]

        if mid:
            parts.append(mid)

        parts.append(str(int(num)))          # 去前导零

        return "-".join(parts)

    return s


# ── 磁力名 -> 候选（含兜底）──────────────────────────────────

# 发布序号前缀：`09.abp-041` / `31.abp-145`。
# 只吃掉**紧贴番号**的 1-2 位数字加点，不碰别的数字。
_RELEASE_IDX = re.compile(r"^\s*\d{1,2}\.\s*(?=[A-Za-z])")

# 站点域名后缀：`ipz-319-gojav.net.mp4` / `xxx.xyz.mp4`。
# `.[a-z]{2,6}` 用点分隔，要求前面是 `-`（避免误吃正常的西文点分名）。
_DOMAIN_TAIL = re.compile(
    r"(?i)-[a-z0-9]{2,20}\.(?:com|net|cc|tv|vip|org|me|xyz|club|top|info)"
    r"(?=\.(?:mp4|mkv|avi|wmv|rmvb|webm|ts|mov|flv|m4v|mpg|mpeg)$)"
)

# 站点域名 + 扩展名（无连字符）：`gojav.net.mp4`
_DOMAIN_EXT = re.compile(
    r"(?i)\b[a-z][a-z0-9]{1,19}\.(?:com|net|cc|tv|vip|org|me|xyz|club|top|info)"
    r"(?=\.(?:mp4|mkv|avi|wmv|rmvb|webm|ts|mov|flv|m4v|mpg|mpeg)$)"
)

# 开头的前导数字：`1stars-248-c.mp4` -> `stars-248-c.mp4`。
# 形态守卫 `(?<![A-Za-z0-9])` 会因为开头这个数字挡掉整个形态。
#
# ⚠️ **只用 1 位数字**。素人厂牌的数字前缀（`200GANA`、`259LUXU`、`300MIUM`、
# 390JAC`）是**番号的一部分**，剥掉会让 target 与 candidate 形态不一致。
# 实测（2026-09-24）：`200GANA-2680` 经 matcher 得 `GANA-2680`；若这里也剥，
# 加上目标侧展开，尚能对上；但剥 2-4 位会额外腐蚀 `1ABP861` 这类本可救回的形态。
# 收紧到 1 位是「救回纯噪音前导数字」与「不碰厂牌前缀」之间的最小面。
_LEAD_DIGITS = re.compile(r"^\s*\d(?=[A-Za-z])")

# CJK 串：`ABP861 美少女と、貸し切り温泉と…`
# `is_non_jav` 的 amateur 规则是「CJK >= 10 且没有严格形态番号 -> 丢弃」，
# 而磁力名天然带大段中日文标题，会被整条误杀（实测 22 条）。
# 兜底做法：把 CJK 段换成空格再重试 —— 只降 CJK 计数，不碰番号本身。
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _fallback_clean(name):
    """剥掉会让 matcher 整条丢弃的噪音，返回待重试的串。

    只在主路径**一个候选都没提出来**时使用。
    """

    s = name or ""

    s = _RELEASE_IDX.sub("", s)

    s = _DOMAIN_TAIL.sub("", s)

    s = _DOMAIN_EXT.sub("", s)

    s = _LEAD_DIGITS.sub("", s)

    return s


def _fallback_drop_cjk(name):
    """把 CJK 段换成空格。用于绕过 is_non_jav 的 amateur 规则。"""

    return _CJK_RUN.sub(" ", name or "")


_MATCHER = NumberMatcher([])


def _extract(text):
    """一次提取：matcher -> 归一化候选集合。"""

    out = {
        normalize_number(c["number"])
        for c in _MATCHER.match(text or "")
    }

    out.discard("")

    return out


# 兜底链。**顺序有意义**：先「只剥噪音、不动内容」，再「去 CJK」，
# 最后两者叠加 —— 实测存在同时需要两种处理的形态：
#   `1ABP861 美少女と、貸し切り温泉と…`（前导数字 + CJK 长标题）
#
# ⚠️ 「去 CJK」那条会**拼接造码**（2026-09-24 专家审查 + 实测发现）：
# `ABP女454.mp4` 去掉中间的 `女` 之后左右拼成 `ABP 454` -> 判定出 `ABP-454`。
# 那是凭空中造出一个番号，而 `ABP-454` 是真实且磁力很多的热门番号 ——
# 一旦某个本地文件其实不是它，这种"造出来的匹配"会把它推进批量删。
#
# 处理：不删这条兜底（删了会丢掉带中日文标题的 22 条召回），而是给所有
# 兜底结果加一道 `_contiguous_in` 守卫 —— 候选必须**原样连续**出现在原串里
# （只忽略分隔符，不忽略任何字符）。`ABP女454` 里 `ABP454` 不连续 -> 拒绝；
# `ABP861 美少女と…` 里 `ABP861` 连续 -> 放行。两头都保住。
_FALLBACKS = (
    _fallback_clean,
    _fallback_drop_cjk,
    lambda n: _fallback_drop_cjk(_fallback_clean(n)),
)


def _squash(s):
    """去掉分隔符并大写（保留 CJK，它们才是判定「是否缝合」的关键）。"""

    return re.sub(r"[\s\-_.]+", "", (s or "").upper())


def _contiguous_in(key, raw):
    """``key`` 是否**原样连续**出现在 ``raw`` 里。

    用途：兜底（尤其「去 CJK」那条）可能把被 CJK 隔断的字符拼到一起，
    凭空造出番号 —— 实测 `ABP女454` 去 CJK 后拼成 `ABP 454`，判出 `ABP-454`。
    而 `ABP-454` 是真实且磁力很多的热门番号，凭空造出的匹配会把它推进批量删。

    判据：把 key 拆成「字母头 + 数字尾」，拼成 `头0*尾` 去原串里找。
    * 只忽略**分隔符**（空格 / `-` / `_` / `.`）—— 它们不影响连续性
    * 数字尾允许前导零差异（`ABP-41` 与 `ABP-041` 是同一个号，
      这是 normalize 的合法归一化，不是造码）
    * **任何被删掉的字符都会破坏连续性** —— `ABP女454` 里的 `女` 不是分隔符，
      所以 `ABP454` 在那里找不到
    """

    haystack = _squash(raw)

    m = re.match(r"^(.*?)(\d+)$", key or "")

    if not m:
        pattern = re.escape(_squash(key))
    else:
        head, digits = m.group(1), m.group(2)
        pattern = re.escape(_squash(head)) + r"0*" + re.escape(digits)

    if not pattern:
        return False

    return re.search(pattern, haystack) is not None


def _filter_contiguous(keys, raw):
    """兜底产出过滤：候选必须能在原串里连续找到。"""

    return {k for k in keys if _contiguous_in(k, raw)}


def candidates(name, with_fallback=True):
    """磁力名 -> 归一化候选番号集合。

    主路径走 matcher；提不出任何候选时，按 `_FALLBACKS` 顺序重试。
    兜底产出额外过一道「连续出现」守卫，防止去 CJK 时缝合造码。
    """

    text = name or ""

    out = _extract(text)

    if out or not with_fallback:
        return out

    for fn in _FALLBACKS:

        cleaned = fn(text)

        if cleaned == text:
            continue

        # 守卫必须在**原始串**上判，不能在被清洗过的串上判 ——
        # 否则 `09.abp-041` 会被误伤：清洗后 `abp-041` 自然连续，
        # 但它之所以需要兜底，正是因为 `09.` 挡了主路径。
        got = _filter_contiguous(_extract(cleaned), text)

        if got:
            return got

    return out


def _target_keys(target):
    """目标番号的可接受键集合。

    关键：**目标番号自己也要走一遍同一条提取管线**。

    原因（自查抓到的真 bug）：`300MAAN-403` 这类「数字前缀厂牌」番号，
    matcher 在磁力名里会归一成 `MAAN-403`（`P_NUMPFX` 剥数字前缀）。
    如果只比 `normalize_number(target)` 一种形态，`300MAAN-403` 作为目标时
    会把所有写着 `300MAAN-403` 的磁力全部漏掉（反向也一样）。

    同理 `261ARA-462` 在 JavDB 上以 `ARA-462` 实存 —— 两种形态都要收。
    """

    keys = set()

    n = normalize_number(target)

    if n:
        keys.add(n)

    for k in candidates(target):
        if k:
            keys.add(k)

    return keys


def is_correct_magnet(name, target):
    """这条磁力是不是 ``target`` 这个番号的。"""

    keys = _target_keys(target)

    if not keys:
        return False

    return bool(keys & candidates(name))


def judge(magnets, target):
    """给一批磁力打 ``is_correct``，并返回统计。

    ``magnets`` 是 javdb 返回的条目列表（含 ``name`` / ``hash``）。
    按 ``hash`` 去重后再计数 —— javdb 上同一条种子会被反复贴。
    """

    keys = _target_keys(target)

    seen_hash = set()

    rows = []

    for x in magnets or []:

        name = (x or {}).get("name") or ""

        h = ((x or {}).get("hash") or "").strip().lower()

        # 无 hash 时用名字去重（少见，但不至于漏计）
        dedup = h or ("name:" + name)

        if dedup in seen_hash:
            continue

        seen_hash.add(dedup)

        ok = bool(keys) and bool(keys & candidates(name))

        rows.append({
            "name": name,
            "hash": h,
            "is_correct": ok,
        })

    correct = sum(1 for r in rows if r["is_correct"])

    return {
        "number": target,
        "key": normalize_number(target),
        "keys": sorted(keys),
        "total": len(rows),
        "correct": correct,
        "magnets": rows,
    }


# ── 分档（D9）─────────────────────────────────────────────────

# ── 档位门槛（主人 2026-09-26 拍板口径）────────────────────────
#
#   极高 = 磁力 **> 3 条** 且 评论 **> 10 条**   （两个条件都要）
#   高   = 磁力 **> 5 条**                        （不看评论）
#   低   = 磁力 1~5 条，且不满足「极高」
#   极低 = 0 条
#
# ⚠️ **是严格大于，不是大于等于**。主人原话：「磁链大于3条 评论大于十条
#    是极高分，磁链大于5条算高分」——所以 4 条磁力算「>3」，5 条不算「>5」。
#
# 两条设计要点：
#
# 1. **评论不是硬性门槛**（主人 2026-09-26 明确）。它只在「磁力已经够」
#    的前提下用来升档（高 → 极高）。评论再多也不能把磁力不足的片抬进
#    一键删——`is_extreme()` 两个条件是与关系。
#
# 2. 旧口径是「高 = 磁力 >= 3」，实测覆盖 **98%** —— 作为闸门几乎没有
#    区分力（见下面保留的分布数据）。新口径把闸门抬到磁力 > 5，
#    并把「磁力 > 3 且评论 > 10」作为另一条进门路径。
#
# 保留的实测分布（2026-09-23，library.db，判定器正确率 99.7%）：
#     0 条      2 部 ( 0.8%)      10-19 条 148 部 (58.7%)
#     1-2 条    4 部 ( 1.6%)      20-49 条  37 部 (14.7%)
#     3-4 条    7 部 ( 2.8%)      50+ 条    7 部 ( 2.8%)
#     5-9 条   47 部 (18.7%)
# 关键：>=3 条覆盖 97.6%、>=6 条覆盖约 79% —— 后者才有筛选意义。
COMMENTS_FOR_TOP = 10       # 极高要求「评论 > 10」
MAGNETS_FOR_TOP = 3         # 极高要求「磁力 > 3」
MAGNETS_FOR_HIGH = 5        # 高要求「磁力 > 5」

# 展示档：磁力 >= 10 条 = 证据强。**仅展示/排序，不做门控。**
MAGNETS_FOR_STRONG = 10


def _as_int(value, default=None):
    """把可能是字符串/空串/None 的计数安全转成 int。

    javdb 返回的 `comments_count` 有时是空串或非数字 —— 直接 `int()`
    会抛 ValueError 把整个定档流程打断（实测 `comments=''` 即崩）。
    """

    if value is None or value is False:
        return default

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, (int, float)):
        return int(value)

    s = str(value).strip().replace(",", "")

    if not s:
        return default

    try:
        return int(s)
    except ValueError:
        return default


def is_evidence_strong(correct):
    """证据是否算「强」（>=10 条正确磁力）。

    **只用于展示与排序，不参与删除门控。** 删除门控用 `is_batch_deletable`。

    存在的理由：`>=3` 覆盖 97.6% 的片，界面上一片全是「高」等于没信息。
    这一档给用户一个「真正证据充分」的标签（覆盖 76.2%），
    让列表能排序、能筛。
    """

    return int(correct or 0) >= MAGNETS_FOR_STRONG


def is_extreme(correct, comments=None):
    """是否满足「极高」的两条件：磁力 > 3 **且** 评论 > 10。

    两个条件是与关系 —— 评论再多也抬不动磁力不足的片
    （主人 2026-09-26：「评论不是硬性标准」，但它是升档条件而非替代条件）。
    """

    c = _as_int(correct, 0) or 0
    n = _as_int(comments)

    return c > MAGNETS_FOR_TOP and n is not None and n > COMMENTS_FOR_TOP


def is_high(correct):
    """是否满足「高」：磁力 > 5。不看评论。"""

    return (_as_int(correct, 0) or 0) > MAGNETS_FOR_HIGH


def tier_of(correct, comments=None):
    """按「正确磁力条数 + 评论数」定档（主人 2026-09-26 口径）。

    闸门（javdb 查不到番号）不在这里 —— 那由 D8 在入库前挡住，不入档。

    四档：

      极低  0 条正确磁力
      低    1~5 条，且不满足「极高」
      高    磁力 > 5 条（不看评论）
      极高  磁力 > 3 条 **且** 评论 > 10 条

    ⚠️ 比较是**严格大于**，与常量字面一致（见上方口径注释）。
    ⚠️ 判定必须走 `is_extreme()` / `is_high()` —— 别在这里重写一遍阈值，
       否则两处会漂移（这个模块被两份调用方共用：定档写库 + 置信分加成）。
    """

    c = _as_int(correct, 0) or 0

    if is_extreme(c, comments):
        return "极高"

    if is_high(c):
        return "高"

    return "低" if c >= 1 else "极低"


# ── D11 第三条（2026-09-24 主人拍板）：识别来源与文件格式 ──────────
#
# 起因（专家审查的设计级意见）：前两条条件衡量的是
# **「这个番号是否真实且成规模」**，而不是**「这个本地文件是不是这部片」**。
# 场景：文件其实是 P，文件名被识别成热门码 SSIS-001 -> 查回来 30 条磁力
# （名字都含 SSIS-001）+ 500 评论 -> 高档 + 极高 -> 第二条比对
# `SSIS-001 == SSIS-001` 通过 -> 把 P 删了。**前两条都挡不住。**
#
# 主人的选择：批量删要求识别**来自高置信主路径**，且文件是**常见 AV 格式**。
#
# 「高置信主路径」用 **match source** 判，不用 matcher 那个 0-100 的分数 ——
# 因为那个分数受「厂牌是否已收录」影响（命中已知厂牌 90、否则 70），
# 而厂牌与「这个文件是不是这部片」无关（主人 2026-09-23 已就此拍板
# 「厂牌不重要」）。真正相关的是**匹配形态**：番号是否以带分隔符的
# 规范形态被认出来。
#
#   std / fc2 / tnum / numpfx  -> 结构化形态匹配（番号带分隔符）  ← 可信
#   nosep                      -> 无分隔符（`ABP171`），形态弱     ← 不可信
#   bare                       -> 裸数字，最容易撞             ← 不可信
#   fallback（judge 里剥噪音后重试）                          ← 不可信
#
# 想要更高门槛时把 BATCH_MIN_CONFIDENCE 提上去即可（90 = 只认已收录厂牌）；
# 但注意那会重新引入厂牌依赖。
# 高置信主路径的识别来源。
#
# ⚠️ 取值必须与 `core/matcher_v2.NumberMatcher.match()` 实际产出的
# `source` **逐字一致**。这里踩过一次真 bug：写成 `"dict"`，
# 而 matcher 实际产出的是 `"dictionary"` —— 结果 208 个**完全可信**的
# 字典命中被判成「不可信」，批量删候选从 280+ 部掉到 99 部。
# 现在有 `tests/test_magnet_judge.py::test_trusted_sources_match_matcher_output`
# 把两边钉在一起，改一边不改另一边就会红。
TRUSTED_MATCH_SOURCES = frozenset({
    "dictionary",   # 人工策展的字典规则（confidence = 规则里的 priority）
    "fc2",
    "std",          # 带分隔符的标准形态（ABP-171）
    "tnum",         # 字母+数字前缀（T28-571）
    "numpfx",       # 数字前缀厂牌（300MAAN-403）
})

# 明确**不可信**的来源 —— 列出来是为了「有意识地区分」，
# 而不是漏掉。测试会检查 matcher 的每个输出都落在两者之一。
UNTRUSTED_MATCH_SOURCES = frozenset({
    # 无分隔符形态（`ABP171`）。形态弱，最容易与别的号撞。
    "nosep",
    # 裸数字，无厂牌信息。
    "bare",
    # 118 站点的紧凑编码（`118abp00171hhb`）。
    #
    # 它有 CONF_STD_KNOWN（90）、也确实是结构化形态，但**番号是经过
    # 站点编码还原出来的**（去 5 位零填充），属「变换」而非「原样识别」——
    # 与兜底路径同类。影响面极小（本次实测 4 个文件），保守起见不收。
    # 要收只需加进上面的集合。
    "118",
})

BATCH_MIN_CONFIDENCE = 0        # 0 = 不看分数，只认 source


def is_trusted_recognition(source, confidence=None):
    """这条识别是否算「高置信主路径」。

    ``source`` 由 `core.matcher_v2.NumberMatcher.match()` 给出；
    ``confidence`` 可选，给了就额外要求 >= `BATCH_MIN_CONFIDENCE`。
    """

    if source not in TRUSTED_MATCH_SOURCES:
        return False

    if BATCH_MIN_CONFIDENCE and confidence is not None:
        try:
            return int(confidence) >= BATCH_MIN_CONFIDENCE
        except (TypeError, ValueError):
            return False

    return True


def is_batch_deletable(tier, number_matches, confidence=None, source=None,
                       filepath=None):
    """D11 批量删的三条件。

    必须**同时**满足：

      1. 档位 >= 高（磁力证据）
      2. javdb 返回的 ``number`` 与识别出的番号一致（``number_matches``）
      3. **识别来自高置信主路径**（``source``）**且文件是常见 AV 格式**
         （``filepath``）—— 2026-09-24 主人拍板新增

    第 3 条防的是前两条都挡不住的情形：文件名被识别成**另一个真实热门码**，
    于是查回来一堆属于那个码的磁力、number 也「一致」——
    前两条全过，但那个文件根本不是这部片。

    只满足一条的转手动删；极低档一律不能删。

    ⚠️ 第 3 条的参数**缺省即拒绝**：没记录 source / filepath 的老数据
    一律不能批量删（宁可漏删）。
    """

    if tier not in ("高", "极高"):
        return False

    if not number_matches:
        return False

    if source is None or filepath is None:
        return False

    from core.av_format import is_typical_av_ext

    if not is_typical_av_ext(filepath):
        return False

    return is_trusted_recognition(source, confidence)


def manual_only(tier):
    """只能逐条手动删的档位（低档）。极低档连手动都不行。"""

    return tier == "低"


def deletable(tier):
    """该档位是否允许删除（低档及以上才谈删除）。"""

    return tier in ("低", "高", "极高")
