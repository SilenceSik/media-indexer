# -*- coding: utf-8 -*-
"""并发批量抓取 测试。

重点验证三件事：
  1. **每线程独立 DB 连接** —— SQLite 连接跨线程用会抛错，必须自建
  2. 结果**按输入顺序**排回来（并发完成顺序是乱的）
  3. 停止信号能刹住；单个失败不影响整批
"""

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.batch_enrich import is_not_found, run_batch   # noqa: E402


class FakeSvc:
    """假服务：记录用过它的线程 id，验证「每线程自建」。"""

    def __init__(self, store, db):
        self.store = store
        self.db = db

    def enrich(self, number, want="all"):

        self.store.append((number, threading.get_ident()))

        if number == "BOOM":
            raise RuntimeError("故意炸")

        if number == "MISS":
            return {"number": number, "error": "找不到番号: MISS"}

        return {"number": number, "magnets": 3, "error": None}


class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")


def make_factory(store):
    def factory():
        return FakeSvc(store, FakeDB())
    return factory


# ─────────────────────── 基本行为

def test_runs_every_number():
    store = []

    rs = run_batch(["A", "B", "C"], make_factory(store), workers=3)

    assert len(rs) == 3
    assert [r["number"] for r in rs] == ["A", "B", "C"]
    assert sorted(n for n, _ in store) == ["A", "B", "C"]


def test_results_ordered_by_input_not_completion():
    """并发完成顺序是乱的，结果必须按**输入顺序**排回来。

    否则调用方按序展示时会错位。
    """

    store = []

    nums = ["S{:02d}".format(i) for i in range(12)]

    rs = run_batch(nums, make_factory(store), workers=6)

    assert [r["number"] for r in rs] == nums


def test_distinct_db_connection_per_task():
    """每个任务要有自己的 DB 连接实例。

    SQLite 连接默认 `check_same_thread=True`，跨线程共用会抛
    `ProgrammingError`。用工厂自建就避开了。
    """

    store = []

    svcs = []

    def factory():
        s = FakeSvc(store, FakeDB())
        svcs.append(s)
        return s

    run_batch(["A", "B", "C", "D"], factory, workers=4)

    assert len(svcs) == 4, "每个任务都该新建服务"

    # 连接对象互不相同
    conns = [id(s.db.conn) for s in svcs]
    assert len(set(conns)) == len(conns)


def test_one_failure_does_not_break_batch():
    """单个炸掉不能影响整批。"""

    store = []

    rs = run_batch(["A", "BOOM", "C"], make_factory(store), workers=3)

    by = {r["number"]: r for r in rs}

    assert by["A"]["error"] is None
    assert "RuntimeError" in by["BOOM"]["error"]
    assert by["C"]["error"] is None


def test_service_creation_failure_isolated():
    """建服务本身失败（比如连不上库）也要兜住，不是整批崩。"""

    calls = [0]

    def factory():
        calls[0] += 1
        if calls[0] % 2 == 0:
            raise RuntimeError("库连不上")
        return FakeSvc([], FakeDB())

    rs = run_batch(["A", "B", "C", "D"], factory, workers=2)

    assert len(rs) == 4
    assert all(r is not None for r in rs)


# ─────────────────────── 进度与停止

def test_progress_callback_counts():
    store = []
    seen = []

    run_batch(["A", "B", "C"], make_factory(store), workers=3,
              on_progress=lambda d, t, r: seen.append((d, t)))

    assert len(seen) == 3
    assert sorted(d for d, _ in seen) == [1, 2, 3]
    assert all(t == 3 for _, t in seen), "总数要一直是 3"


def test_should_stop_halts_dispatch():
    """停止信号生效后不该再派发新任务。"""

    store = []
    flag = {"stop": False}

    def stopper():
        return flag["stop"]

    # 一开始就要求停 -> 应当没有任务真的跑
    flag["stop"] = True

    rs = run_batch(["A", "B", "C"], make_factory(store), workers=2,
                   should_stop=stopper)

    assert store == [], "已停止就不该派发"
    assert all("停止" in (r.get("error") or "") or
               "未执行" in (r.get("error") or "") for r in rs)


def test_empty_input():
    assert run_batch([], make_factory([]), workers=3) == []


def test_workers_clamped_to_at_least_one():
    """workers=0 不能把线程池搞崩。"""

    store = []

    rs = run_batch(["A"], make_factory(store), workers=0)

    assert len(rs) == 1


# ─────────────────────── 失败分类

def test_not_found_classification():
    """「番号不存在」与「请求出问题」必须分开。

    混在一起会把「番号本来就没有」误判成「被限流」，
    进而错误地降并发或停下来。
    """

    assert is_not_found("找不到番号: SSIS-800")
    assert is_not_found("404 not found")

    assert not is_not_found("connection reset by peer")
    assert not is_not_found("timeout")
    assert not is_not_found("")
    assert not is_not_found(None)
