# -*- coding: utf-8 -*-
"""歧义番号处理 + 封面路径形状 + 任务控制 测试。

这三个都是实测踩出来的：
  * 同番号多个精确匹配被当 notfound -> 整批抓取落库 0
  * 封面只存 basename -> 全站封面撞名
  * 长任务没有暂停/停止
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.javdb_adapter import JavDBCLIClient              # noqa: E402
from web import app as web_app                                 # noqa: E402


class FakeRun:
    """假 subprocess.run 结果。"""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


# ─────────────────────── 歧义番号（落库 0 的根因）

def test_detail_ambiguous_uses_search_then_id(monkeypatch):
    """`detail <番号>` 报「多个精确匹配」时，走 search + detail --id。

    实测 ABF-001 / ABF-087 这类号必然撞这个，早前当作 notfound
    直接返回 None，导致整批抓取落库 0。
    """

    client = JavDBCLIClient()

    calls = []

    def fake_run(args, timeout=None):
        calls.append(args)

        if args[:2] == ["detail", "ABF-001"]:
            return FakeRun("番号 ABF-001 有多个精确匹配", returncode=1)

        if args[:2] == ["search", "ABF-001"]:
            return FakeRun(json.dumps({"movies": [
                {"id": "idA", "number": "ABF-001"},
                {"id": "idB", "number": "ABF-001"},
            ]}))

        if args[:3] == ["detail", "idA", "--id"]:
            return FakeRun(json.dumps({
                "number": "ABF-001", "title": "A",
                "magnets": [{"hash": "h1", "name": "m1"}],
            }))

        if args[:3] == ["detail", "idB", "--id"]:
            return FakeRun(json.dumps({
                "number": "ABF-001", "title": "B",
                "magnets": [{"hash": "h2", "name": "m2"}],
            }))

        return FakeRun("", returncode=1)

    monkeypatch.setattr(client, "_run", fake_run)

    got = client.detail("ABF-001")

    assert got is not None, "歧义番号不得被当作查不到"
    assert got["ambiguous_versions"] == 2
    hashes = {m["hash"] for m in got["magnets"]}
    assert hashes == {"h1", "h2"}, "两个版本的磁力都要拿到"


def test_detail_ambiguous_dedupes_magnets(monkeypatch):
    """多版本里重复的磁力按 hash 去重。"""

    client = JavDBCLIClient()

    def fake_run(args, timeout=None):
        if args[:2] == ["detail", "X-1"]:
            return FakeRun("多个精确匹配", returncode=1)
        if args[:2] == ["search", "X-1"]:
            return FakeRun(json.dumps({"movies": [
                {"id": "a", "number": "X-1"},
                {"id": "b", "number": "X-1"},
            ]}))
        if args[:2] == ["detail", "a"]:
            return FakeRun(json.dumps({"magnets": [{"hash": "same"}, {"hash": "onlyA"}]}))
        if args[:2] == ["detail", "b"]:
            return FakeRun(json.dumps({"magnets": [{"hash": "same"}]}))
        return FakeRun("", returncode=1)

    monkeypatch.setattr(client, "_run", fake_run)

    got = client.detail("X-1")

    assert len(got["magnets"]) == 2


def test_search_exact_ids_filters_fuzzy_results(monkeypatch):
    """search 返回模糊结果时必须过滤 —— 否则会把别的番号的磁力并进来。

    实测搜 `ABF-001` 返回 20 条，只有前 2 条 number 真的是 ABF-001，
    其余是 ABF-008 / ABF-010 / ABF-031…
    """

    client = JavDBCLIClient()

    movies = [
        {"id": "9bx8", "number": "ABF-001"},
        {"id": "76MM91", "number": "ABF-001"},
        {"id": "de3w8", "number": "ABF-008"},
        {"id": "35Da3", "number": "ABF-010"},
        {"id": "Oymz", "number": "ABF-011"},
    ]

    monkeypatch.setattr(
        client, "_run",
        lambda args, timeout=None: FakeRun(json.dumps({"movies": movies})),
    )

    assert client._search_exact_ids("ABF-001") == ["9bx8", "76MM91"]


def test_detail_ambiguous_only_merges_exact_versions(monkeypatch):
    """合并磁力时只取精确匹配版本，不得混入模糊结果。"""

    client = JavDBCLIClient()

    movies = [
        {"id": "a", "number": "X-1"},
        {"id": "other", "number": "X-9"},          # 模糊结果，不能要
    ]

    def fake_run(args, timeout=None):
        if args[0] == "detail" and args[1] == "X-1":
            return FakeRun("多个精确匹配", returncode=1)
        if args[0] == "search":
            return FakeRun(json.dumps({"movies": movies}))
        if args[:2] == ["detail", "a"]:
            return FakeRun(json.dumps({"magnets": [{"hash": "good"}]}))
        if args[:2] == ["detail", "other"]:
            raise AssertionError("不得抓取模糊匹配的番号")
        return FakeRun("", returncode=1)

    monkeypatch.setattr(client, "_run", fake_run)

    got = client.detail("X-1")

    assert got["ambiguous_versions"] == 1
    assert [m["hash"] for m in got["magnets"]] == ["good"]


def test_detail_real_notfound_still_none(monkeypatch):
    """真的查不到（不是歧义）仍返回 None。"""

    client = JavDBCLIClient()

    monkeypatch.setattr(
        client, "_run",
        lambda args, timeout=None: FakeRun("找不到番号: NOPE-999", returncode=1),
    )

    assert client.detail("NOPE-999") is None


def test_assets_ambiguous_uses_detail_fields(monkeypatch):
    """歧义番号走 detail 字段回退拼 URL。

    为什么不能用 `assets list <id>`：该子命令只吃番号、没有 `--id` 参数
    （实测 `unknown flag: --id`），传 id 也会 `找不到番号`。
    """

    client = JavDBCLIClient()

    def fake_run(args, timeout=None):
        if args[:2] == ["assets", "list"]:
            return FakeRun("番号 ABF-001 有多个精确匹配", returncode=1)
        if args[:2] == ["search", "ABF-001"]:
            return FakeRun(json.dumps({"movies": [{"id": "idA", "number": "ABF-001"}]}))
        if args[:2] == ["detail", "idA"]:
            return FakeRun(json.dumps({
                "cover_url": "http://x/cover.jpg",
                "preview_images": [
                    {"large_url": "http://x/s1.jpg"},
                    {"large_url": "http://x/s2.jpg"},
                ],
            }))
        return FakeRun("", returncode=1)

    monkeypatch.setattr(client, "_run", fake_run)

    got = client.assets("ABF-001", "image")

    urls = [u for _, u in got]

    assert "http://x/cover.jpg" in urls
    assert "http://x/s1.jpg" in urls
    assert len(got) == 3


def test_assets_ambiguous_picks_version_with_most_previews(monkeypatch):
    """多个版本时挑**截图最多**的当正身。

    实测有 preview_images 的版本不一定是第一个：
      ABF-001: 76MM91 有 11 张 / 9bx8 有 0 张
      ABF-087: VwGnq 有 0 张  / NQwPYw 有 13 张
    取第一个会拿到没有截图的那版。
    """

    client = JavDBCLIClient()

    def fake_run(args, timeout=None):
        if args[:2] == ["assets", "list"]:
            return FakeRun("多个精确匹配", returncode=1)
        if args[:2] == ["search", "ABF-087"]:
            return FakeRun(json.dumps({"movies": [
                {"id": "empty", "number": "ABF-087"},
                {"id": "rich", "number": "ABF-087"},
            ]}))
        if args[:2] == ["detail", "empty"]:
            return FakeRun(json.dumps({
                "cover_url": "http://x/empty-cover.jpg",
                "preview_images": [],
            }))
        if args[:2] == ["detail", "rich"]:
            return FakeRun(json.dumps({
                "cover_url": "http://x/rich-cover.jpg",
                "preview_images": [
                    {"large_url": "http://x/a.jpg"},
                    {"large_url": "http://x/b.jpg"},
                    {"large_url": "http://x/c.jpg"},
                ],
            }))
        return FakeRun("", returncode=1)

    monkeypatch.setattr(client, "_run", fake_run)

    urls = [u for _, u in client.assets("ABF-087", "image")]

    assert "http://x/rich-cover.jpg" in urls, "应选截图多的那版"
    assert "http://x/empty-cover.jpg" not in urls
    assert len(urls) == 4


# ─────────────────────── 封面路径形状

def test_cover_filename_keeps_subdirectory():
    """带番号子目录的封面路径要保留层级，不能只留 basename。"""

    assert web_app.cover_filename("ABP-041/image-002.jpg") == "ABP-041/image-002.jpg"


def test_cover_filename_handles_flat_layout():
    """早期扁平布局（番号做前缀）也要能出图。"""

    assert web_app.cover_filename("ABP-041-image-002.jpg") == "ABP-041-image-002.jpg"


def test_cover_filename_blocks_traversal():
    """目录穿越必须挡掉。"""

    assert web_app.cover_filename("../../etc/passwd") == "etc/passwd"


def test_cover_filename_windows_backslash():
    assert web_app.cover_filename(r"ABP-041\image-002.jpg") == "ABP-041/image-002.jpg"


def test_screenshot_files_keeps_subdirectory():
    """截图同理：两个番号的 image-001.jpg 不能撞成一个。"""

    raw = json.dumps(["ABP-041/image-001.jpg", "SSIS-001/image-001.jpg"])

    got = web_app.screenshot_files(raw)

    assert got == ["ABP-041/image-001.jpg", "SSIS-001/image-001.jpg"]


# ─────────────────────── 任务控制

def test_control_routes_registered():
    paths = {r.path for r in web_app.app.routes}

    assert "/api/scan/pause" in paths
    assert "/api/scan/resume" in paths
    assert "/api/scan/stop" in paths


def test_stop_without_job_reports_clearly():
    """没任务在跑时，停止/暂停要明确说明而不是静默成功。"""

    with web_app._SCAN_LOCK:

        web_app._SCAN_JOB["running"] = False

    assert web_app.scan_stop()["ok"] is False
    assert web_app.scan_pause()["ok"] is False
    assert web_app.scan_resume()["ok"] is False


def test_pause_resume_toggles_flag():
    with web_app._SCAN_LOCK:

        web_app._SCAN_JOB["running"] = True

    try:

        assert web_app.scan_pause()["ok"] is True

        assert web_app._SCAN_PAUSE.is_set() is True

        assert web_app.scan_resume()["ok"] is True

        assert web_app._SCAN_PAUSE.is_set() is False

    finally:

        with web_app._SCAN_LOCK:

            web_app._SCAN_JOB["running"] = False

        web_app._SCAN_PAUSE.clear()

        web_app._SCAN_STOP.clear()


def test_reset_job_clears_stale_control_flags():
    """新任务必须清掉上一轮的停止/暂停标志，否则新任务一启动就停。"""

    web_app._SCAN_STOP.set()

    web_app._SCAN_PAUSE.set()

    web_app._reset_job("(test)", False, False)

    assert web_app._SCAN_STOP.is_set() is False
    assert web_app._SCAN_PAUSE.is_set() is False
    assert web_app._SCAN_JOB["stopped"] is False
    assert web_app._SCAN_JOB["paused"] is False

    with web_app._SCAN_LOCK:

        web_app._SCAN_JOB["running"] = False
