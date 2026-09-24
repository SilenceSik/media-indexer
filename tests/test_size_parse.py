# -*- coding: utf-8 -*-
"""磁力体积文本排序：**必须换算成字节**再排，不能交给 SQL。

背景（2026-09-24 实测 ABP-171 的 76 条磁力）：
  `magnets.size_text` 是文本（`"5.65 GB"` / `"818 MB"`），
  `ORDER BY size_text DESC` 走的是**字典序** —— `'8' > '5'`，
  于是 818 MB 排在 5.65 GB 前面，整个「按体积排序」是坏的。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.size_parse import sort_key, to_bytes  # noqa: E402


def test_to_bytes_basic_units():
    assert to_bytes("818 MB") == 818 * 1024 ** 2
    assert to_bytes("490 MB") == 490 * 1024 ** 2
    assert to_bytes("5.65 GB") == int(5.65 * 1024 ** 3)
    assert to_bytes("1.5 TB") == int(1.5 * 1024 ** 4)
    assert to_bytes("2 KB") == 2048
    assert to_bytes("0 B") == 0


def test_to_bytes_tolerates_formatting():
    # 无空格、逗号、小写、前后空白
    assert to_bytes("5.65GB") == to_bytes("5.65 GB")
    assert to_bytes("1,024 MB") == to_bytes("1024 MB")
    assert to_bytes("  818 mb  ") == to_bytes("818 MB")


def test_to_bytes_unknown_returns_none_not_zero():
    """认不出就 None —— 返回 0 会让「未知体积」混进小文件里。"""

    for bad in [None, "", "   ", "未知", "abc", "N/A", "-1 GB"]:
        assert to_bytes(bad) is None, "{} 应为 None".format(repr(bad))


def test_sort_key_orders_large_first():
    """按体积从大到小；认不出的垫底。"""

    items = ["818 MB", "5.65 GB", "5.51 GB", "490 MB", "未知"]

    got = sorted(items, key=sort_key)

    assert got == ["5.65 GB", "5.51 GB", "818 MB", "490 MB", "未知"]


def test_sort_key_matches_numeric_order():
    """与「换算成字节后降序」完全一致 —— 这是它存在的全部意义。"""

    items = ["818 MB", "692 MB", "5.65 GB", "5.51 GB", "5.40 GB", "490 MB"]

    by_key = sorted(items, key=sort_key)

    by_bytes = sorted(items, key=to_bytes, reverse=True)

    assert by_key == by_bytes


def test_sql_text_order_is_actually_wrong():
    """固化作恶的旧行为，免得有人「顺手优化」回 SQL 排序。

    字典序下 `'818 MB' > '5.65 GB'`（'8' > '5'），所以旧的
    `ORDER BY size_text DESC` 把 818 MB 排在最前 —— 与体积无关。
    """

    items = ["818 MB", "692 MB", "5.65 GB", "5.51 GB", "5.40 GB"]

    naive = sorted(items, reverse=True)

    assert naive[0] == "818 MB", "字典序确实把 818 MB 排第一"

    correct = sorted(items, key=sort_key)

    assert correct[0] == "5.65 GB", "按字节排才轮到最大的 5.65 GB"

    assert naive != correct, "两种排序结果必须不同，否则这个修复没有意义"
