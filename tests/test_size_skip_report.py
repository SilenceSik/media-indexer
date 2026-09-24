# -*- coding: utf-8 -*-
"""大小门槛的"挡下上报"测试。

真实 bug：扫描器遇到大小不符的文件直接 `continue`，什么都不记。界面上的
`skipped_count` 永远是 0 —— 用户开了门槛，看到"扫到 4 个文件、通过 2 个"，
却不知道另外 2 个是被门槛挡下的还是压根没识别出来。门槛是否生效无从判断。

修法是给 scanner 加 `on_size_skip` 回调，由调用方收口计数。

────────────────────────────────────────────────────────────────
⚠️ 本文件**只用字节级的小文件**（几个字节），门槛也按字节给。

过滤逻辑跟"字节数有多大"完全无关，用 50 字节和 50 GB 走的是同一条分支。
之前这版用真·GB 级文件（最狠的一个 12 GB），一轮跑掉 16.5 GB，pytest 默认
保留最近 3 轮 —— 直接把 C 盘写满（实测 57 GB，剩 3.2 GB 可用）。
别改回去。要"看起来真实"的大文件时，改门槛，不要改文件。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.scanner_v2 import Scanner                          # noqa: E402

# 三个档位（字节）。整组加起来不到 1 KB。
SMALL = 50          # 小
MID = 400           # 中
BIG = 4000          # 大


def make_file(path, size):
    """造一个**真·小**文件。"""

    path.write_bytes(b"x" * size)

    return str(path)


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "media"

    d.mkdir()

    make_file(d / "ABP-171.mp4", SMALL)
    make_file(d / "SSIS-001.mp4", MID)
    make_file(d / "MIDV-100.mp4", BIG)

    return d


def run(tree, **kw):
    skips = []

    scanner = Scanner(":memory:")

    got = scanner.scan(
        str(tree),
        update_index=False,
        on_size_skip=lambda p, s, lim, is_min: skips.append(
            (os.path.basename(p), s, lim, is_min)
        ),
        **kw
    )

    return sorted(os.path.basename(f) for f in got), skips


def test_lower_bound_reports_skips(tree):
    """设了下限，被挡下的文件要出现在回调里。"""

    got, skips = run(tree, min_size=MID)

    assert got == ["MIDV-100.mp4", "SSIS-001.mp4"]

    assert len(skips) == 1, f"应上报 1 个，实际 {skips}"

    name, size, limit, is_min = skips[0]

    assert name == "ABP-171.mp4"
    assert size == SMALL
    assert limit == MID
    assert is_min is True, "应标记为「小于下限」"


def test_upper_bound_reports_skips(tree):
    """设了上限，超出的文件要出现在回调里。"""

    got, skips = run(tree, max_size=1000)

    assert got == ["ABP-171.mp4", "SSIS-001.mp4"]

    assert len(skips) == 1

    name, size, limit, is_min = skips[0]

    assert name == "MIDV-100.mp4"
    assert size == BIG
    assert is_min is False, "应标记为「超过上限」"


def test_both_bounds_report_both_sides(tree):
    """两端都设，两个方向都要上报。"""

    got, skips = run(tree, min_size=MID, max_size=1000)

    assert got == ["SSIS-001.mp4"]

    assert len(skips) == 2

    by_name = {s[0]: s for s in skips}

    assert by_name["ABP-171.mp4"][3] is True     # 太小
    assert by_name["MIDV-100.mp4"][3] is False   # 太大


def test_no_bounds_reports_nothing(tree):
    """不设门槛就不该有上报。"""

    got, skips = run(tree)

    assert len(got) == 3
    assert skips == []


def test_callback_is_optional(tree):
    """不给回调也不能崩（CLI 那条路就不需要）。"""

    scanner = Scanner(":memory:")

    got = scanner.scan(str(tree), update_index=False, min_size=MID)

    assert len(got) == 2


def test_boundary_is_inclusive(tree):
    """正好等于门槛的文件要留下 —— 用 < 和 > 比较，不是 <= / >=。"""

    got, skips = run(tree, min_size=MID, max_size=MID)

    assert got == ["SSIS-001.mp4"], "正好卡在两端，应通过"
    assert len(skips) == 2


def test_skipped_files_do_not_advance_index(tree):
    """被门槛挡下的文件不能推进 file_index。

    否则放宽门槛后重扫，这些文件因为"索引里已是最新"而被静默跳过，
    用户会以为功能坏了。
    """

    scanner = Scanner(":memory:")

    # 先只放行最大的那个，其余被挡下
    first = scanner.scan(str(tree), update_index=True, min_size=3000)

    assert [os.path.basename(f) for f in first] == ["MIDV-100.mp4"]

    # 放宽门槛重扫：上一轮被挡下的两个必须出现（说明索引没被推进）。
    # MIDV-100 不该再出现 —— 它上轮已入索引且未变化，本来就该跳过。
    second = scanner.scan(str(tree), update_index=False, min_size=0)

    names = sorted(os.path.basename(f) for f in second)

    assert names == ["ABP-171.mp4", "SSIS-001.mp4"], (
        f"放宽门槛后没扫全：{names} —— 被挡下的文件疑似被推进了索引"
    )
