# -*- coding: utf-8 -*-
"""把前端（node）测试挂进 pytest，一次跑全。

前端的大小滑块映射有 node 测试锁住（tests/size_filter.test.js）——
那些用例覆盖的是「标签写 0MB 实际全放行」「≥3GB 按钮应用成 5000MB」
这类只在界面上显形、后端测试看不见的 bug。

本机没有 node 时跳过，不让它挡住后端测试。
"""

import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_frontend_size_filter():
    """跑 node 里的 size_filter 测试套件。"""

    proc = subprocess.run(
        ["node", "tests/size_filter.test.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    out = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"前端测试失败：\n{out}"

    # 确认它真的跑了用例，而不是静默 0 个（防"空测试"）
    assert "passed" in out, f"没看到测试统计：\n{out}"

    line = [l for l in out.splitlines() if "passed" in l][-1]

    n = int(line.strip().split()[0])

    assert n >= 15, f"用例数只有 {n}，疑似被裁剪：\n{out}"
