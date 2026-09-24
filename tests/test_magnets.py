"""C5 磁力 / 可删数据落点（D5）— 验收测试

对应的四组断言：
  1. magnets 表建立成功（列 / 唯一索引与 D5 一致）
  2. add_magnet 幂等（重复落同一条不新增行）
  3. magnets_for_title 返回正确
  4. deletable_titles() 能真实算出本地占用（造 2 文件 + 1 磁力，断言总字节数）
  5. 对真实 verify_cache.json 跑导入：条数 > 0 且二次运行条数不变
"""

import importlib.util
import os
import sqlite3

import pytest

from core.database_v2 import Database

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REAL_CACHE = r"X:\corpus\verify_cache.json"

REAL_LIBRARY_DB = r"X:\corpus\library.db"


def load_importer():
    """按路径直接载入 tools/import_verified_magnets.py（tools/ 不是包）。"""

    path = os.path.join(ROOT, "tools", "import_verified_magnets.py")

    spec = importlib.util.spec_from_file_location(
        "import_verified_magnets",
        path,
    )

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    return module


def count(path, table):
    conn = sqlite3.connect(path)

    try:

        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    finally:

        conn.close()


@pytest.fixture()
def library_db(tmp_path):
    return str(tmp_path / "library_v2.db")


# ------------------------------------------------------------ 1. 建表

