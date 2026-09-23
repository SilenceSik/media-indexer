# -*- coding: utf-8 -*-
"""文件操作服务测试。

重点在**删除门控**：没磁力的番号必须被拒绝 —— 这是 README 写死的契约。
所有删除走回收站，绝不永久删除。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from services.file_service import FileService                  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """一个带 1 个番号 + 1 个真实文件的环境。"""

    db = Database(str(tmp_path / "t.db"))

    video = tmp_path / "ABP-171.mp4"

    video.write_bytes(b"x" * 64)

    svc = FileService(db, str(tmp_path / "torrents"))

    return db, svc, video


# ─────────────────────────── 删除门控（核心契约）

def test_delete_refused_without_magnet(env):
    """没有磁力 -> 拒绝删除，文件原封不动。"""

    db, svc, video = env

    db.add_file("ABP-171", str(video))

    ok, msg, detail = svc.delete_title("ABP-171")

    assert ok is False
    assert "磁力" in msg
    assert detail["deleted"] == []
    assert video.exists(), "门控拒绝时不得动文件"


def test_delete_refused_with_unverified_magnet_only(env):
    """只有 verified=0 的候选磁力 -> 仍拒绝（候选不足以判定可删）。"""

    db, svc, video = env

    db.add_file("ABP-171", str(video))

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:aaa", verified=0)

    ok, msg, _ = svc.delete_title("ABP-171")

    assert ok is False
    assert video.exists()


def test_delete_allowed_with_verified_magnet(env):
    """有 verified=1 磁力 -> 放行，文件进回收站（不再存在于原位置）。"""

    db, svc, video = env

    db.add_file("ABP-171", str(video))

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:bbb", verified=1)

    ok, msg, detail = svc.delete_title("ABP-171")

    assert ok is True, msg
    assert len(detail["deleted"]) == 1
    assert not video.exists(), "文件应已离开原位置（进回收站）"


def test_delete_removes_media_file_row(env):
    """删除后要摘掉 media_files 关联，否则库里一直挂着不存在的路径。"""

    db, svc, video = env

    db.add_file("ABP-171", str(video))

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:ccc", verified=1)

    svc.delete_title("ABP-171")

    left = db.conn.execute(
        "SELECT COUNT(*) FROM media_files WHERE filepath=?", (str(video),)
    ).fetchone()[0]

    assert left == 0


def test_delete_missing_file_is_tolerated(env):
    """文件已不在磁盘 -> 记进 missing，不抛异常。"""

    db, svc, video = env

    db.add_file("ABP-171", str(video))

    db.add_magnet("ABP-171", "magnet:?xt=urn:btih:ddd", verified=1)

    video.unlink()

    ok, msg, detail = svc.delete_title("ABP-171")

    assert ok is True
    assert detail["missing"]


def test_deletable_index_only_verified(env):
    db, svc, video = env

    db.add_file("A-1", str(video))
    db.add_magnet("A-1", "magnet:?xt=urn:btih:e1", verified=1)

    db.add_file("B-2", str(video))
    db.add_magnet("B-2", "magnet:?xt=urn:btih:e2", verified=0)

    index = svc.deletable_index()

    assert "A-1" in index
    assert "B-2" not in index


# ─────────────────────────── 保存磁力

def test_save_torrent_writes_file(env, tmp_path):
    db, svc, video = env

    db.add_file("ABP-171", str(video))

    db.add_magnet(
        "ABP-171", "magnet:?xt=urn:btih:fff",
        source="test", size_text="5.74 GB", verified=1,
    )

    ok, msg, path = svc.save_torrent("ABP-171")

    assert ok is True
    assert os.path.exists(path)

    text = open(path, encoding="utf-8").read()

    assert "ABP-171" in text
    assert "magnet:?xt=urn:btih:fff" in text
    assert "5.74 GB" in text


def test_save_torrent_without_magnet(env):
    db, svc, video = env

    db.add_file("ABP-171", str(video))

    ok, msg, path = svc.save_torrent("ABP-171")

    assert ok is False
    assert path is None


# ─────────────────────────── 空目录

def test_scan_empty_dirs_finds_nested_empties(tmp_path):
    """整棵子树没文件的目录都要找出来（含嵌套）。"""

    root = tmp_path / "lib"

    (root / "a" / "b" / "c").mkdir(parents=True)

    (root / "full").mkdir(parents=True)

    (root / "full" / "x.mp4").write_bytes(b"x")

    svc = FileService(None, str(tmp_path / "t"))

    found = svc.scan_empty_dirs([str(root)])

    names = {os.path.basename(p) for p in found}

    assert names == {"c", "b", "a"}
    assert "full" not in names, "有文件的目录不算空"


def test_scan_empty_dirs_does_not_swallow_dir_with_nested_file(tmp_path):
    """深层有文件的子树，中间目录不得被当空目录。

    实测踩过：`ga/game/images/KISS-01.webm` 里 `ga/game` 被误判为空 ——
    因为判定用的是「子目录访问过」而不是「子目录判定为空」。
    真删了就把整棵有文件的树送进回收站。
    """

    root = tmp_path / "lib"

    deep = root / "ga" / "game" / "images"

    deep.mkdir(parents=True)

    (deep / "KISS-01.webm").write_bytes(b"x")

    svc = FileService(None, str(tmp_path / "t"))

    found = svc.scan_empty_dirs([str(root)])

    assert found == [], f"不该有任何空目录，实际 {found}"


def test_scan_empty_dirs_mixed(tmp_path):
    """同一棵树里既有无文件的深层目录，也有带文件的目录。"""

    root = tmp_path / "lib"

    (root / "has" / "file").mkdir(parents=True)

    (root / "has" / "file" / "x.mp4").write_bytes(b"x")

    (root / "void" / "deeper").mkdir(parents=True)

    svc = FileService(None, str(tmp_path / "t"))

    found = svc.scan_empty_dirs([str(root)])

    names = {os.path.basename(p) for p in found}

    assert names == {"void", "deeper"}
    assert "has" not in names


def test_scan_empty_dirs_never_returns_scan_root(tmp_path):
    """扫描根自身永不入选（删扫描根是灾难性的）。"""

    root = tmp_path / "lib"

    (root / "empty").mkdir(parents=True)

    svc = FileService(None, str(tmp_path / "t"))

    found = svc.scan_empty_dirs([str(root)])

    assert str(root) not in found
    assert os.path.basename(found[0]) == "empty"


def test_scan_empty_dirs_ignores_nonexistent_root(tmp_path):
    svc = FileService(None, str(tmp_path / "t"))

    assert svc.scan_empty_dirs([str(tmp_path / "nope")]) == []


def test_delete_empty_dirs_only_removes_empty(tmp_path):
    """删除前复查：扫描后被塞进文件的目录不得被删。"""

    root = tmp_path / "lib"

    empty = root / "empty"
    empty.mkdir(parents=True)

    became_used = root / "used"
    became_used.mkdir(parents=True)

    svc = FileService(None, str(tmp_path / "t"))

    # 模拟「扫描后目录被占用」
    (became_used / "new.mp4").write_bytes(b"x")

    ok, failed = svc.delete_empty_dirs([str(empty), str(became_used)])

    assert ok == 1
    assert not empty.exists()
    assert became_used.exists(), "非空目录不得删除"


# ─────────────────────────── 打开位置

def test_open_in_explorer_rejects_empty():
    ok, msg = FileService.open_in_explorer("")

    assert ok is False


def test_open_in_explorer_missing_path(tmp_path):
    ok, msg = FileService.open_in_explorer(str(tmp_path / "nope" / "x.mp4"))

    assert ok is False
