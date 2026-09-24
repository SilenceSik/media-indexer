# -*- coding: utf-8 -*-
"""测试自身的磁盘安全护栏。

这条规则来自一次真实事故：为了测文件大小过滤，测试里造了真·GB 级的文件
（最大一个 12 GB）。一轮跑掉 16.5 GB，而 pytest 默认保留最近 3 轮临时目录
—— 累计 57 GB，直接把 C 盘写满（剩 3.2 GB，系统告警）。

过滤逻辑跟"字节数有多大"无关，50 字节和 50 GB 走的是同一条分支。要"看起来
真实"时，该改的是**门槛**，不是文件。

下面这条护栏扫全项目的测试代码，发现"造大文件"的写法就失败。
"""

import io
import os
import re
import tokenize

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TESTS = os.path.join(ROOT, "tests")

# 单次写入超过这个量就值得警惕（字节）
LIMIT = 8 * 1024 * 1024        # 8 MB


def _test_files():
    for name in sorted(os.listdir(TESTS)):
        if name.endswith(".py") and name.startswith("test_"):
            yield name, os.path.join(TESTS, name)


def _code_only(path):
    """只取真实代码，去掉注释与字符串字面量。

    本文件自己的文档字符串里就写着被禁的写法当例子 —— 不过滤注释，
    护栏会抓到自己。
    """

    out = []

    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)

    return " ".join(out)


def test_no_gigabyte_scale_test_fixtures():
    """测试里不许出现 GB 级的文件写入。"""

    bad = []

    for name, path in _test_files():
        src = _code_only(path)

        for m in re.finditer(r"\*\s*(\d{4,})\s*\*\s*1024\s*\*\s*1024", src):
            bad.append((name, m.group(0)))

        for m in re.finditer(r"\*\s*1024\s*\*\s*1024\s*\*\s*(\d{4,})", src):
            bad.append((name, m.group(0)))

        # seek 到 GB 级偏移再写 —— Windows 上这不是稀疏文件，NTFS 会填零
        for m in re.finditer(r"\.seek \(\s*(\d+)\s*\*\s*1024\s*\*\s*1024", src):
            if int(m.group(1)) * 1024 * 1024 > LIMIT:
                bad.append((name, m.group(0)))

    assert not bad, (
        "发现 GB 级测试文件，会写满磁盘：\n"
        + "\n".join(f"  {n}: {s}" for n, s in bad)
        + "\n\n过滤逻辑与字节数无关 —— 改门槛，别改文件大小。"
    )


def test_no_unbounded_bytes_multiplication():
    """写文件时不许出现大到离谱的字节倍数。"""

    bad = []

    for name, path in _test_files():
        src = _code_only(path)

        # b"..." * 字面量  —— 只允许小字面量
        for m in re.finditer(r"\*\s*(\d+)\b", src):
            pass        # 交给下面更精确的两条

        # 形如 * 1024 * 1024 * N 或 N * 1024 * 1024
        for m in re.finditer(r"(\d{3,})\s*\*\s*1024\s*\*\s*1024", src):
            if int(m.group(1)) * 1024 * 1024 > LIMIT:
                bad.append((name, m.group(0)))

        for m in re.finditer(r"1024\s*\*\s*1024\s*\*\s*(\d{3,})", src):
            if int(m.group(1)) * 1024 * 1024 > LIMIT:
                bad.append((name, m.group(0)))

    assert not bad, "可疑的大文件写入：\n" + "\n".join(f"  {n}: {s}" for n, s in bad)


def test_pytest_tmpdir_does_not_accumulate():
    """pytest 保留的临时目录不该堆到 GB 级。

    保留策略是 pytest 自己的行为，这条只能事后检查 —— 但至少能让"又堆起来"
    这件事被看见，而不是等磁盘满了才发现。
    """

    import glob

    base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP", "/tmp"),
        "Temp",
    )

    dirs = glob.glob(os.path.join(base, "pytest-of-*"))

    if not dirs:
        pytest.skip("没有 pytest 临时目录")

    total = 0

    for d in dirs:
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass

    # 1 GB 以下算正常（大量小文件也会有些体积）
    assert total < 1024 ** 3, (
        f"pytest 临时目录已堆到 {total / 1024 ** 3:.1f} GB —— "
        f"多半是某个测试在造大文件。检查 tests/ 下的写入逻辑。"
    )
