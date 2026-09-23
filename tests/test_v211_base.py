"""v2.1.1 基础设施修复层 — 验收测试

覆盖 spec 的四组验收标准：
  1. 数据库：v1 拒绝启动 / v2 正常创建 titles|media_files|metadata|file_index
  2. TaskQueue：add -> get -> done
  3. Worker：任务 -> JavDB mock -> metadata 写入
  4. QualityChecker：duplicate_files / missing_files / missing_metadata 同一个库
"""

import json
import os
import shutil
import sqlite3
import tempfile

import pytest

from core.database_v2 import Database
from core.database_guard import DatabaseGuard
from core.task_queue import TaskQueue
from core.metadata_worker import MetadataWorker
from core.quality import QualityChecker
from services.quality_service import QualityService
from services.media_service import MediaService
from services.metadata_service import MetadataService
from adapters.javdb_adapter import JavDBAdapter


@pytest.fixture()
def tmpdir():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_v1(path):
    """造一个 v1 库（含 videos 表）。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE videos (id INTEGER PRIMARY KEY, number TEXT, filename TEXT, filepath TEXT)"
    )
    conn.execute("INSERT INTO videos (number, filename, filepath) VALUES ('ABC-123','a.mp4','X:/a.mp4')")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 1. 数据库

def test_v1_database_is_rejected(tmpdir):
    path = os.path.join(tmpdir, "library.db")
    make_v1(path)

    with pytest.raises(Exception) as e:
        Database(path)

    assert "v1 database detected" in str(e.value)


def test_guard_detects_v1(tmpdir):
    path = os.path.join(tmpdir, "library.db")
    make_v1(path)

    with pytest.raises(Exception):
        DatabaseGuard().check(path)


def test_guard_passes_v2(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    Database(path)

    assert DatabaseGuard().check(path) is True


def test_v2_creates_four_tables(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    Database(path)

    conn = sqlite3.connect(path)
    tables = {
        x[0]
        for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()

    assert {"titles", "media_files", "metadata", "file_index"} <= tables


def test_metadata_table_shape(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)

    cols = {
        r[1]
        for r in db.conn.execute("PRAGMA table_info(metadata)")
    }

    assert {
        "title_id", "title", "cover", "cover_local",
        "release_date", "maker", "actresses", "tags", "updated_time",
    } <= cols


# ------------------------------------------------------------- 2. TaskQueue

def test_task_queue_add_get_done(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    queue = TaskQueue(path)

    queue.add("metadata", {"number": "ABC-123"})

    task = queue.get()
    assert task is not None
    assert task["type"] == "metadata"
    assert task["status"] == "running"
    assert json.loads(task["payload"])["number"] == "ABC-123"

    queue.done(task["id"])

    row = queue.conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task["id"],)
    ).fetchone()
    assert row["status"] == "completed"

    assert queue.get() is None


def test_task_queue_fail(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    queue = TaskQueue(path)

    queue.add("metadata", {"number": "ABC-123"})
    task = queue.get()
    queue.fail(task["id"], "boom")

    row = queue.conn.execute(
        "SELECT status,error FROM tasks WHERE id=?", (task["id"],)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "boom"


def test_task_queue_priority(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    queue = TaskQueue(path)

    queue.add("metadata", {"number": "LOW"}, priority=0)
    queue.add("metadata", {"number": "HIGH"}, priority=9)

    assert json.loads(queue.get()["payload"])["number"] == "HIGH"


def test_task_queue_add_accepts_plain_string(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    queue = TaskQueue(path)

    queue.add("metadata", "ABC-123")

    assert queue.get()["payload"] == "ABC-123"


# ---------------------------------------------------------------- 3. Worker

class MockJavDBClient:
    """替代 javdb CLI。"""

    def __init__(self, payload=None):
        self.payload = payload or {
            "title": "Mock Title",
            "cover": "https://example.invalid/c.jpg",
            "release_date": "2026-01-01",
            "maker": "MockMaker",
            "actresses": ["A", "B"],
            "tags": ["tag1"],
        }
        self.calls = []

    def search(self, number):
        self.calls.append(number)
        return dict(self.payload)


def test_worker_writes_metadata(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)
    queue = TaskQueue(path)

    client = MockJavDBClient()
    javdb = JavDBAdapter(client)
    worker = MetadataWorker(queue, javdb, db)

    queue.add("metadata", {"number": "ABC-123"})
    worker.run_once()

    assert client.calls == ["ABC-123"]

    row = db.conn.execute("SELECT * FROM metadata").fetchone()
    assert row is not None
    assert row["title"] == "Mock Title"
    assert row["maker"] == "MockMaker"
    assert json.loads(row["actresses"]) == ["A", "B"]

    assert queue.get() is None  # 已 done，不再 pending


def test_worker_marks_failed_on_exception(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)
    queue = TaskQueue(path)

    class Boom:
        def search(self, number):
            raise RuntimeError("upstream down")

    worker = MetadataWorker(queue, Boom(), db)
    queue.add("metadata", {"number": "ABC-123"})
    worker.run_once()

    row = queue.conn.execute("SELECT status,error FROM tasks").fetchone()
    assert row["status"] == "failed"
    assert "upstream down" in row["error"]


def test_worker_noop_on_empty_queue(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)
    queue = TaskQueue(path)

    MetadataWorker(queue, MockJavDBClient(), db).run_once()

    assert db.conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 0


def test_metadata_service_enqueues(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    queue = TaskQueue(path)
    service = MetadataService(queue)

    result = service.request("ABC-123")

    assert result["success"] is True
    assert result["data"]["queued"] is True
    assert queue.get() is not None


# ---------------------------------------------------- 4. QualityChecker

def test_quality_checks_same_database(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)

    # 存在的文件 + 缺失的文件 + 重复 hash
    present = os.path.join(tmpdir, "present.mp4")
    open(present, "wb").close()

    db.add_file("ABC-123", present, file_hash="hash-a", size=0)
    db.add_file("ABC-123", os.path.join(tmpdir, "gone.mp4"), file_hash="hash-a", size=0)
    db.add_file("DEF-456", os.path.join(tmpdir, "gone2.mp4"), file_hash=None, size=0)

    # ABC-123 有 metadata，DEF-456 没有
    db.save_metadata({
        "number": "ABC-123",
        "title": "T",
        "cover": "c",
        "release_date": "2026-01-01",
        "maker": "M",
        "actresses": [],
        "tags": [],
    })

    checker = QualityChecker(path)

    dup = checker.duplicate_files()
    assert dup == [{"hash": "hash-a", "count": 2}]

    missing = checker.missing_files()
    assert sorted(x["filepath"] for x in missing) == sorted([
        os.path.join(tmpdir, "gone.mp4"),
        os.path.join(tmpdir, "gone2.mp4"),
    ])

    assert checker.missing_metadata() == ["DEF-456"]


def test_quality_service_report_shape(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)
    db.add_file("ABC-123", os.path.join(tmpdir, "x.mp4"))

    result = QualityService(path).report()

    assert result["success"] is True
    assert set(result["data"]) == {
        "duplicate_files", "missing_files", "missing_metadata"
    }
    assert result["trace_id"]


# ------------------------------------------------------ 5. Service 返回格式

def test_all_services_return_unified_shape(tmpdir):
    path = os.path.join(tmpdir, "library_v2.db")
    db = Database(path)
    queue = TaskQueue(path)

    results = [
        MediaService(path).search("ABC-123"),
        MediaService(path).add_file("ABC-123", "X:/a.mp4"),
        MetadataService(queue).request("ABC-123"),
        QualityService(path).report(),
    ]

    for r in results:
        assert set(r) == {"success", "data", "error", "trace_id"}
        assert r["success"] is True
        assert r["error"] is None
