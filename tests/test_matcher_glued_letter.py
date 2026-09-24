# -*- coding: utf-8 -*-
"""目录名误匹配收紧：数值后**直接粘字母**的未收录厂牌不再认。

背景（主人 2026-09-24 报的 FH-27 假阳性）：
  目录名 `…激情啪啪等FH 27V` 被抠成 `FH-27`（尾部 V 当版本标记丢掉），
  而本地其实是 25 集自拍短片。JavDB 上 FH-27 真实存在，于是它的磁力
  与评论被强加在这些错误文件上 —— 卡片显示「高 / 磁力 15 / 评论」，
  全不是它的。要害不是「查不到」，恰恰是「查得到」。

收紧规则：`prefix not in KNOWN_STUDIO and 数值后紧跟 ASCII 字母` -> 不认。

**为什么不一刀切**：`ABP-171UC`（无厂牌）、`ABP-00171hhb`（118 站点 ID）
这类「数值粘尾字母」在**已收录**厂牌上是常见噪声标记，一刀切会把真番号
一起挡掉。已收录厂牌人工策展过，信任它。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.matcher_v2 import NumberMatcher, _glued_letter  # noqa: E402


def _engine():
    return NumberMatcher([])


def _top(text):
    got = _engine().match(text)

    return got[0]["number"] if got else None


# ── 该挡的 ────────────────────────────────────────────────

REJECT = [
    # 主人报的原始形态（去掉 amateur 词，确保走的是这条新规则
    # 而不是 is_non_jav 的旁路拦截）
    (r"D:\x\無修正FH 27V\a.mp4", "fh27v_space"),
    (r"D:\x\FH27V\a.mp4", "fh27v_nospace"),
]

# ⚠️ 曾经还挡过 `XYZ 145V`（3 位数字+粘字母），**2026-09-24 撤销**：
# 语料实测「3-5 位数字 + 粘字母」有 51 条，全是真分片标记
# （ATID-516C / OFJE-312A / MVSD-513C …）—— 与 `XYZ 145V` 形态完全一样，
# **分不开**。硬挡会误杀真片，所以这一档放行，交给时长判定兜底
# （那才是「认错片」的判据）。收紧只保留「2 位数字」这一档：
# 语料里 2 位数字+粘字母是 **0 条**，纯噪声形态。


def test_reject_glued_letter_unknown_studio():
    for path, tag in REJECT:
        assert _top(path) is None, "{}：{} 不该被认成番号".format(tag, path)


# ── 不该误伤的（这些是真实语料里的形态）────────────────────

KEEP = [
    # 已收录厂牌 + 粘尾字母：无厂牌 / 站点后缀，是真番号的噪声标记
    (r"D:\x\ABP-171UC.非同厂版本.torrent", "ABP-171"),
    (r"D:\x\ABP-00171hhb_000^WM.mp4", "ABP-00171"),
    # 分隔开的版本标记
    (r"D:\x\ABP-171-C.torrent", "ABP-171"),
    (r"D:\x\ABP-171-U.torrent.非同厂版本", "ABP-171"),
    # 常规形态
    (r"D:\x\[HD]ABP-171\ABP-171.mp4", "ABP-171"),
    (r"D:\x\07121-abp-171-HD\a.mp4", "ABP-171"),
    (r"D:\x\SSIS-001\SSIS-001.mp4", "SSIS-001"),
    (r"D:\x\STARS-804\a.mp4", "STARS-804"),
    (r"D:\x\FC2-PPV-1115273\a.mp4", "FC2-PPV-1115273"),
    (r"D:\x\ABP-171\ABP-171 彼女のお姉さん.mp4", "ABP-171"),
    # ── 真·分片标记（3 位以上数字 + 字母）──
    # ⚠️ 这几条是「我第一版收紧过头」的回归护栏：当初没卡数字位数，
    # 把它们一起挡了 —— `DSVR-219D.VR.mp4` / `KSDO-021A.avi` 当场认不出，
    # 重扫会让它们掉出库，直接抵消「保卡」。
    (r"F:\x\DSVR-219\DSVR-219D.VR.mp4", "DSVR-219"),
    (r"F:\x\DSVR-219\DSVR-219E.VR.mp4", "DSVR-219"),
    (r"D:\x\KSDO-021\KSDO-021A.avi", "KSDO-021"),
    (r"D:\x\KSDO-021\KSDO-021B.avi", "KSDO-021"),
    (r"D:\x\ATID-516\ATID-516C.mp4", "ATID-516"),
    (r"D:\x\OFJE-312A~nyap2p.com.mp4", "OFJE-312"),
    # 站点后缀 ch（变体标记），3 位数字
    (r"D:\x\fsdss-274ch.mp4", "FSDSS-274"),
]


def test_keep_real_numbers():
    for path, want in KEEP:
        got = _top(path)
        assert got == want, "{}：认出 {}，应为 {}".format(path, got, want)


# ── 边界：未收录厂牌但**没有**粘尾字母 -> 仍然放行 ────────

def test_unknown_studio_without_glued_letter_still_passes():
    """这条故意不收紧。

    没有粘尾字母时无从判断它是不是真番号，交给 JavDB 核对（D8 查不到即剔除）。
    收紧到这一档会误伤真实存在的冷门厂牌。
    """

    got = _engine().match(r"D:\x\XY 27\a.mp4")

    assert got, "未收录厂牌 + 无粘尾字母不该被挡"

    assert got[0]["confidence"] == 70, "未收录厂牌应为 70 分"


# ── 助手本身 ──────────────────────────────────────────────

def test_glued_letter_helper():
    assert _glued_letter("FH27V", 4) is True
    assert _glued_letter("FH27", 4) is False        # 到末尾
    assert _glued_letter("ABP171-C", 6) is False    # 是 '-' 不是字母
    assert _glued_letter("171UC", 3) is True
