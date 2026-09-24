"""置信分：把「这个番号真实存在」与「这个文件就是这部片」合成一个 0-100 的分。

背景（2026-09-24）
-----------------
旧界面显示的是 `evidence_strong` —— 一个**二值位**，条件只有
「正确磁力 >= 10 条」。它衡量的是**服务器侧证据是否成规模**，
完全不看本地文件。

FH-27 就是这么拿到「强证据」的：15 条磁力、番号核对通过、
来源可信 —— 但本地 25 个文件全是 9~28 分钟的直播自拍短片，
元数据却是 130 分钟，短了 78%~93%。分数体系里根本没有时长的位置。

所以本模块分两段算：

    base        服务器侧「这个番号真实且成规模」         0-100
    consistency 本地文件「就是这部片」（时长吻合度）       0-1
    置信分 = base * consistency

**时长是乘性因子，不是与磁力并列的加分项。** 因为磁力条数、评论数、
番号核对、识别来源这四个维度，全都只证明「服务器上存在这个番号」；
只有时长是直接检验「本地这份文件 = 这部片」的证据。性质不同，
放在同一个加权和里会被前者的高分稀释掉。

⚠️ 分数**只用于展示与排序，不参与任何删除门控**。
   删除仍由 `tier`（D9）+ D11 三条件把关 ——
   见 `core/database_v2.deletion_eligibility()`。

   ⚠️ 但**别把这句话套到时长判定上**（2026-09-24 主人校准）：
   分数与「剔除」是两件事。分数不门控，而`duration_check.mismatch_verdict`
   判出「本地文件与元数据不是同一部片」时，**会**在抓元数据时把该番号
   从媒体库剔除（扣到 0 只是它的外在表现）。两者共用同一份时长数据，
   但决策权在 `mismatch_verdict`，不在分数高低。
"""

import math

from core import duration_check
from core.magnet_judge import TRUSTED_MATCH_SOURCES

# ── base 的四个维度权重（和为 1.0）──────────────────────────────
#
# 取值依据：
#   * 磁力权重最高 —— 「号码真实且成规模」最直接的证据。
#   * 番号核对次之 —— javdb 返回的号必须与本地认出的号逐字相同，
#     不一致意味着整个页面都属于另一部片（P0-2 那种张冠李戴）。
#   * 识别来源第三 —— 形态是否可信（带分隔符的标准形态 vs 裸数字）。
#   * 评论数最低 —— 只说明有讨论度，与「是不是这部片」最不相关。
W_MAGNETS = 0.36
W_MATCH = 0.29
W_SOURCE = 0.21
W_COMMENTS = 0.14

# 磁力条数饱和点：到这个数就算满，再多不加分。
# 取 15（不是高档门槛 3）是因为高档门槛只保证「能进批量删」，
# 而满分应当留给证据明显成规模的番号。
MAGNETS_SATURATION = 15

# 评论数饱和点，与 magnet_judge.COMMENTS_FOR_TOP 对齐。
COMMENTS_SATURATION = 50

# 来源可信时的得分，以及不可信时保留的得分。
SOURCE_TRUSTED = 1.0
SOURCE_UNTRUSTED = 0.30

# 时长相差极大时，consistency 的下限。
#
# 不设 0 是为了**避免偶发误伤把分数一把清光**：ffprobe 偶尔会给出
# 错的时长、元数据时长本身也可能错。留 0.15 让这类番号落到个位数
# 而不是 0，界面上仍一眼可疑，但不至于和服务端完全没查到的番号混淆。
CONSISTENCY_FLOOR = 0.15

# 没跑过时长核对时的 consistency。
# 用 1.0（不惩罚）+ 单独返回 duration_known=False，
# 让界面能标出「时长未验」而不是假装算过了。
CONSISTENCY_UNKNOWN = 1.0


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def magnet_factor(correct):
    """磁力条数 -> 0~1。对数式，3 条得 0.5，15 条封顶。"""

    c = max(0, int(correct or 0))

    if c <= 0:
        return 0.0

    return _clamp(math.log1p(c) / math.log1p(MAGNETS_SATURATION))


def comment_factor(comments):
    """评论数 -> 0~1。线性到饱和点。评论缺失（None）按 0 计。"""

    if comments is None:
        return 0.0

    try:
        n = int(comments)
    except (TypeError, ValueError):
        return 0.0

    return _clamp(n / float(COMMENTS_SATURATION))


def source_factor(source):
    """识别来源形态 -> 0~1。

    取值必须与 `matcher_v2` 实际产出的 source 逐字对齐 ——
    白名单直接复用 `magnet_judge.TRUSTED_MATCH_SOURCES`，
    两边不会再各留一份字面量表（那是踩过的真 bug）。
    """

    if not source:
        return SOURCE_UNTRUSTED

    return SOURCE_TRUSTED if source in TRUSTED_MATCH_SOURCES else SOURCE_UNTRUSTED


