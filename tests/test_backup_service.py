# -*- coding: utf-8 -*-
"""导出 / 导入压缩包 测试。

重点：
  * 导出必须用 SQLite backup API 落一致快照（WAL 下直接拷文件会漏页）
  * 导入是**合并**语义，不是覆盖
  * 导入前必须备份现有库
  * 目录穿越必须挡掉
"""

import json
import os
import sqlite3
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from services.backup_service import BackupService              # noqa: E402


@pytest.fixture
def env(tmp_path):
    """一个带数据 + 素材的环境。"""

    db_path = str(tmp_path / "library_v2.db")

    db = Database(db_path)

    db.add_file("ABP-171", str(tmp_path / "ABP-171.mp4"))

    db.save_metadata({
        "number": "ABP-171",
        "title": "标题",
        "cover_local": "ABP-171/image-001.jpg",
        "screenshots": ["ABP-171/image-001.jpg"],
    })

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", verified=1)

    covers = tmp_path / "covers" / "ABP-171"

    covers.mkdir(parents=True)

    (covers / "image-001.jpg").write_bytes(b"cover-bytes")

    shots = tmp_path / "screenshots" / "ABP-171"

    shots.mkdir(parents=True)

    (shots / "image-001.jpg").write_bytes(b"shot-bytes")

    torrents = tmp_path / "torrents"

    torrents.mkdir()

    (torrents / "ABP-171.txt").write_text("magnet:?xt=urn:btih:aaa", encoding="utf-8")

    exports = tmp_path / "exports"

    svc = BackupService(
        db_path, str(tmp_path / "covers"), str(tmp_path / "screenshots"),
        str(torrents), str(exports),
    )

    return svc, db_path, tmp_path, db


# ─────────────────────── 导出

def test_export_creates_zip_with_manifest(env):
    svc, db_path, tmp, db = env

    path, stats = svc.export(include_images=True)

    assert os.path.exists(path)

    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        assert "library.db" in names
        assert "manifest.json" in names

        manifest = json.loads(z.read("manifest.json"))

        assert manifest["version"] >= 1
        assert manifest["include_images"] is True
        assert manifest["counts"]["titles"] == 1


def test_export_includes_assets(env):
    svc, db_path, tmp, db = env

    path, stats = svc.export(include_images=True)

    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        assert any(n.startswith("covers/") for n in names)
        assert any(n.startswith("screenshots/") for n in names)
        assert any(n.startswith("torrents/") for n in names)

    assert stats["covers"] == 1
    assert stats["screenshots"] == 1


def test_export_without_images_skips_assets(env):
    """不含素材时不该打包封面/截图，但磁力文本档仍要带。"""

    svc, db_path, tmp, db = env

    path, stats = svc.export(include_images=False)

    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        assert not any(n.startswith("covers/") for n in names)
        assert not any(n.startswith("screenshots/") for n in names)
        assert any(n.startswith("torrents/") for n in names)

    assert stats["images"] is False


def test_export_leaves_no_snapshot_behind(env):
    """一致性快照是临时文件，导出后必须清掉。"""

    svc, db_path, tmp, db = env

    svc.export(include_images=False)

    leftovers = [
        n for n in os.listdir(svc.export_dir)
        if n.startswith(".")
    ]

    assert leftovers == [], f"残留临时文件：{leftovers}"


def test_export_snapshot_is_readable_db(env):
    """导出包里的库必须是可用的 SQLite（不是半写状态）。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=False)

    extract = tmp / "check"

    extract.mkdir()

    with zipfile.ZipFile(path) as z:
        z.extract("library.db", str(extract))

    c = sqlite3.connect(str(extract / "library.db"))

    try:
        assert c.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM magnets").fetchone()[0] == 1
    finally:
        c.close()


# ─────────────────────── 导入

def test_import_merges_into_existing_db(env, tmp_path):
    """导入是合并：新番号加进来，已有的不重复。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=True)

    # 在现有库里再加一个番号（导入后应变成 2 个）
    db.add_file("SSIS-001", str(tmp / "SSIS-001.mp4"))

    stats, backup = svc.import_zip(path, include_images=True)

    assert stats["titles_added"] == 0, "原番号不该重复建"
    assert stats["files_added"] == 0, "原文件不该重复加"

    total = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM titles"
    ).fetchone()[0]

    assert total == 2


def test_import_adds_new_titles(env, tmp_path):
    """导入一个含新番号的包，应真的加进来。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=False)

    # 清空当前库（模拟另一台机器）
    c = sqlite3.connect(db_path)

    for t in ("magnets", "metadata", "media_files", "titles"):
        c.execute(f"DELETE FROM {t}")

    c.commit()
    c.close()

    stats, _ = svc.import_zip(path, include_images=False)

    assert stats["titles_added"] == 1
    assert stats["files_added"] == 1
    assert stats["magnets_added"] == 1


def test_import_backs_up_current_db(env):
    """导入是覆盖性操作，必须先备份现有库。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=False)

    stats, backup = svc.import_zip(path, include_images=False)

    assert backup, "必须返回备份路径"
    assert os.path.exists(backup), "备份文件要真的在"
    assert "before-import" in backup


def test_import_rejects_non_library_zip(env, tmp_path):
    """不是本工具的导出包要明确拒绝。"""

    svc, db_path, tmp, db = env

    bad = tmp_path / "bad.zip"

    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("random.txt", "hello")

    with pytest.raises(ValueError) as e:
        svc.import_zip(str(bad))

    assert "library.db" in str(e.value)


def test_import_missing_file(env):
    svc, db_path, tmp, db = env

    with pytest.raises(FileNotFoundError):
        svc.import_zip(str(tmp / "nope.zip"))


def test_import_does_not_overwrite_existing_assets(env, tmp_path):
    """素材同名不覆盖 —— 已有的优先。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=True)

    cover = tmp_path / "covers" / "ABP-171" / "image-001.jpg"

    cover.write_bytes(b"LOCAL-EDITED")

    svc.import_zip(path, include_images=True)

    assert cover.read_bytes() == b"LOCAL-EDITED", "本地已有的不该被覆盖"


def test_import_blocks_path_traversal(env, tmp_path):
    """zip 里的 ../ 必须挡掉，不能写到目标目录之外。"""

    svc, db_path, tmp, db = env

    evil = tmp_path / "evil.zip"

    good = svc.export(include_images=False)[0]

    with zipfile.ZipFile(good) as src, zipfile.ZipFile(evil, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n))
        dst.writestr("covers/../../pwned.txt", "owned")

    svc.import_zip(str(evil), include_images=True)

    assert not (tmp_path / "pwned.txt").exists()
    assert not (tmp_path.parent / "pwned.txt").exists()


# ─────────────────────── 往返一致性

def test_roundtrip_preserves_records(env, tmp_path):
    """导出再导入（到空库）后，记录数一致。"""

    svc, db_path, tmp, db = env

    path, _ = svc.export(include_images=True)

    before = {}

    c = sqlite3.connect(db_path)

    for t in ("titles", "media_files", "metadata", "magnets"):
        before[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    c.close()

    svc.import_zip(path, include_images=True)

    after = {}

    c = sqlite3.connect(db_path)

    for t in ("titles", "media_files", "metadata", "magnets"):
        after[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    c.close()

    assert before == after
