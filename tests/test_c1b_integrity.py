"""C1b 落库幂等补漏 —— 验收测试

覆盖四件事：
  1. 干跑语义（决策点 2 · 2A）：persist=False 不推进 file_index
  2. 规则版本（决策点 2 · 2B）：字典一变，同一 index.db 也能重扫
  3. 落库完整性（决策点 1 · 1A）：UPSERT 重链 title_id，不引入任何 DELETE
  4. 目录名误命中（追加项）：先 basename，再回退直接父目录

外加决策点 3 的「存量错值自愈」链路真跑：旧字典错值入库 → 换新字典 →
同一个 index.db 重扫 → title_id 被纠正。
"""

import json
import os
import sqlite3

import pytest

from core.database_v2 import Database
from core.file_index import FileIndex, rules_fingerprint
from core.quality import QualityChecker
from services.scan_service import ScanService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES_PATH = os.path.join(ROOT, "data", "dictionary.json")

# 现有字典里没有 ZZZZ，用来模拟「字典增强」
ENHANCED_EXTRA = {
    "code": "ZZZZ",
    "aliases": [],
    "pattern": "ZZZZ[-_ ]?\\d{3,6}",
    "priority": 100,
}

# 模拟「字典把规则写窄」的坏规则：ABP1234567.mp4 被截成 ABP123456
ABP_BROKEN = {
    "code": "ABP",
    "aliases": [],
    "pattern": "ABP\\d{3,6}",
    "priority": 100,
}

# 注：C3 的 canonical() 会给无分隔形态补分隔符（ABP123456 -> ABP-123456），
# 故断言用补分隔后的形态；规则 pattern 里分隔符仍是可选的。
# 修好后的规则：吃满 7 位
ABP_FIXED = {
    "code": "ABP",
    "aliases": [],
    "pattern": "ABP\\d{3,7}",
    "priority": 100,
}


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as fh:
        return json.load(fh)["rules"]


@pytest.fixture()
def library_db(tmp_path):
    """v2 主库路径（每用例一份，互不干扰）"""

    return str(tmp_path / "library_v2.db")


def rules_with(replacement):
    """现有规则里替换掉同 code 的那条，其余保持原样"""

    return [
        replacement if r["code"] == replacement["code"] else r
        for r in load_rules()
    ]


def make_media(tmp_path, names, folder="media"):
    """在 tmp_path/<folder> 下造假文件，支持 'a/b.mp4' 这种子目录写法"""

    root = tmp_path / folder

    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 32)

    return str(root)


def service_for(tmp_path, library_db, rules, index_name="index.db"):
    return ScanService(
        str(tmp_path / index_name),
        rules,
        db=Database(library_db)
    )


def query(path, sql, args=()):
    conn = sqlite3.connect(path)

    try:
        return conn.execute(sql, args).fetchall()

    finally:
        conn.close()


def counts(path):
    return (
        query(path, "SELECT COUNT(*) FROM titles")[0][0],
        query(path, "SELECT COUNT(*) FROM media_files")[0][0],
    )


def media_row(path):
    return query(
        path,
        """
        SELECT media_files.id,
               media_files.title_id,
               media_files.filename,
               media_files.size,
               titles.number
        FROM media_files
        JOIN titles ON titles.id = media_files.title_id
        """
    )[0]


# =============================================================== 1. 干跑语义


def test_dry_run_does_not_advance_index(tmp_path, library_db):
    """persist=False 不写库，也**不推进 file_index**（真·干跑）"""

    folder = make_media(tmp_path, ["ABP-123.mp4", "IPX-456.mp4"])
    index_db = str(tmp_path / "index.db")

    service_for(tmp_path, library_db, load_rules()).scan(folder, persist=False)

    assert query(index_db, "SELECT COUNT(*) FROM file_index")[0][0] == 0
    assert counts(library_db) == (0, 0)