def base_score(correct, comments, matches, source, weights=None):
    """服务器侧「番号真实且成规模」-> 0~100。不含时长。

    `weights`: 四项配比（已归一化到 1）。不传用模块默认 W_*。
    主人 09-24 要求首页可自己配比 —— 权重从参数进来，但**默认值仍在这里**，
    保证「不配置 = 老行为」。

    零证据（没有任何正确磁力 **且** 番号核对没通过）直接 0：
    来源形态本身不构成「这个号存在」的证据，不能凭它拿底分。
    反例：javdb 认得这个号但没查到磁力（correct=0, matches=True），
    那是有证据的（50 分左右），不落在这条上。
    """

    if int(correct or 0) <= 0 and not matches:
        return 0

    w = weights or {
        "magnets": W_MAGNETS,
        "match": W_MATCH,
        "source": W_SOURCE,
        "comments": W_COMMENTS,
    }

    s = (
        w.get("magnets", W_MAGNETS) * magnet_factor(correct)
        + w.get("match", W_MATCH) * (1.0 if matches else 0.0)
        + w.get("source", W_SOURCE) * source_factor(source)
        + w.get("comments", W_COMMENTS) * comment_factor(comments)
    )

    return int(round(_clamp(s) * 100))


def consistency_from_ratios(ratios):
    """一组 (ratio, shorter) -> 0~1 的一致性因子。

    取**中位数**而不是最差或平均：
      * 取最差——真片常有附加的短花絮/预告文件，会把整部片拖低；
      * 取平均——同样被那些小文件带偏（它们时长小但 ratio 大）；
      * 取中位数——主体文件占多数时结果稳定，不受个别附件影响。

    `ratios` 元素为 `(ratio, shorter)`；`shorter=True` 表示本地比
    元数据短（可疑方向）。空输入返回 None（= 没数据，不是「不合格」）。
    """

    if not ratios:
        return None

    scored = []

    for item in ratios:

        if item is None:
            continue

        ratio, shorter = item

        if ratio is None:
            continue

        v = duration_check.score(ratio, shorter=bool(shorter))

        if v is not None:
            scored.append(v)

    if not scored:
        return None

    scored.sort()

    n = len(scored)
    mid = n // 2

    if n % 2:
        return scored[mid]

    return (scored[mid - 1] + scored[mid]) / 2.0


def file_ratios(files):
    """media_files 行 -> [(ratio, shorter)]。

    `shorter` 由本地/参考时长现算，而不是只看存的 `duration_ratio`
    （那一列是绝对值，丢了方向，而方向正是判定的关键：
    短了可疑、长了正常）。
    """

    out = []

    for f in files or []:

        local = f.get("duration_local") if isinstance(f, dict) else None
        ref = f.get("duration_ref") if isinstance(f, dict) else None

        if not local or not ref:
            continue

        try:
            local_s = float(local)
            ref_s = float(ref) * 60.0
        except (TypeError, ValueError):
            continue

        if ref_s <= 0:
            continue

        out.append((abs(local_s - ref_s) / ref_s, local_s < ref_s))

    return out


def score(correct, comments=None, matches=None, source=None,
          ratios=None, mismatch=False, weights=None):
    """算置信分。

    ratios: `[(ratio, shorter)]`，来自 `file_ratios()`。
            传 None / 空表示**还没跑过时长核对**（不是「时长不合格」）。

    mismatch: 时长已被判**完全不符**（`duration_check.mismatch_verdict`）。
              为 True 时直接归零，且**不走 CONSISTENCY_FLOOR 兜底** ——
              见下方注释。

    weights:  四项配比（已归一化到 1），来自首页的「自己配比」。
              不传 = 模块默认（老行为）。

    返回 `{score, base, consistency, duration_known, mismatch}`：
      * `score`          0-100，界面显示与排序用
      * `base`           不含时长的服务器侧分
      * `consistency`    时长因子（1.0 但 duration_known=False 时是「未验」）
      * `duration_known` 时长是否真的参与过计算
      * `mismatch`       是否被判「本地文件与元数据不是同一部片」
    """

    if matches is None:
        # 没传就当作核对通过 —— 调用方没数据时不要凭空白扣 29 分。
        matches = True

    base = base_score(correct, comments, matches, source, weights=weights)

    if mismatch:

        # ── 主人 2026-09-24 定：超大差距要扣到 0 ──
        #
        # 这里**必须绕开下面那个 CONSISTENCY_FLOOR**。兜底是为
        # 「ffprobe 偶发误读」留的缓冲，它假设我们只有**一个**弱信号；
        # 而走到这里时证据已经不止一个：每文件比例的中位数极低
        # **且**求和比排除了分片。再兜底就等于把真问题藏起来 ——
        # FH-27 正是这么拿到 0.15 兜底、显示约 12 分而不是 0 的。
        return {
            "score": 0,
            "base": base,
            "consistency": 0.0,
            "duration_known": True,
            "mismatch": True,
        }

    cons = consistency_from_ratios(ratios)

    if cons is None:
        return {
            "score": base,
            "base": base,
            "consistency": CONSISTENCY_UNKNOWN,
            "duration_known": False,
            "mismatch": False,
        }

    cons = max(CONSISTENCY_FLOOR, _clamp(cons))

    return {
        "score": int(round(base * cons)),
        "base": base,
        "consistency": cons,
        "duration_known": True,
        "mismatch": False,
    }
