# -*- coding: utf-8 -*-
"""时长探测与比对。

## 定位：**两段式**（主人 2026-09-24 定，先「只作评分」后校准）

### 一、小偏差 —— 只作评分，**不作拒绝理由**

AV 在传播过程中常被加几分钟广告片头，或者同目录下放一个几分钟的
「番号预览」视频。所以「本地时长 ≠ 元数据时长」**不能**当拒绝理由 ——
真片会被误杀。

它作为**评分项**用，并且能抓出一类 D8/D11 都拦不住的东西：**片段文件**。
实测（2026-09-24，83 个文件）：

    真正片   javdb 125分  vs  本地 127.8分   (2.3%)
    真正片   javdb 210分  vs  本地 210.7分   (0.3%)
    片段     javdb 120分  vs  本地 0.6分     (99%)   ← IPX-879-A.mp4
    片段     javdb 120分  vs  本地 0.3分     (100%)  ← IPX-916-K.mp4

77/83（93%）在 6% 以内；偏差大的几乎都是片段。

### 二、超大差距 —— 扣到 0 **并踢出媒体库**

主人 2026-09-24 校准：像 FH-27 那样「9 分钟 vs 130 分钟」的差距，
不是轻扣分的事。`mismatch_verdict()` 判出「本地这批文件根本不是这部片」
时，置信分归零，且抓元数据时该番号会从媒体库移除（磁盘文件不动）。

档位（`tier`）仍只由服务器侧磁力证据决定，**时长不参与删除门控** ——
两条路径的决策体是 `mismatch_verdict`，不是分数高低。

## 为什么用 ffprobe 而不是抽帧校验

ffprobe 只读容器头（几 KB），对 E/F 这类有病盘的机器压力极小。
抽帧要解码画面，成本高一个量级，还要先标定（非同厂版、裁剪版、水印都会
让感知哈希失配）—— 主人已定「费效比不行，先不做」。
"""

import os
import subprocess

# 判定阈值。**只影响评分与提示，不拒绝任何东西。**
#
# 取「相对 + 绝对」两个，命中任一即算吻合：
#   * 相对 10%：覆盖实测的广告片头（一般几分钟，占比通常 < 8%）
#   * 绝对 5 分钟：短片上很有用 —— 比如 20 分钟的片，加 1 分钟广告就是 5%，
#     但如果按纯相对算，10 分钟的片加 1 分钟就到 10% 边缘了
RATIO_OK = 0.10
ABS_OK_SECONDS = 5 * 60


def probe_duration(path, timeout=30):
    """ffprobe 取时长（秒）。取不到返回 None。

    **绝不抛异常**：这个函数在遍历几万个文件的路径上，一个坏文件
    不该打断整轮。
    """

    if not path:
        return None

    try:

        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, timeout=timeout,
        ).stdout.decode("utf-8", "replace").strip()

        if not out:
            return None

        value = float(out)

        # ffprobe 对某些损坏文件会返回 0 或负数 —— 视为取不到
        return value if value > 0 else None

    except (OSError, subprocess.SubprocessError, ValueError):

        return None


def compare(local_seconds, ref_minutes):
    """本地时长（秒）与元数据时长（分钟）比对。

    返回 `{ratio, delta_seconds, ok}`；任一缺数据则返回 None。
    """

    if not local_seconds or not ref_minutes:
        return None

    try:

        ref_seconds = float(ref_minutes) * 60.0

    except (TypeError, ValueError):

        return None

    if ref_seconds <= 0:
        return None

    delta = local_seconds - ref_seconds

    ratio = abs(delta) / ref_seconds

    ok = ratio <= RATIO_OK or abs(delta) <= ABS_OK_SECONDS

    return {
        "ratio": ratio,
        "delta_seconds": delta,
        "ok": ok,
    }


