# -*- coding: utf-8 -*-
"""人类可读体积文本 -> 字节数。

存在的唯一理由：`magnets.size_text` 是**文本**（`"5.65 GB"` / `"818 MB"`），
在 SQL 里按它排序走的是**字典序** —— `'8' > '5'`，于是 818 MB 排在
5.65 GB 前面，整个「按体积排序」是坏的（2026-09-24 实测 ABP-171 的
76 条磁力）。要按体积排，就必须先换算成数字。

**认不出来就返回 None**，不猜、不编一个近似值 —— 排序时 None 归末尾。
"""

# 长的排前面：`"818MB".endswith("B")` 也成立，先匹配 MB 才不会只剥掉 B。
UNITS = (
    ("TB", 1024 ** 4),
    ("GB", 1024 ** 3),
    ("MB", 1024 ** 2),
    ("KB", 1024),
    ("B", 1),
)

__all__ = ["to_bytes", "sort_key"]


def to_bytes(text):
    """`"5.65 GB"` -> 字节数。空、认不出、负数一律 None。"""

    if text is None:

        return None

    s = str(text).strip().upper().replace(",", "").replace(" ", "")

    if not s:

        return None

    mult = 1

    for unit, k in UNITS:

        if s.endswith(unit):

            mult = k

            s = s[: -len(unit)]

            break

    try:

        v = float(s)

    except ValueError:

        return None

    if v < 0:

        return None

    return int(v * mult)


def sort_key(text):
    """给 `sorted(..., key=...)` 用：按体积**从大到小**。

    返回负数，所以直接 `sorted(ms, key=lambda m: sort_key(m["size_text"]))`
    就是从大到小。认不出的垫底（不是当成 0 混进小文件里）。
    """

    b = to_bytes(text)

    if b is None:

        return (1, 0)

    return (0, -b)
