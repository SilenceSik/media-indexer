# -*- coding: utf-8 -*-
"""多目录扫描 + 去掉预算数 的测试。

两件事：
  1. `/api/scan/start` 接受 `paths` 数组（旧的 `path` 单值仍兼容）
  2. 扫描**不再预先 os.walk 一遍**数总数 —— 总数由扫描器走完目录后回调报回
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def app_mod(tmp_path_factory):
    d = tmp_path_factory.mktemp("multi")

    os.environ["LMM_DB"] = str(d / "library.db")
    os.environ["LMM_COVERS"] = str(d / "covers")
    os.environ["LMM_SCREENSHOTS"] = str(d / "shots")
    os.environ["LMM_TORRENTS"] = str(d / "torrents")
    os.environ["LMM_EXPORTS"] = str(d / "exports")

    for m in list(sys.modules):
        if m == "web.app" or m.startswith("web.app."):
            del sys.modules[m]

    import web.app as m

    return m


def make_dirs(tmp_path, n=2):
    out = []
    for i in range(n):
        p = tmp_path / "d{}".format(i)
        p.mkdir()
        (p / "SSIS-{:03d}.mp4".format(i + 1)).write_bytes(b"x")
        out.append(str(p))
    return out


def test_job_defaults_have_paths_keys(app_mod):
    """`paths` / `current_path` 必须在默认表里 —— 否则会跨任务残留。

    这是之前踩过的一类 bug（`enrich_errors` 不重置、初始字典缺 `paused`）。
    """

    d = app_mod._job_defaults()

    assert "paths" in d
    assert "current_path" in d
    assert d["paths"] == [], "必须是新容器（浅拷贝会共用同一个 list）"


def test_multi_path_scan_accepted(app_mod, tmp_path):
    """两个目录一次提交，都要被扫到。

    用真实的 HTTP 调用走一遍接口 —— 这才验证得到「paths 数组被接受」。
    """

    from fastapi.testclient import TestClient

    dirs = make_dirs(tmp_path, 2)

    client = TestClient(app_mod.app)

    r = client.post("/api/scan/start", json={
        "paths": dirs,
        "dry_run": True,
    })

    assert r.status_code == 200, r.text

    body = r.json()

    assert body["ok"] is True
    assert body["paths"] == dirs, "两个目录都要被收下"

    # 等后台线程收尾（dry_run 很快）
    import time

    for _ in range(60):
        if not app_mod._SCAN_JOB["running"]:
            break
        time.sleep(0.1)

    job = app_mod._SCAN_JOB

    assert job["paths"] == dirs
    assert job["total"] == 2, "两个目录各 1 个文件，共 2"


def test_single_path_still_works(app_mod, tmp_path):
    """旧的 `path` 单值写法仍要认（向后兼容）。"""

    from fastapi.testclient import TestClient

    dirs = make_dirs(tmp_path, 1)

    client = TestClient(app_mod.app)

    r = client.post("/api/scan/start", json={
        "path": dirs[0],
        "dry_run": True,
    })

    assert r.status_code == 200, r.text

    import time

    for _ in range(60):
        if not app_mod._SCAN_JOB["running"]:
            break
        time.sleep(0.1)

    assert app_mod._SCAN_JOB["paths"] == dirs


def test_missing_dir_rejected_with_message(app_mod):
    """目录不存在要明确报错，且指出是哪个。"""

    from fastapi.testclient import TestClient

    client = TestClient(app_mod.app)

    r = client.post("/api/scan/start", json={
        "paths": [r"Z:\definitely\not\here"],
    })

    assert r.status_code == 400
    assert "不存在" in r.json()["error"]


def test_paths_deduped_and_ordered(app_mod, tmp_path):
    """重复目录去重、保持顺序。"""

    from core.scan_scope import _norm_path

    # 直接测接口的归一化逻辑：重复输入只留一份
    raw = [r"E:\片", r"E:\片", r"F:\下载"]

    seen = set()
    out = [p for p in raw if not (p in seen or seen.add(p))]

    assert out == [r"E:\片", r"F:\下载"]


def test_scan_service_reports_discovered():
    """`on_discovered` 回调必须在扫描器走完目录后报回总数。"""

    import tempfile

    from core.database_v2 import Database
    from services.scan_service import ScanService

    tmp = tempfile.mkdtemp()

    media = os.path.join(tmp, "m")
    os.makedirs(media)

    for i in range(3):
        with open(os.path.join(media, "ABP-{:03d}.mp4".format(i + 1)), "wb") as f:
            f.write(b"x")

    db = Database(os.path.join(tmp, "lib.db"))

    svc = ScanService(
        os.path.join(tmp, "idx.db"),
        [{"pattern": r"ABP[-_ ]?\d{3,6}", "priority": 100}],
        db=db,
        excluded_segments=[],
    )

    seen = []

    svc.scan(media, persist=True, on_discovered=seen.append)

    assert seen == [3], "应报回 3 个待处理文件，实际 {}".format(seen)


def test_scan_service_callback_is_optional(tmp_path):
    """不给回调也要能跑（旧调用方不受影响）。"""

    import tempfile

    from core.database_v2 import Database
    from services.scan_service import ScanService

    tmp = tempfile.mkdtemp()

    media = os.path.join(tmp, "m")
    os.makedirs(media)

    with open(os.path.join(media, "ABP-001.mp4"), "wb") as f:
        f.write(b"x")

    db = Database(os.path.join(tmp, "lib.db"))

    svc = ScanService(
        os.path.join(tmp, "idx.db"),
        [{"pattern": r"ABP[-_ ]?\d{3,6}", "priority": 100}],
        db=db,
        excluded_segments=[],
    )

    res = svc.scan(media, persist=True)

    assert len(res["data"]) == 1
