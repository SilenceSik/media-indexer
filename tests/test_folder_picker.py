# -*- coding: utf-8 -*-
"""文件夹选择框端点 测试。

真实修过的 bug：端点原本写成 `async def` 且内部 `thread.join()` 等用户选完
（最长 5 分钟）。async def 跑在事件循环上，会把整个服务冻住 —— 用户开着
选择框的期间，连前端的进度轮询都收不到响应。

正确写法是**同步 def**：FastAPI 会把它放进线程池，不挡别人。

下面锁住三件事：
  1. 端点必须是同步函数（不是 coroutine function）
  2. 选择框开着时再来一个请求 → 409，而不是排队或崩
  3. 选择框阻塞期间，别的接口照常响应
"""

import asyncio
import inspect
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def app_mod(tmp_path_factory):
    d = tmp_path_factory.mktemp("picker")

    os.environ["LMM_DB"] = str(d / "library.db")
    os.environ["LMM_COVERS"] = str(d / "covers")
    os.environ["LMM_SCREENSHOTS"] = str(d / "shots")
    os.environ["LMM_TORRENTS"] = str(d / "torrents")
    os.environ["LMM_EXPORTS"] = str(d / "exports")

    for mod in list(sys.modules):
        if mod == "web.app" or mod.startswith("web.app."):
            del sys.modules[mod]

    import web.app as m

    return m


def test_pick_folder_endpoint_is_sync(app_mod):
    """端点必须是同步 def。

    如果哪天有人改回 `async def`，这个测试会挂 —— 那次改动会重新引入
    "开选择框时整个服务卡死" 的 bug。
    """

    fn = app_mod.api_pick_folder

    assert not asyncio.iscoroutinefunction(fn), (
        "api_pick_folder 又变回 async def 了 —— 它内部会阻塞等待用户选择，"
        "async def 会冻住整个事件循环。必须保持同步 def。"
    )

    assert inspect.isfunction(fn) or callable(fn)


def test_pick_folder_returns_selected_path(app_mod, monkeypatch):
    """选中后要把路径带回来。"""

    monkeypatch.setattr(app_mod, "_pick_folder", lambda initial="": r"E:\新下载")

    r = app_mod.api_pick_folder()

    assert r["ok"] is True
    assert r["path"] == r"E:\新下载"


def test_pick_folder_cancel_returns_empty(app_mod, monkeypatch):
    """取消不是错误 —— 返回空路径，前端据此清提示。"""

    monkeypatch.setattr(app_mod, "_pick_folder", lambda initial="": "")

    r = app_mod.api_pick_folder()

    assert r["ok"] is True
    assert r["path"] == ""


def test_pick_folder_error_is_reported(app_mod, monkeypatch):
    """tkinter 起不来（无桌面会话等）要报错，不是 500 崩掉。"""

    def boom(initial=""):
        raise RuntimeError("no display")

    monkeypatch.setattr(app_mod, "_pick_folder", boom)

    r = app_mod.api_pick_folder()

    assert r.status_code == 500

    body = r.body.decode("utf-8")

    assert "no display" in body


def test_concurrent_pick_returns_409(app_mod, monkeypatch):
    """已经有一个选择框开着时，第二个请求要立刻被拒，不能排队。"""

    release = threading.Event()

    def slow_pick(initial=""):
        release.wait(timeout=10)
        return r"E:\x"

    monkeypatch.setattr(app_mod, "_pick_folder", slow_pick)

    first = {}

    def run_first():
        first["r"] = app_mod.api_pick_folder()

    t = threading.Thread(target=run_first)
    t.start()

    # 等第一个真的进到 busy 状态
    for _ in range(100):
        if app_mod._PICK_STATE["busy"]:
            break
        time.sleep(0.02)

    assert app_mod._PICK_STATE["busy"], "第一个没进入 busy"

    second = app_mod.api_pick_folder()

    assert second.status_code == 409

    release.set()
    t.join(timeout=10)

    assert first["r"]["ok"] is True
    assert app_mod._PICK_STATE["busy"] is False, "busy 标志没清干净"


def test_busy_flag_clears_even_on_error(app_mod, monkeypatch):
    """出错也要把 busy 清掉，否则选择框永远打不开了。"""

    def boom(initial=""):
        raise ValueError("x")

    monkeypatch.setattr(app_mod, "_pick_folder", boom)

    app_mod.api_pick_folder()

    assert app_mod._PICK_STATE["busy"] is False


def test_other_endpoints_respond_while_picker_open(app_mod, monkeypatch):
    """选择框开着时，别的接口必须照常响应（这正是当初的 bug）。"""

    from fastapi.testclient import TestClient

    release = threading.Event()

    monkeypatch.setattr(
        app_mod, "_pick_folder",
        lambda initial="": (release.wait(timeout=10), r"E:\x")[1],
    )

    t = threading.Thread(target=app_mod.api_pick_folder, daemon=True)
    t.start()

    for _ in range(100):
        if app_mod._PICK_STATE["busy"]:
            break
        time.sleep(0.02)

    # 关键：选择框还开着，这个请求要能立刻回来
    client = TestClient(app_mod.app)

    started = time.time()

    r = client.get("/api/scan/status")

    elapsed = time.time() - started

    release.set()

    assert r.status_code == 200, "选择框开着时状态接口挂了"
    assert elapsed < 3, f"状态接口被选择框拖住了 {elapsed:.1f}s"