def test_dry_run_then_real_run_still_persists(tmp_path, library_db):
    """同一 index.db：先干跑再真跑，必须照常落库（原为静默空转）"""

    folder = make_media(tmp_path, ["ABP-123.mp4", "IPX-456.mp4"])

    dry = service_for(tmp_path, library_db, load_rules()).scan(
        folder,
        persist=False
    )

    assert len(dry["data"]) == 2
    assert [row["persisted"] for row in dry["data"]] == [False, False]

    wet = service_for(tmp_path, library_db, load_rules()).scan(
        folder,
        persist=True
    )

    assert len(wet["data"]) == 2
    assert all(row["persisted"] for row in wet["data"])

    assert counts(library_db) == (2, 2)


# ============================================================ 2. 规则版本指纹


def test_rules_fingerprint_tracks_content():
    rules = load_rules()

    assert rules_fingerprint(rules) == rules_fingerprint(load_rules())
    assert rules_fingerprint(rules) != rules_fingerprint(rules + [ENHANCED_EXTRA])


def test_enhanced_dictionary_rescans_same_index(tmp_path, library_db):
    """字典增强后，同一 index.db 重扫即可补录（C3 场景）"""

    folder = make_media(tmp_path, ["ZZZZ123456.mp4"])
    index_db = str(tmp_path / "index.db")

    first = ScanService(
        index_db, load_rules(), db=Database(library_db)
    ).scan(folder)

    assert first["data"][0]["numbers"] == []
    assert counts(library_db) == (0, 0)

    # 同一个 index.db，只换规则集
    second = ScanService(
        index_db, load_rules() + [ENHANCED_EXTRA], db=Database(library_db)
    ).scan(folder)

    assert len(second["data"]) == 1
    assert [n["number"] for n in second["data"][0]["numbers"]] == ["ZZZZ-123456"]
    assert second["data"][0]["persisted"] is True

    assert counts(library_db) == (1, 1)


