"""P2-4 端到端测试：串起「扫描 → 落库 → 质检 → MCP 工具调用」全链路。

为什么需要这一条：P0-1（扫描不落库）/ P0-2（质检连错库）之所以
逃过 104 个单元用例，是因为**没有任何一条测试把四个环节串在同一份
数据上**——每个环节单独看都绿。

本文件直接调用 agent/tools_v2.py 里被 @mcp.tool() 注册的**真实工具
函数**（mcp 2.0.0 装饰后返回原函数），并把 DB / INDEX_DB 重定向到
tmp_path，从而在真实调用路径上验证四个环节共享同一份库。

链路：
    scan_library(folder)  →  落库到 DB
    search_media(number)  →  从**同一份** DB 读回
    check_library_quality() → 对**同一份** DB 做质检

断言的是「跨环节数据一致」，不是单个函数的返回值形状。
"""

import json
import os
import sqlite3

import pytest

import agent.tools_v2 as tools


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_rules():
    with open(
        os.path.join(ROOT, "data", "dictionary.json"),
        encoding="utf-8"
    ) as fh:
        return json.load(fh)["rules"]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把 MCP 工具层指向 tmp 库 —— 四个环节共享这一份。"""

    db_path = str(tmp_path / "library_v2.db")
    index_path = str(tmp_path / "file_index_v2.db")

    monkeypatch.setattr(tools, "DB", db_path)
    monkeypatch.setattr(tools, "INDEX_DB", index_path)

    # scan 用真实规则库（与生产同源）
    monkeypatch.setattr(tools, "load_rules", load_rules)

    return db_path


def make_media(tmp_path, names):
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)

    for name in names:
        (folder / name).write_bytes(b"\x00" * 16)

    return str(folder)


def count_rows(path, table):
    conn = sqlite3.connect(path)

    try:
        return conn.execute(
            "SELECT COUNT(*) FROM %s" % table
        ).fetchone()[0]

    finally:
        conn.close()


# --------------------------------------------------------------- 全链路串通

def test_scan_then_search_then_quality_share_one_db(tmp_path, wired):
    """四个环节必须落在同一份数据上 —— 这是本文件存在的理由。"""

    folder = make_media(
        tmp_path,
        ["ABP-123.mp4", "IPX-456.mp4", "no_number_clip.mp4"]
    )

    # ① 扫描（经 MCP 工具函数，真实调用路径）
    scanned = tools.scan_library(folder=folder)

    assert scanned["failed"] == []

    summary = scanned["folders"][0]

    assert summary["folder"] == folder
    assert summary["files"] == 3
    assert summary["persisted"] == 2          # 有番号的 2 个落库

    # ② 落库真的写进了 MCP 层指向的 DB
    assert count_rows(wired, "titles") == 2
    assert count_rows(wired, "media_files") == 2

    # ③ 检索读回**同一份**库（P0-2 类数据源错位会在这里暴露）
    found = tools.search_media(number="ABP-123")

    assert found["success"] is True
    assert found["data"], "刚扫进去的番号必须能被检索到"

    # ④ 质检跑在**同一份**库上，能看到刚落的数据
    quality = tools.check_library_quality()

    assert quality["success"] is True

    data = quality["data"]

    assert set(data) == {
        "duplicate_files",
        "missing_files",
        "missing_metadata"
    }

    # 刚落库的 2 个 title 都没有 metadata → 质检必须报出来；
    # 若质检连的是另一个空库，这里会是 []（正是 P0-2 的逃逸形态）
    assert sorted(data["missing_metadata"]) == ["ABP-123", "IPX-456"]

    # 文件都在 → 不报缺失
    assert data["missing_files"] == []


# ------------------------------------------- 质检能看到"被删掉的文件"

def test_quality_detects_file_removed_after_scan(tmp_path, wired):
    """扫描后删文件 → 质检必须报 missing —— 跨环节状态变化可见。"""

    folder = make_media(tmp_path, ["ABP-123.mp4"])

    tools.scan_library(folder=folder)

    assert count_rows(wired, "media_files") == 1

    os.remove(os.path.join(folder, "ABP-123.mp4"))

    quality = tools.check_library_quality()

    missing = quality["data"]["missing_files"]

    assert len(missing) == 1
    assert missing[0]["filepath"].endswith("ABP-123.mp4")


# ------------------------------------------------- 幂等：重复扫描不翻倍

def test_rescan_keeps_single_row_per_file(tmp_path, wired):
    """同一批文件扫两遍，库里不出现重复行。"""

    folder = make_media(tmp_path, ["ABP-123.mp4", "IPX-456.mp4"])

    tools.scan_library(folder=folder)

    # 换个 index 库强制重扫（否则原 index 会跳过未变文件）
    tools.INDEX_DB = str(tmp_path / "index2.db")

    tools.scan_library(folder=folder)

    assert count_rows(wired, "media_files") == 2
    assert count_rows(wired, "titles") == 2
