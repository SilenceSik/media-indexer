# -*- coding: utf-8 -*-
"""扫描范围解析测试。

核心安全约束：**只有列在 scan_scope 里的盘才会被扫** ——
不写就等于不扫，这样系统盘不会因为「默认全扫」被意外遍历。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.scan_scope import (                                 # noqa: E402
    known_paths,
    parse_scope,
    resolve_targets,
)


def test_parse_scope_normalizes_drive_keys():
    """盘符各种写法都要认：`E` / `E:` / `e:` / `E:\\`。"""

    scope, bad = parse_scope({
        "scan_scope": {"c": "skip", "D:": "full", "e": "known", "F:\\": "known"}
    })

    assert scope == {"C:": "skip", "D:": "full", "E:": "known", "F:": "known"}
    assert bad == []


def test_unknown_mode_falls_back_to_skip():
    """不认识的取值按 skip，并且要记下来（不能静默）。"""

    scope, bad = parse_scope({"scan_scope": {"D:": "sometimes"}})

    assert scope["D:"] == "skip"
    assert len(bad) == 1


def test_unlisted_drive_is_never_scanned(tmp_path):
    """**安全约束**：没列出来的盘不扫。

    这条保证系统盘（C:）不会因为配置省略而被意外遍历。
    """

    targets, notes = resolve_targets({"scan_scope": {"D:": "full"}})

    assert targets == ["D:\\"]

    assert not any(t.upper().startswith("C:") for t in targets)


def test_skip_mode_excluded(tmp_path):
    targets, notes = resolve_targets({
        "scan_scope": {"C:": "skip", "D:": "full"}
    })

    assert targets == ["D:\\"]
    assert any("skip" in n for n in notes)


def test_full_mode_scans_whole_drive():
    targets, _ = resolve_targets({"scan_scope": {"M:": "full"}})

    assert targets == ["M:\\"]


def test_known_mode_uses_known_paths():
    """known 档只扫列出来的目录，**不整盘**。

    这里用真实存在的 E: 路径 —— 关键断言是「不会退化成扫 E:\\ 整盘」。
    只做 isdir 判断，不遍历（E 是病盘，不碰内容）。
    """

    targets, notes = resolve_targets({
        "scan_scope": {"E:": "known"},
        "known_paths": [r"E:\迅雷下载", r"E:\Down"],
    })

    assert targets == [r"E:\迅雷下载", r"E:\Down"]

    # 关键：没有 E:\ 本身
    assert r"E:\\" not in targets
    assert not any(t.rstrip("\\").upper() == "E:" for t in targets)


def test_known_mode_does_not_scan_other_drives(tmp_path):
    """known 档只捡本盘路径 —— 别的盘的路径不该被这个盘扫到。

    `tmp_path` 在 C 盘，用它当「不属于 E 的路径」，验证会被过滤掉。
    """

    other = tmp_path / "not_on_e"
    other.mkdir()

    targets, _ = resolve_targets({
        "scan_scope": {"E:": "known"},
        "known_paths": [str(other), r"E:\迅雷下载"],
    })

    assert str(other) not in targets, "C 盘的路径不该被 E 盘捡走"
    assert targets == [r"E:\迅雷下载"]


def test_missing_known_path_is_reported_not_crashed():
    """known_paths 里写了不存在的目录 -> 跳过并说明，不报错。"""

    targets, notes = resolve_targets({
        "scan_scope": {"E:": "known"},
        "known_paths": [r"E:\definitely\not\here"],
    })

    assert targets == []
    assert any("不存在" in n for n in notes)


def test_known_mode_without_paths_is_reported():
    """known 档但没给该盘路径 -> 跳过并说明（不是静默什么都不做）。"""

    targets, notes = resolve_targets({"scan_scope": {"E:": "known"}})

    assert targets == []
    assert any("known_paths" in n for n in notes)


def test_explicit_paths_win_over_scope():
    """命令行显式给的路径优先于配置。"""

    targets, notes = resolve_targets(
        {"scan_scope": {"C:": "skip"}}, explicit=[r"X:\somewhere"]
    )

    assert targets == [r"X:\somewhere"]
    assert any("命令行" in n for n in notes)


def test_legacy_scan_paths_still_work():
    """没配 scan_scope 时回退到旧的 scan_paths（向后兼容）。"""

    targets, notes = resolve_targets({"scan_paths": [r"D:\\", r"E:\\"]})

    assert "D:\\" in targets
    assert any("scan_paths" in n for n in notes)


def test_drive_root_keeps_trailing_backslash():
    """**盘根必须保留尾部反斜杠**。

    这是实测抓到的真 bug：`_norm_path` 原本无脑 `rstrip('\\\\')`，
    把 `D:\\` 削成 `D:` —— 而 Windows 里 `D:` 是「D 盘的当前目录」，
    不是根目录。扫描会跑到别的地方去。
    """

    targets, _ = resolve_targets({"scan_scope": {"D:": "full"}})

    assert targets == ["D:\\"], "盘根不能削成 D:"

    # 各种写法都该归一到 D:\
    for written in ("D", "D:", r"D:\\", "D:/"):

        t, _ = resolve_targets({"scan_scope": {written: "full"}})

        assert t == ["D:\\"], "{} 没归一成盘根".format(written)


def test_path_normalization():
    """路径归一：斜杠、重复分隔符、尾巴。"""

    targets, _ = resolve_targets({
        "scan_scope": {"E:": "known"},
        "known_paths": ["E:/迅雷下载/", r"E:\\片\\\\"],
    })

    # 目录不存在所以 targets 为空，但归一化本身不该崩
    assert isinstance(targets, list)
