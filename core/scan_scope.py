# -*- coding: utf-8 -*-
"""扫描范围解析：哪些盘扫、扫到哪一层。

分三档（2026-09-24 主人定）：

    skip    不扫
    known   只扫 `known_paths` 里列的已知 AV 目录（保护慢盘/病盘）
    full    整盘扫

## 为什么不干脆全扫

`E:` 与 `F:` 是**同一块物理盘**（TOSHIBA HDWD130，WMI 报 Warning /
Predictive Failure）。全盘遍历会读大量目录项 —— 对一块已经在预警的盘
是额外负担，而收益接近零：AV 只集中在少数几个目录里
（26,230 条快照统计：`X:\\downloads` / `X:\\downloads` / `X:\\media` /
`X:\\dl-other` 四个目录占了绝大多数命中）。

## 安全默认

**只有列在 `scan_scope` 里的盘才参与扫描。** 不写就等于不扫 ——
这样 C 盘（系统盘）不会因为"默认全扫"被意外遍历。
"""

import os
import string

VALID_MODES = ("skip", "known", "full")

DEFAULT_MODE = "skip"


def _norm_drive(key):
    """把配置里的盘符写法归一成 `E:` 这种形式。

    接受: `E` / `E:` / `e:` / `X:\\` / `E:/`
    """

    s = str(key or "").strip().upper().rstrip("\\/")

    if not s:
        return ""

    if len(s) == 1 and s in string.ascii_uppercase:
        return s + ":"

    if len(s) == 2 and s[1] == ":" and s[0] in string.ascii_uppercase:
        return s

    return s


def _norm_path(p):
    """路径归一：统一单反斜杠、去尾部分隔符、大写盘符。

    ⚠️ **盘根要保留尾部反斜杠**：`X:\\` 不能被削成 `D:` ——
    在 Windows 里 `D:` 表示「D 盘的当前目录」，不是根目录。
    削掉会让扫描跑到别的地方去（实测抓到的 bug）。
    """

    s = str(p or "").strip().replace("/", "\\")

    while "\\\\" in s:
        s = s.replace("\\\\", "\\")

    # 先判断是不是盘根（削之前判）
    body = s.rstrip("\\")

    is_root = len(body) == 2 and body[1] == ":"

    s = body

    if len(s) >= 2 and s[1] == ":":
        s = s[0].upper() + s[1:]

    if is_root:
        s += "\\"

    return s


def parse_scope(config):
    """配置 -> `{盘符: 模式}`。非法取值按 `skip` 并记下来。"""

    raw = (config or {}).get("scan_scope") or {}

    out = {}
    bad = []

    for key, val in raw.items():

        drive = _norm_drive(key)

        if not drive:
            continue

        mode = str(val).strip().lower()

        if mode not in VALID_MODES:
            bad.append((key, val))
            mode = DEFAULT_MODE

        out[drive] = mode

    return out, bad


def known_paths(config, drive=None):
    """`known` 档要扫的路径。给了 drive 就只回该盘的。"""

    paths = [
        _norm_path(p)
        for p in ((config or {}).get("known_paths") or [])
    ]

    paths = [p for p in paths if p]

    if not drive:
        return paths

    d = _norm_drive(drive)

    return [p for p in paths if p.upper().startswith(d)]


def resolve_targets(config, explicit=None):
    """算出本次要扫的目录列表。

    `explicit` 是命令行显式给的路径 —— 给了就用它，无视配置
    （显式意图优先于配置）。

    返回 `(targets, notes)`；`notes` 是给人看的说明，讲清每个盘为什么
    扫 / 不扫、以及哪些路径不存在。
    """

    if explicit:
        return list(explicit), ["使用命令行指定的路径（忽略 scan_scope）"]

    scope, bad = parse_scope(config)

    notes = []

    for key, val in bad:
        notes.append("scan_scope 里 {:?} 取值 {!r} 不认识，按 skip 处理".format(
            key, val))

    if not scope:
        # 没配 scan_scope -> 退回旧的 scan_paths（向后兼容）
        legacy = [
            _norm_path(p)
            for p in ((config or {}).get("scan_paths") or [])
        ]

        legacy = [p for p in legacy if p]

        if legacy:
            notes.append("未配置 scan_scope，回退到 scan_paths")

        return legacy, notes

    targets = []

    for drive in sorted(scope):

        mode = scope[drive]

        if mode == "skip":
            notes.append("{}  skip —— 不扫".format(drive))
            continue

        if mode == "full":
            targets.append(drive + "\\")
            notes.append("{}  full —— 整盘扫".format(drive))
            continue

        # known
        paths = known_paths(config, drive)

        if not paths:
            notes.append("{}  known —— 但 known_paths 里没有该盘的路径，跳过".format(
                drive))
            continue

        for p in paths:

            if os.path.isdir(p):
                targets.append(p)
            else:
                notes.append("{}  路径不存在，已跳过：{}".format(drive, p))

        notes.append("{}  known —— 扫 {} 个已知目录".format(
            drive, len([p for p in paths if os.path.isdir(p)])))

    return targets, notes