def score(ratio, shorter=False):
    """时长吻合度 -> 0~1 的评分（1 = 完全吻合）。

    **不对称**：短了可疑，长了正常。

    依据（2026-09-24 实测 83 个真实文件）：真片的偏差**全是正的**，
    普遍比元数据长 3~6% —— 那是传播时加的片头广告/水印段，属常态。
    按对称算法，一个 12% 偏长的真片只会得 0.42 分（看着像出了问题），
    但它其实完全正常。所以「偏长」的扣分要轻得多。

    真正该警惕的是**短**：实测片段文件只有正片的 0.5%（0.6 分钟 vs 120 分钟）。

    只用于展示与排序，**不参与任何门控**。
    """

    if ratio is None:
        return None

    r = abs(float(ratio))

    if shorter:

        # 短了：0% -> 1.0，>=20% -> 0.0
        if r <= 0:
            return 1.0

        if r >= 0.20:
            return 0.0

        return 1.0 - (r / 0.20)

    # 长了（或未知方向）：容忍度高得多，>=50% 才归零
    if r <= 0:
        return 1.0

    if r >= 0.50:
        return 0.0

    return 1.0 - (r / 0.50) * 0.5


def is_likely_fragment(local_seconds, ref_minutes, max_ratio=0.25):
    """是不是**很可能**为片段（几十秒/几分钟 vs 上百分的元数据）。

    ⚠️ 这是**提示**，不是判定。真片被加了长广告、或者本身就是短篇集，
    都可能落在这里。界面标出来让人看，别拿它自动删。
    """

    c = compare(local_seconds, ref_minutes)

    if not c:
        return False

    return c["ratio"] > max_ratio and c["delta_seconds"] < 0


# ═══════════════════════ 时长「完全不符」判定 ═══════════════════════
#
# 主人 2026-09-24 定：像 FH-27 那种「9 分钟 vs 130 分钟」的**超大差距**，
# 不是轻扣分的事 —— 要扣到 0 分，并在抓元数据时把它从媒体库里踢出去。
#
# 上面 `score()` 的「只作评分、不拒绝」针对的是**小偏差**（几分钟广告
# 片头、长花絮），那些确实不能拒绝。本段处理的是另一个量级：
# 本地这一堆文件与元数据**根本不是同一部片**。

# 每文件比例的中位数低于此值 -> 本地文件与元数据是两部片。
#
# 取值依据（2026-09-24 实测 296 个番号、326 个文件）：
#      p1  = 0.238    p5  = 0.563    p10 = 0.979
#      p50 = 1.006    p90 = 1.043    p99 = 1.149
# 正常片子紧紧堆在 1.0 附近（p10~p90 只有 0.98~1.04），坏的掉到 0.25 以下，
# **中间是空的** —— 0.25 距两侧都有很大余量，不是卡在数据堆里切的。
MISMATCH_RATIO = 0.25

# **分片豁免**：一部片被拆成几个文件时，每个文件都远小于元数据，
# 但**求和**接近。实测（同一批数据）：
#     KSDO-021  5 文件  中位数 0.193  求和比 1.00  ← 单看每文件会误杀
#     SDMU-960  4 文件  中位数 0.212  求和比 0.85  ← 同上
# 没有这道豁免，这两部真片会被扣到 0 并踢出库。
SPLIT_SUM_LO = 0.75
SPLIT_SUM_HI = 1.35

# 至少要有这么多个**有数据的文件**才敢判分片 —— 一个文件永远不是分片。
SPLIT_MIN_FILES = 2


def _median(values):
    if not values:
        return None

    vs = sorted(values)
    n = len(vs)
    mid = n // 2

    if n % 2:
        return vs[mid]

    return (vs[mid - 1] + vs[mid]) / 2.0


def times_corroborate(sum_ratio):
    """求和比是否落在「分片」区间。`None` -> 不能据此豁免。"""

    if sum_ratio is None:
        return False

    return SPLIT_SUM_LO <= sum_ratio <= SPLIT_SUM_HI


