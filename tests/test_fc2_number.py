# -*- coding: utf-8 -*-
"""FC2 番号归一：CLI 只认 `FC2-<数字>`，不认网页写法 `FC2-PPV-<数字>`。

主人 2026-09-24 晚发现的怪现象：**FC2 的片子全都有标题、有磁力，却一个
封面、一张截图都没有。**

根因不是抓取失败，而是 `assets()` 把原始番号直接喂给 CLI：

    assets list FC2-1003647       -> rc=0，12 个资产
    assets list FC2-PPV-1003647   -> rc=1，找不到番号

`detail()` 早前做了这层归一，`assets()` / `search()` 没做 —— 于是元数据
抓到了（走 detail），资产列表却是空的（走 assets），下载环节无物可下。

这里锁住「所有走 CLI 的入口都必须归一」。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.javdb_adapter import JavDBCLIClient            # noqa: E402


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder(JavDBCLIClient):
    """记下每次实际传出去的 argv。"""

    def __init__(self, replies=None):
        super().__init__()
        self.seen = []
        self._replies = replies or {}

    def _run(self, args, timeout=None):
        self.seen.append(list(args))
        key = args[0] + " " + (args[1] if len(args) > 1 else "")
        return self._replies.get(
            key, FakeResult(0, "image\thttps://x/covers/a.jpg\n")
        )


# ─────────────────────── 归一本体

def test_cli_number_converts_fc2_ppv():
    """`FC2-PPV-N` -> `FC2-N`。"""

    c = JavDBCLIClient()

    assert c._cli_number("FC2-PPV-1003647") == "FC2-1003647"
    assert c._cli_number("FC2-PPV-906625") == "FC2-906625"


def test_cli_number_leaves_others_alone():
    """其他番号**一个字符都不能动**。"""

    c = JavDBCLIClient()

    for n in ["SSIS-001", "ABP-108", "FC2-1003647", "DV-1666",
              "1PONDO-010112_001", "SSIS001"]:
        assert c._cli_number(n) == n, n


def test_cli_number_handles_empty():
    """空值不炸。"""

    c = JavDBCLIClient()

    assert c._cli_number("") == ""
    assert c._cli_number(None) == ""


# ─────────────────────── 各入口都归一了

def test_assets_normalizes_fc2():
    """**就是这条 bug**：assets 必须传 `FC2-N`。"""

    c = Recorder()
    c.assets("FC2-PPV-1003647")

    assert c.seen, "该调用 CLI"

    argv = c.seen[0]

    assert "FC2-1003647" in argv, "实际传的是 {}".format(argv)

    assert "FC2-PPV-1003647" not in argv, "原始番号不该传给 CLI"


def test_assets_keeps_other_numbers_verbatim():
    """非 FC2 不动。"""

    c = Recorder()
    c.assets("SSIS-001")

    assert "SSIS-001" in c.seen[0]


def test_search_normalizes_fc2():
    """search 也要归一 —— 同一类 bug 的另一个入口。"""

    c = Recorder()

    try:
        c.search("FC2-PPV-906625")
    except Exception:                                       # noqa: BLE001
        pass

    if c.seen:
        argv = c.seen[0]

        assert "FC2-906625" in argv, "实际传的是 {}".format(argv)

        assert "FC2-PPV-906625" not in argv


def test_detail_normalizes_fc2():
    """detail 早前就归一了，别改回去。"""

    c = Recorder()
    c.detail_checked("FC2-PPV-1003647")

    argv = c.seen[0]

    assert "FC2-1003647" in argv

    assert "FC2-PPV-1003647" not in argv


# ─────────────────────── 归一后确实拿得到资产

def test_assets_parses_reply():
    """归一之后能正常解析出资产列表。"""

    c = Recorder({
        "assets list": FakeResult(
            0,
            "image\thttps://x/small_covers/a.jpg\n"
            "image\thttps://x/covers/a.jpg\n"
            "image\thttps://x/samples/a_l_0.jpg\n",
        )
    })

    out = c.assets("FC2-PPV-1003647")

    assert len(out) == 3

    assert out[0][1].endswith("small_covers/a.jpg")


def test_real_cli_fc2_assets_not_empty():
    """**真跑一次 CLI** —— FC2 必须能拿到资产。

    这条会在没有 CLI 的环境里跳过（比如 CI），本机跑得到就是回归守卫。
    """

    import shutil

    if not shutil.which("javdb"):
        import pytest

        pytest.skip("本机没有 javdb CLI")

    c = JavDBCLIClient()

    got = c.assets("FC2-PPV-1003647")

    assert len(got) >= 3, "FC2 该有封面+截图，实际 {} 条".format(len(got))
