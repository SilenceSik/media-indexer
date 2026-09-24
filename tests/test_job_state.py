# -*- coding: utf-8 -*-
"""任务状态归位 测试。

这个文件来自一次真实的 bug 回看：`_reset_job` 手写字段清单，新加字段时
漏改一处，导致

  * `enrich_errors` 从不重置 —— 上一轮的抓取失败列表残留到新一轮；
  * 初始字典缺 `paused` / `min_conf` 等新键 —— 服务刚启动、还没跑过任务时
    `/api/scan/status` 直接 KeyError。

修法是把"归零值"收成单一来源 `_job_defaults()`。下面这些测试锁住它。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 坑：这里**不能**在模块导入期写 os.environ。
#
# 曾经写成 os.environ.setdefault("LMM_DB", <临时路径>)，看着无害，实际是
# 全量测试里最难查的一个污染源：pytest 收集阶段就会导入所有测试模块，
# 于是从收集完那一刻起 LMM_DB 全进程指向一个空临时库，而且不是
# monkeypatch，pytest 不会回滚。之后任何在进程内重新解析库路径的代码
# （尤其浏览器用例起的服务）都会去查那个空库。
#
# 下面的 app_mod 夹具本来就会在导入 web.app 之前设好这些变量，模块级这行
# 是多余的。真要有额外需求，用夹具 + monkeypatch。


@pytest.fixture(scope="module")
def app_mod(tmp_path_factory):
    d = tmp_path_factory.mktemp("jobstate")

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


def test_initial_state_has_every_key(app_mod):
    """服务刚启动、还没跑过任务时，状态里必须啥键都有。

    之前初始字典漏了新字段，前端读 `job.paused` 直接 KeyError。
    """

    d = app_mod._job_defaults()

    for key in ("paused", "stopped", "min_conf", "max_conf",
                "min_size", "max_size", "enrich_errors",
                "skipped_count", "export_result"):
        assert key in d, f"默认表缺 {key}"


def test_reset_job_clears_every_key(app_mod):
    """_reset_job 必须把**所有**键归零，不能漏。"""

    job = app_mod._SCAN_JOB

    # 先把每一项都弄脏
    for k, v in app_mod._job_defaults().items():
        if isinstance(v, int):
            job[k] = 99
        elif isinstance(v, bool):
            job[k] = not v
        elif isinstance(v, list):
            job[k] = ["脏数据"]
        elif isinstance(v, str):
            job[k] = "脏数据"
        else:
            job[k] = "脏数据"

    app_mod._reset_job("E:\\x", False)

    for k, want in app_mod._job_defaults().items():
        if k in ("running", "path", "dry_run", "phase", "started"):
            continue                    # 这几个本来就该被设成新任务的
        got = job[k]
        assert got == want, f"{k} 没归零：{got!r} != {want!r}"


def test_reset_job_clears_stale_enrich_errors(app_mod):
    """上一轮的抓取失败列表不能残留到新一轮。"""

    job = app_mod._SCAN_JOB

    job["enrich_errors"] = ["ABP-001: 超时", "SSIS-002: 找不到番号"]

    app_mod._reset_job("E:\\x", False, enrich=True)

    assert job["enrich_errors"] == [], "上一轮的失败列表残留了"


def test_defaults_returns_fresh_containers(app_mod):
    """默认表每次都要给新容器。

    浅拷贝会让默认表和运行时状态共用同一个 list —— worker append 就污染了
    默认值，下一轮 reset 拿到的还是脏数据（等于没修）。
    """

    a = app_mod._job_defaults()
    b = app_mod._job_defaults()

    assert a is not b
    assert a["files"] is not b["files"]
    assert a["enrich_errors"] is not b["enrich_errors"]
    assert a["unrecognized"] is not b["unrecognized"]

    a["files"].append("x")

    assert b["files"] == [], "默认表被污染了"


def test_reset_job_twice_does_not_accumulate(app_mod):
    """连跑两轮，第二轮不该看到第一轮的文件列表。"""

    job = app_mod._SCAN_JOB

    app_mod._reset_job("E:\\x", False)

    job["files"].append({"number": "ABP-171", "confidence": 100})
    job["unrecognized"].append("junk.mp4")

    app_mod._reset_job("E:\\y", False)

    assert job["files"] == []
    assert job["unrecognized"] == []


def test_status_endpoint_works_before_any_job(app_mod):
    """没跑过任务就查状态，也要能正常返回（不是 500）。"""

    from fastapi.testclient import TestClient

    fresh = app_mod._job_defaults()

    app_mod._SCAN_JOB.clear()
    app_mod._SCAN_JOB.update(fresh)

    client = TestClient(app_mod.app)

    r = client.get("/api/scan/status")

    assert r.status_code == 200

    body = r.json()

    assert body["running"] is False
    assert body["paused"] is False
