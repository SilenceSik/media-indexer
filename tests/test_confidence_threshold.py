# -*- coding: utf-8 -*-
"""落库置信度门槛 + SQLite WAL 并发设置 测试。

背景（两个实测问题）：
  * `kcf9.com-3 (1)_(new)_amq13.mp4` 被 P_STD 当成 AMQ-13（conf 70），
    没有门槛时照样入库 —— 用户看到的"错误匹配"。阈值就是这道闸。
  * 抓取时页面卡顿：默认 journal_mode=delete 读写互斥。
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from core.scanner_v2 import resolve_extensions                 # noqa: E402
from services.scan_service import ScanService                  # noqa: E402

RULES = [
    {"pattern": r"ABP[-_ ]?\d{3,6}", "priority": 100},
]


@pytest.fixture
def svc(tmp_path):
    db = Database(str(tmp_path / "lib.db"))

    return ScanService(
        str(tmp_path / "idx.db"),
        RULES,
        db=db,
    ), db, tmp_path


# ─────────────────────── 置信度门槛

def test_default_threshold_collects_everything(svc):
    """默认 0-100 = 全收，行为与加阈值之前一致。"""

    s, db, tmp = svc

    (tmp / "AMQ-13-test.mp4").write_bytes(b"x")

    s.scan(str(tmp), persist=True)

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1


def test_high_threshold_skips_guess(svc):
    """min=90 时，70 分的猜测不得入库 —— 这正是 AMQ-13 的场景。"""

    s, db, tmp = svc

    (tmp / "kcf9.com-3 (1)_(new)_amq13.mp4").write_bytes(b"x")

    res = s.scan(str(tmp), persist=True, min_conf=90)

    rows = res["data"]

    assert len(rows) == 1
    assert rows[0]["persisted"] is False
    assert rows[0]["skipped"]["number"] == "AMQ-13"
    assert rows[0]["skipped"]["confidence"] == 70

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 0


def test_high_threshold_keeps_trusted(svc):
    """可信档（字典规则 priority=100）不受高门槛影响。"""

    s, db, tmp = svc

    (tmp / "ABP-171.mp4").write_bytes(b"x")

    s.scan(str(tmp), persist=True, min_conf=90)

    assert db.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1


def test_range_can_target_only_guesses(svc):
    """70-70 区间专门捞出待人工确认的猜测。"""

    s, db, tmp = svc

    (tmp / "kcf9.com-3 (1)_(new)_amq13.mp4").write_bytes(b"x")
    (tmp / "ABP-171.mp4").write_bytes(b"x")

    s.scan(str(tmp), persist=True, min_conf=70, max_conf=70)

    numbers = {
        r[0]
        for r in db.conn.execute(
            "SELECT t.number FROM media_files m JOIN titles t ON t.id=m.title_id"
        )
    }

    assert numbers == {"AMQ-13"}, f"应只收 70 分的，实际 {numbers}"


def test_skipped_records_reason(svc):
    """被挡下时要记明原因，否则用户只看到"扫到了但没进库"。"""

    s, db, tmp = svc

    (tmp / "kcf9.com-3 (1)_(new)_amq13.mp4").write_bytes(b"x")

    res = s.scan(str(tmp), persist=True, min_conf=90)

    reason = res["data"][0]["skipped"]["reason"]

    assert "90" in reason and "100" in reason


def test_dry_run_has_no_skipped(svc):
    """dry-run 不落库，也就无所谓"挡下"。"""

    s, db, tmp = svc

    (tmp / "kcf9.com-3 (1)_(new)_amq13.mp4").write_bytes(b"x")

    res = s.scan(str(tmp), persist=False, min_conf=90)

    assert res["data"][0]["persisted"] is False
    assert res["data"][0]["skipped"] is None


def test_best_entry_returns_confidence(svc):
    """best_entry 要带回 confidence（门槛判断依赖它）。"""

    s, db, tmp = svc

    entry = s.best_entry([
        {"number": "A-1", "confidence": 60},
        {"number": "B-2", "confidence": 90},
    ])

    assert entry["number"] == "B-2"
    assert entry["confidence"] == 90

    assert s.best_entry([]) is None


# ─────────────────────── WAL（抓取时不卡顿）

def test_database_uses_wal(tmp_path):
    """库必须是 WAL 模式 —— delete 模式下读写互斥，抓取时页面会卡。"""

    db = Database(str(tmp_path / "w.db"))

    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"


def test_database_busy_timeout_set(tmp_path):
    db = Database(str(tmp_path / "w2.db"))

    timeout = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert timeout >= 5000


def test_read_works_while_another_conn_writes(tmp_path):
    """并发读写不互相阻塞（WAL 的核心收益）。

    用一个连接持有写事务，另一个连接读取 —— delete 模式下这会
    抛 database is locked，WAL 下应能正常读到旧数据。
    """

    path = str(tmp_path / "conc.db")

    db = Database(path)

    db.add_file("ABP-171", str(tmp_path / "x.mp4"))

    writer = sqlite3.connect(path)

    writer.execute("BEGIN IMMEDIATE")

    writer.execute("INSERT INTO titles (number) VALUES ('ZZZ-999')")

    try:

        reader = sqlite3.connect(path, timeout=2.0)

        # 不应该抛 locked
        rows = reader.execute("SELECT COUNT(*) FROM titles").fetchone()

        assert rows[0] >= 1

        reader.close()

    finally:

        writer.rollback()

        writer.close()