def number_in_filename(number, filepath):
    """番号是否出现在**文件自己的名字**里（而不是只在目录名）。

    这是「认错片」与「内容不全」的分水岭（主人 2026-09-24 提的分片情形）：

      * `X:\\…\\DSVR-219\\DSVR-219D.VR.mp4` —— 名字里就有 DSVR-219。
        文件**确实是它的一部分**，时长短是因为只下了 D/E 两片，
        不是认错片 -> **不该剔**。
      * `…等FH 27V\\…～01.mp4` —— 名字里没有 FH-27，那个号是从**目录名**
        抠出来的 -> 才可能是把别人的片认成了它。

    用归一化子串判断，不重跑 matcher：把两边都压成大写字母数字，
    再看包含关系（`DSVR-219D.VR.mp4` -> `DSVR219DVRMP4` 含 `DSVR219`）。
    这样不用为判定再实例化一个 parser，也不会被分隔符形态影响。
    """

    import os
    import re as _re

    if not number or not filepath:

        return False

    def squash(s):

        return _re.sub(r"[^A-Za-z0-9]", "", s or "").upper()

    return squash(number) in squash(os.path.basename(filepath))


def mismatch_verdict(pairs, ref_seconds=None, partial_ok=False):
    """判断一组本地文件与元数据是否**完全不符**（不是同一部片）。

    `pairs`: `[(local_seconds, ref_seconds), ...]` —— 拿不到某个值就跳过该条。

    `partial_ok`: 这批文件**已经确认属于该番号**（番号就写在文件名里），
      且**是多个文件**（一部片分成 A/B/… 几段）-> 时长偏短只说明
      **内容没下全**，不是认错片 -> 一律不判不符。

      ⚠️ 两个条件缺一不可，都是实测逼出来的：
        * 只看「番号在文件名里」不够 —— `IPX-951` 的文件名里也有这个号
          （`…[IPX-951].mp4`），但那是 38 分钟的**自拍合集**，不是正片，
          本该判不符。
        * 看「多个文件」才对得上主人 09-24 的判断：`DSVR-219D/E`
          （两段）加起来只有元数据的 48%，是**分成了 A B 两个视频**，
          不是认错片。

    返回 `{mismatch, median, sum_ratio, n, reason}`：
      * `mismatch=True`  该扣到 0 分并从媒体库剔除
      * `mismatch=False` 正常，**或证据不足以判定**（宁可放过不可错杀）

    三道豁免，缺任一条都会误杀真片：
      1. **番号在文件名里** -> 内容不全，不是认错（`partial_ok`）
      2. **分片**：每文件都小、但求和接近元数据（见 SPLIT_SUM_* 的实测）
      3. **证据不足**：没有可用数据时一律判 False，绝不凭空白杀
    """

    usable = [
        (float(loc), float(ref))
        for loc, ref in (pairs or [])
        if loc and ref and float(ref) > 0
    ]

    if not usable:

        return {
            "mismatch": False,
            "median": None,
            "sum_ratio": None,
            "n": 0,
            "reason": "no_data",
        }

    ratios = [loc / ref for loc, ref in usable]

    med = _median(ratios)

    # 求和比：只对「每文件都偏小」的情形有意义，所以仍然按中位数判门槛，
    # 求和只用来**豁免**。元数据时长优先用外层给的（与时长来源一致）。
    ref = float(ref_seconds) if ref_seconds else max(r for _l, r in usable)

    sum_ratio = (sum(loc for loc, _r in usable) / ref) if ref > 0 else None

    info = {
        "mismatch": False,
        "median": med,
        "sum_ratio": sum_ratio,
        "n": len(usable),
        "reason": "",
    }

    if med is None or med >= MISMATCH_RATIO:

        info["reason"] = "ratio_ok"

        return info

    # 番号就在文件名里 -> 是它的一部分，只是没下全。绝不剔。
    if partial_ok:

        info["reason"] = "partial_content"

        return info

    # 比例极低 —— 再看是不是分片
    if len(usable) >= SPLIT_MIN_FILES and times_corroborate(sum_ratio):

        info["reason"] = "split_across_files"

        return info

    info["mismatch"] = True
    info["reason"] = "duration_mismatch"

    return info