def test_legacy_index_db_is_migrated(tmp_path):
    """老库（file_index 无 rules_version 列）打开不炸，自动补列"""

    index_db = str(tmp_path / "legacy_index.db")

    conn = sqlite3.connect(index_db)
    conn.execute(
        """
        CREATE TABLE file_index
        (
            path TEXT PRIMARY KEY,
            size INTEGER,
            mtime REAL,
            last_scan REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO file_index VALUES('X:/a.mp4', 32, 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    index = FileIndex(index_db, rules_version="fp-new")

    columns = [
        row[1] for row in index.conn.execute("PRAGMA table_info(file_index)")
    ]

    assert "rules_version" in columns

    # 老行 rules_version 为 NULL != 当前指纹 → 需要重扫
    assert index.changed("X:/a.mp4", 32, 1.0) is True

    # 重扫后写回指纹，不再反复重扫
    index.update("X:/a.mp4", 32, 1.0)
    assert index.changed("X:/a.mp4", 32, 1.0) is False


def test_shared_library_db_as_index_is_migrated(tmp_path, library_db):
    """index_db 直接指向 v2 主库（file_index 由 Database.create 建，4 列旧结构）

    生产上 ScanService 的 index_db 参数是任意路径；撞上主库时，
    建表先跑没有坏处，但要确认补列路径同样生效。
    """

    Database(library_db)

    columns = {
        row[1]
        for row in query(library_db, "PRAGMA table_info(file_index)")
    }

    assert "rules_version" not in columns          # 建表时确实没有

    folder = make_media(tmp_path, ["ABP-123.mp4"])

    # index_db 就指向主库本身
    service = ScanService(
        library_db,
        load_rules(),
        db=Database(library_db)
    )

    result = service.scan(folder)

    assert result["data"][0]["persisted"] is True
    assert counts(library_db) == (1, 1)

    stored = query(
        library_db, "SELECT rules_version FROM file_index"
    )[0][0]

    assert stored == rules_fingerprint(load_rules())


def test_rules_version_none_keeps_old_behaviour(tmp_path):
    """不传指纹时行为与旧版一致（只看 size/mtime）"""

    index = FileIndex(str(tmp_path / "plain_index.db"))

    assert index.changed("X:/a.mp4", 32, 1.0) is True

    index.update("X:/a.mp4", 32, 1.0)

    assert index.changed("X:/a.mp4", 32, 1.0) is False
    assert index.changed("X:/a.mp4", 64, 1.0) is True


# ==================================================== 3. 落库完整性（1A/UPSERT）


def test_rescan_same_number_creates_no_new_rows(tmp_path, library_db):
    """同一 filepath 番号未变 → 复用 title，不新增任何行"""

    folder = make_media(tmp_path, ["ABP-123.mp4"])
    index_db = str(tmp_path / "index.db")

    service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert counts(library_db) == (1, 1)

    # 换 index 强制重扫（文件未变）
    again = service_for(
        tmp_path, library_db, load_rules(), index_name="index2.db"
    ).scan(folder)

    assert again["data"][0]["persisted"] is True
    assert counts(library_db) == (1, 1)


def test_stale_title_id_is_relinked(tmp_path, library_db):
    """同一 filepath 解析出新番号 → title_id 重链，旧值不再挂死"""

    folder = make_media(tmp_path, ["ABP1234567.mp4"])
    index_db = str(tmp_path / "index.db")

    service_for(tmp_path, library_db, load_rules()).scan(folder)

    first_id = media_row(library_db)[1]
    assert media_row(library_db)[4] == "ABP-123456"

    # 规则集 B：同一路径命中不同番号文本（模拟 C3 修好解析后的重扫）
    # 无分隔符形态让内置 std 规则不参与，两套规则各自给出唯一番号
    rules_b = [
        {
            "code": "ABP",
            "aliases": [],
            "pattern": "ABP\\d{3,7}",
            "priority": 100,
        }
    ]

    result = service_for(
        tmp_path, library_db, rules_b
    ).scan(folder)

    assert [n["number"] for n in result["data"][0]["numbers"]] == [
        "ABP-1234567"
    ]
    assert result["data"][0]["persisted"] is True

    row = media_row(library_db)

    assert row[4] == "ABP-1234567"        # 关联已指向新番号
    assert row[1] != first_id            # 确实换了 title
    assert counts(library_db) == (2, 1)  # 孤儿旧 title 保留（1A：不删）


def test_size_and_filename_are_refreshed(tmp_path, library_db):
    """size 变化重扫 → 库里的 size/filename 跟着更新（原为整行跳过）"""

    folder = make_media(tmp_path, ["ABP-123.mp4"])
    path = os.path.join(folder, "ABP-123.mp4")

    service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert media_row(library_db)[3] == 32

    with open(path, "wb") as fh:
        fh.write(b"\x00" * 4096)

    # 同一 index.db：size 变了必然重扫
    service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert media_row(library_db)[3] == 4096
    assert counts(library_db) == (1, 1)


def test_orphan_title_is_not_reported_as_missing_metadata(tmp_path, library_db):
    """孤儿 title 不再污染质检报告（只报真有文件的番号）"""

    folder = make_media(tmp_path, ["ABP-123CD.mp4"])

    service_for(tmp_path, library_db, load_rules()).scan(folder)

    rules_b = [
        {
            "code": "ABP",
            "aliases": [],
            "pattern": "ABP[-_ ]?\\d{3,6}[A-Z]{2}",
            "priority": 100,
        }
    ]

    service_for(tmp_path, library_db, rules_b).scan(folder)

    checker = QualityChecker(library_db)

    assert counts(library_db) == (2, 1)             # 库内确有孤儿行
    assert checker.missing_metadata() == ["ABP-123CD"]


def test_add_file_upsert_keeps_old_values_on_none(tmp_path, library_db):
    """新值为 NULL 时保留旧值，且 created_time 不因重扫刷新"""

    db = Database(library_db)
    path = str(tmp_path / "ABP-123.mp4")

    db.add_file("ABP-123", path, file_hash="hash-a", size=32)
    before = query(library_db, "SELECT created_time FROM media_files")[0][0]

    db.add_file("ABP-123", path)

    row = query(
        library_db,
        "SELECT title_id, file_hash, size, created_time FROM media_files"
    )[0]

    assert row[1] == "hash-a"
    assert row[2] == 32
    assert row[3] == before
    assert counts(library_db) == (1, 1)


def test_add_file_reports_relink(tmp_path, library_db):
    """add_file 返回值带 previous_title_id，便于观测重链"""

    db = Database(library_db)
    path = str(tmp_path / "ABP-123.mp4")

    first = db.add_file("ABP-123", path)

    assert first["previous_title_id"] is None

    second = db.add_file("ABP-124", path)

    assert second["previous_title_id"] == first["title_id"]
    assert second["title_id"] != first["title_id"]


# =============================================== 4. 目录名误命中（basename 优先）


def test_basename_number_is_used(tmp_path, library_db):
    """番号在文件名里 → 用文件名解析"""

    folder = make_media(tmp_path, ["ABP-123.mp4"])

    result = service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert [n["number"] for n in result["data"][0]["numbers"]] == ["ABP-123"]
    assert media_row(library_db)[4] == "ABP-123"


def test_parent_folder_number_is_used_as_fallback(tmp_path, library_db):
    """文件名无番号、直接父目录是番号 → 回退父目录名（番号目录/视频 结构）"""

    folder = make_media(tmp_path, ["ABP-999/random_clip.mp4"])

    result = service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert [n["number"] for n in result["data"][0]["numbers"]] == ["ABP-999"]
    assert result["data"][0]["persisted"] is True
    assert counts(library_db) == (1, 1)


def test_ancestor_folder_number_does_not_hijack(tmp_path, library_db):
    """番号在更上层目录（祖父级）→ 不得命中（原为整条路径参与匹配）"""

    folder = make_media(tmp_path, ["ABP-777/season1/clip.mp4"])

    result = service_for(tmp_path, library_db, load_rules()).scan(folder)

    assert result["data"][0]["numbers"] == []
    assert result["data"][0]["persisted"] is False
    assert counts(library_db) == (0, 0)


# ============================== 决策点 3：存量错值「免费自愈」链路（真跑）


def test_stored_wrong_number_self_heals_after_dictionary_fix(
    tmp_path, library_db
):
    """坏字典按错值入库 → 换修好的字典 → 同一 index.db 重扫 → 自动纠错

    C3 修好 FC2 之后，活字典已解不出错值，故用「把规则写窄」的
    ABP_BROKEN 显式复现存量错值：ABP1234567.mp4 被截成 ABP123456。
    断言两件事：错值行被重链到正确番号；不需要任何专门的修复入口。
    """

    folder = make_media(tmp_path, ["ABP1234567.mp4"])
    index_db = str(tmp_path / "index.db")

    # 第 1 轮：坏字典（错值 ABP123456）
    before = ScanService(
        index_db, rules_with(ABP_BROKEN), db=Database(library_db)
    ).scan(folder)

    assert [n["number"] for n in before["data"][0]["numbers"]] == [
        "ABP-123456"
    ]
    assert media_row(library_db)[4] == "ABP-123456"

    # 第 2 轮：修好的字典，**同一个 index.db**，文件本身没动
    after = ScanService(
        index_db, rules_with(ABP_FIXED), db=Database(library_db)
    ).scan(folder)

    assert len(after["data"]) == 1
    assert [n["number"] for n in after["data"][0]["numbers"]] == [
        "ABP-1234567"
    ]

    row = media_row(library_db)

    assert row[4] == "ABP-1234567"
    assert counts(library_db) == (2, 1)

    # 孤儿旧行仍在，但不再被当成「缺元数据」报出来
    assert QualityChecker(library_db).missing_metadata() == ["ABP-1234567"]