def test_magnets_table_created(tmp_path, library_db):
    db = Database(library_db)

    tables = {
        x[0]
        for x in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert "magnets" in tables

    cols = {
        r[1]: r for r in db.conn.execute("PRAGMA table_info(magnets)")
    }

    # D5 的四列必须都在；此外 D9/D11 落地时新增了三列判定字段
    # （is_correct / magnet_hash / name）—— 用子集断言，避免以后再加列
    # 又得改一次这条测试（它是「表建对了」的契约，不是「列一个不多」的契约）。
    assert {
        "id", "title_id", "magnet", "source", "size_text", "verified", "created_time",
    } <= set(cols)

    # D9/D11：单条磁力的判定结果
    assert {"is_correct", "magnet_hash", "name"} <= set(cols)

    # D5：magnet 是 NOT NULL
    assert cols["magnet"][3] == 1

    # 原有四表未被改动
    assert {"titles", "media_files", "metadata", "file_index"} <= tables

    indexes = {
        x[0]
        for x in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }

    assert "idx_magnet_title" in indexes


def test_create_is_idempotent(tmp_path, library_db):
    """重复实例化同一个库，不会因为 IF NOT EXISTS 报错、也不重复建表。"""

    Database(library_db)
    Database(library_db)

    assert count(library_db, "magnets") == 0


# ------------------------------------------------------ 2. add_magnet 幂等

def test_add_magnet_is_idempotent(tmp_path, library_db):
    db = Database(library_db)

    first = db.add_magnet(
        "ABP-171",
        "magnet:?xt=urn:btih:aaa",
        source="verify_cache",
        size_text="4.2GB",
        verified=1,
    )

    assert first["created"] is True

    again = db.add_magnet(
        "ABP-171",
        "magnet:?xt=urn:btih:aaa",
        source="verify_cache",
        size_text="4.2GB",
        verified=1,
    )

    assert again["created"] is False
    assert again["id"] == first["id"]
    assert again["title_id"] == first["title_id"]

    assert count(library_db, "magnets") == 1

    # 同番号的另一条磁力 → 新行，挂在同一个 title 上
    other = db.add_magnet(
        "ABP-171",
        "magnet:?xt=urn:btih:bbb",
        verified=1,
    )

    assert other["created"] is True
    assert other["title_id"] == first["title_id"]
    assert count(library_db, "magnets") == 2

    # 番号复用：不同番号的 title_id 必须不同
    assert db.get_or_create_title("IPX-999") != first["title_id"]


def test_add_magnet_repeat_does_not_lower_verified(tmp_path, library_db):
    db = Database(library_db)

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", verified=1)

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", verified=0)

    row = db.conn.execute(
        "SELECT verified FROM magnets WHERE magnet='magnet:?xt=urn:btih:aaa'"
    ).fetchone()

    assert row["verified"] == 1

    assert count(library_db, "magnets") == 1


# ----------------------------------------------------- 3. magnets_for_title

def test_magnets_for_title_returns_rows(tmp_path, library_db):
    db = Database(library_db)

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", size_text="4.2GB", verified=1)
    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:bbb", size_text="5.0GB", verified=1)
    db.add_magnet("IPX-999", "magnet:?xt=urn:btih:ccc")

    rows = db.magnets_for_title("ABP-171")

    assert [r["magnet"] for r in rows] == [
        "magnet:?xt=urn:btih:aaa",
        "magnet:?xt=urn:btih:bbb",
    ]

    assert rows[0]["size_text"] == "4.2GB"
    assert rows[0]["verified"] == 1

    # 不存在的番号 → 空列表，不抛异常
    assert db.magnets_for_title("NOPE-000") == []


# ------------------------------------------------- 4. deletable_titles 真算

def test_deletable_titles_sums_local_bytes(tmp_path, library_db):
    """造 2 个文件 + 1 条已验证磁力，断言总字节数是真算出来的。"""

    db = Database(library_db)

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", verified=1)

    db.add_file("ABP-171", os.path.join(tmp_path, "a.mp4"), size=1000)
    db.add_file("ABP-171", os.path.join(tmp_path, "b.mp4"), size=2500)

    titles = db.deletable_titles()

    assert len(titles) == 1

    entry = titles[0]

    assert entry["number"] == "ABP-171"
    assert entry["magnet_count"] == 1
    assert entry["file_count"] == 2
    assert entry["total_bytes"] == 3500
    assert sorted(f["filepath"] for f in entry["files"]) == sorted([
        os.path.join(tmp_path, "a.mp4"),
        os.path.join(tmp_path, "b.mp4"),
    ])


def test_deletable_titles_ignores_unverified_and_empty(tmp_path, library_db):
    """未验证磁力不算可删；没磁力的番号不出现；无本地文件的番号 total_bytes=0。"""

    db = Database(library_db)

    # verified=0 → 不算可删
    db.add_magnet("IPX-001", "magnet:?xt=urn:btih:unverified", verified=0)
    db.add_file("IPX-001", os.path.join(tmp_path, "c.mp4"), size=999)

    # 只有文件、没有磁力 → 不出现
    db.add_file("SSIS-002", os.path.join(tmp_path, "d.mp4"), size=500)

    # 有已验证磁力、但本地没有文件 → 出现，占用 0
    db.add_magnet("MIDE-003", "magnet:?xt=urn:btih:only-magnet", verified=1)

    titles = db.deletable_titles()

    assert [t["number"] for t in titles] == ["MIDE-003"]

    assert titles[0]["total_bytes"] == 0
    assert titles[0]["file_count"] == 0


def test_deletable_titles_multiple_files_multiple_magnets(tmp_path, library_db):
    """多条磁力不会把 media_files 的体积重复累加（JOIN 乘法回归保护）。"""

    db = Database(library_db)

    db.add_file("ABP-171", os.path.join(tmp_path, "a.mp4"), size=1000)

    for i in range(5):

        db.add_magnet("ABP-171", "magnet:?xt=urn:btih:%d" % i, verified=1)

    entry = db.deletable_titles()[0]

    assert entry["magnet_count"] == 5
    assert entry["file_count"] == 1
    assert entry["total_bytes"] == 1000


def test_deletion_gate_requires_verified_magnet(tmp_path, library_db):
    """
    删除门控策略矩阵（主人 2026-09-23 规矩 ②）。

    「多抓的可以通过后面抓取磁力的时候把不对的项目去除 —— 错的项目不含磁力，
      没有磁力的项目即使是真 AV 也不应该删除。」

    所以**唯一的可删条件**是：该番号挂着 verified=1 的磁力。
    下面逐条钉死，任何一条被放松都必须显式改这段测试。
    """

    db = Database(library_db)

    # ① 真 AV + 真文件，但一条磁力都没有 → 不可删（用户原话的直接落地）
    db.add_file("ABP-171", os.path.join(tmp_path, "real_av.mp4"), size=4096)

    # ② 假阳性番号（空格/下划线噪音抽出来的）+ 真文件 → 不可删
    db.add_file("SHEET-27", os.path.join(tmp_path, "noise.mp4"), size=2048)

    # ③ 有文件 + 只有 verified=0 的候选磁力 → 不可删
    db.add_file("IPX-999", os.path.join(tmp_path, "candidate.mp4"), size=1024)
    db.add_magnet("IPX-999", "magnet:?xt=urn:btih:candidate", verified=0)

    assert db.deletable_titles() == []

    # ④ 拿到 verified=1 之后，才第一次出现在可删列表里
    db.add_magnet("IPX-999", "magnet:?xt=urn:btih:verified", verified=1)

    titles = db.deletable_titles()

    assert [t["number"] for t in titles] == ["IPX-999"]
    assert titles[0]["total_bytes"] == 1024

    # ⑤ 真 AV 那条依然不在（它的文件还在本地，但没有磁力）
    assert "ABP-171" not in [t["number"] for t in titles]


def test_deletion_gate_does_not_delete_when_no_local_file(tmp_path, library_db):
    """
    有磁力但本地没有该番号的文件 → 出现在列表里但占用 0。

    调用方按 total_bytes / files 判空，就不会「删了个寂寞」，
    也不会因为列表非空就去删别的番号。
    """

    db = Database(library_db)

    db.add_magnet("MIDE-003", "magnet:?xt=urn:btih:only-magnet", verified=1)

    titles = db.deletable_titles()

    assert len(titles) == 1
    assert titles[0]["file_count"] == 0
    assert titles[0]["total_bytes"] == 0
    assert titles[0]["files"] == []


# --------------------------------------------- 5. 真实 verify_cache.json 导入

@pytest.mark.skipif(
    not os.path.exists(REAL_CACHE),
    reason="真实 verify_cache.json 不在本机（%s）" % REAL_CACHE,
)
def test_real_verify_cache_import_is_idempotent(tmp_path, library_db):
    importer = load_importer()

    library = REAL_LIBRARY_DB if os.path.exists(REAL_LIBRARY_DB) else None

    first = importer.run(library_db, REAL_CACHE, library)

    total_first = count(library_db, "magnets")

    assert total_first > 0

    assert first["stats"]["inserted"] == total_first

    # 二次运行：一行都不该新增
    second = importer.run(library_db, REAL_CACHE, library)

    total_second = count(library_db, "magnets")

    assert total_second == total_first

    assert second["stats"].get("inserted", 0) == 0
    assert second["stats"].get("duplicate", 0) == total_first

    # 无法解析的番号有明确计数，不允许静默丢弃
    assert first["stats"]["unresolved"] > 0
    assert sum(first["reasons"].values()) == first["stats"]["unresolved"]

    # 真实数字回显，便于门禁取证
    print(
        "\n[real] verify_cache=%s magnets=%d 二次运行=%d unresolved=%d reasons=%s total_bytes=%d"
        % (
            REAL_CACHE,
            total_first,
            total_second,
            first["stats"]["unresolved"],
            first["reasons"],
            sum(t["total_bytes"] for t in Database(library_db).deletable_titles()),
        )
    )


@pytest.mark.skipif(
    not os.path.exists(REAL_CACHE),
    reason="真实 verify_cache.json 不在本机（%s）" % REAL_CACHE,
)
def test_real_import_feeds_deletable_titles(tmp_path, library_db):
    """真实清单 + 真实本地文件清单 → deletable_titles() 能算出非零总占用。"""

    importer = load_importer()

    list_path = r"X:\corpus\final_list.tsv"

    if not os.path.exists(list_path):

        pytest.skip("final_list.tsv 不在本机")

    result = importer.run(
        library_db,
        REAL_CACHE,
        REAL_LIBRARY_DB if os.path.exists(REAL_LIBRARY_DB) else None,
        final_list=list_path,
    )

    titles = Database(library_db).deletable_titles()

    assert result["stats"]["list_registered"] == 312

    assert len(titles) > 0

    assert sum(t["total_bytes"] for t in titles) > 0

    # 312 条清单里，266 条所属番号在本批数据中能解析出真实磁力（其余 46 条的番号
    # 在 verify_cache/library.db 里都拿不到 hash，见导入报告的 unresolved 分类）。
    # deletable_titles() 只返回「有已验证磁力」的番号，所以这里必然是 266 而非 312。
    assert sum(t["file_count"] for t in titles) == 266

    assert all(t["magnet_count"] > 0 for t in titles)
