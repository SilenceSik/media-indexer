# -*- coding: utf-8 -*-
"""卷安全检查测试（`core/trash_guard.py`）。

守的是一条对外承诺：**删除只走回收站、随时可恢复**。
所以必须证明两件事：

1. 只有「内置固定盘 + NTFS/ReFS + 非外接总线」才放行；
2. 拿不准的时候**拒绝**，而不是默认对方是普通内置盘
   （主人 2026-09-25：「不应该用我们的白名单默认别人是普通磁盘」）。

第 3 层（总线）是关键：U 盘 / 移动硬盘常被 Windows 报成 `DRIVE_FIXED`、
文件系统也常是 NTFS —— 只查前两层会漏。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import trash_guard                                     # noqa: E402
from core.trash_guard import recyclable_reason                   # noqa: E402
from core.database_v2 import Database                            # noqa: E402
from services.file_service import FileService                    # noqa: E402


@pytest.fixture
def on_windows(monkeypatch):
    """把守卫钉在 Windows 分支上（测试要能跑在任何平台）。"""

    monkeypatch.setattr(trash_guard, "IS_WINDOWS", True)

    return monkeypatch


def _pin(monkeypatch, drive_type, fs, bus):
    """钉住两层底层探测，按需返回指定值。"""

    monkeypatch.setattr(
        trash_guard, "_volume_info", lambda root: (drive_type, fs)
    )

    monkeypatch.setattr(trash_guard, "_bus_type", lambda letter: bus)


# ─────────────────────────── 放行：内置固定盘

def test_internal_ntfs_fixed_allowed(on_windows):
    """SATA 上的 NTFS 固定盘 -> 放行（典型内置盘）。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, "NTFS", "SATA")

    ok, why = recyclable_reason("C:" + os.sep + "movies" + os.sep + "a.mp4")

    assert ok is True
    assert why == ""


def test_nvme_refs_allowed(on_windows):
    """NVMe 上的 ReFS -> 放行（ReFS 也有回收站）。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, "REFS", "NVMe")

    ok, _ = recyclable_reason("M:" + os.sep + "backup" + os.sep + "a.mp4")

    assert ok is True


# ─────────────────────────── 第 1 层：盘类型

def test_network_drive_refused(on_windows):
    """网络盘没有回收站 -> 拒绝，且理由要点明「找不回来」。"""

    _pin(on_windows, trash_guard.DRIVE_REMOTE, "NTFS", "iSCSI")

    ok, why = recyclable_reason("Z:" + os.sep + "share" + os.sep + "a.mp4")

    assert ok is False
    assert "网络盘" in why


def test_removable_drive_refused(on_windows):
    """可移动盘（U 盘）-> 拒绝。"""

    _pin(on_windows, trash_guard.DRIVE_REMOVABLE, "NTFS", "USB")

    ok, why = recyclable_reason("E:" + os.sep + "a.mp4")

    assert ok is False
    assert "可移动盘" in why


# ─────────────────────────── 第 2 层：文件系统

def test_exfat_refused(on_windows):
    """exFAT 卷没有 $Recycle.Bin -> 拒绝。

    这是最隐蔽的一种：盘类型可能报固定盘、用户也以为删得进回收站。
    """

    _pin(on_windows, trash_guard.DRIVE_FIXED, "EXFAT", "SATA")

    ok, why = recyclable_reason("D:" + os.sep + "a.mp4")

    assert ok is False
    assert "EXFAT" in why


def test_fat32_refused(on_windows):
    """FAT32 同理。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, "FAT32", "SATA")

    ok, why = recyclable_reason("D:" + os.sep + "a.mp4")

    assert ok is False
    assert "FAT32" in why


# ─────────────────────────── 第 3 层：总线（本次纠正的重点）

def test_usb_hdd_refused_even_if_fixed_ntfs(on_windows):
    """USB 移动硬盘：盘类型报固定盘、文件系统是 NTFS —— 仍必须拒绝。

    只查盘类型会漏掉这一整类，而它恰是「备份盘」最常见的形态。
    """

    _pin(on_windows, trash_guard.DRIVE_FIXED, "NTFS", "USB")

    ok, why = recyclable_reason("M:" + os.sep + "backup" + os.sep + "a.mp4")

    assert ok is False
    assert "USB" in why


def test_sd_card_refused(on_windows):
    """读卡器里的 SD 卡 -> 拒绝。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, "NTFS", "SD")

    ok, why = recyclable_reason("F:" + os.sep + "a.mp4")

    assert ok is False
    assert "SD" in why


# ─────────────────────────── 失败方向：拿不准就拒绝

def test_unreadable_filesystem_refused(on_windows):
    """读不到文件系统 -> 拒绝（不默认对方是普通盘）。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, None, "SATA")

    ok, why = recyclable_reason("D:" + os.sep + "a.mp4")

    assert ok is False
    assert "拿不准" in why


def test_unknown_bus_refused(on_windows):
    """查不到总线 -> 拒绝。"""

    _pin(on_windows, trash_guard.DRIVE_FIXED, "NTFS", None)

    ok, why = recyclable_reason("D:" + os.sep + "a.mp4")

    assert ok is False
    assert "拿不准" in why


def test_empty_path_refused(on_windows):
    """空路径 -> 拒绝。"""

    ok, why = recyclable_reason("")

    assert ok is False
    assert "空" in why


def test_volume_info_exception_refused(on_windows, monkeypatch):
    """底层探测抛异常 -> 拒绝（不能把异常当放行）。"""

    def boom(root):
        raise OSError("模拟读卷失败")

    monkeypatch.setattr(trash_guard, "_volume_info", boom)

    ok, why = recyclable_reason("D:" + os.sep + "a.mp4")

    assert ok is False
    assert "拒绝删除" in why


# ─────────────────────────── 非 Windows：交给 send2trash 自己

def test_non_windows_passthrough(monkeypatch):
    """非 Windows 不做判断（没有盘符概念）。"""

    monkeypatch.setattr(trash_guard, "IS_WINDOWS", False)

    ok, why = recyclable_reason("/home/user/video.mp4")

    assert ok is True
    assert why == ""


# ─────────────────────────── 接线：拒绝时不得调用 send2trash

def test_trash_and_mark_blocks_without_calling_send2trash(tmp_path, monkeypatch):
    """守卫拒绝时，文件必须原封不动，且不落到 failed（是 blocked）。"""

    import services.file_service as fs

    db = Database(str(tmp_path / "t.db"))

    video = tmp_path / "ABP-171.mp4"

    video.write_bytes(b"x" * 64)

    svc = FileService(db, str(tmp_path / "torrents"))

    monkeypatch.setattr(
        fs, "recyclable_reason", lambda p: (False, "这是移动盘 / 外接盘（USB）")
    )

    detail = svc._trash_and_mark([str(video)])

    assert detail["deleted"] == [], "被拦下的文件不得计为已删"
    assert detail["blocked"], "必须记进 blocked 并带原因"
    assert video.exists(), "★ 被拦下的文件必须还在磁盘上"

    # 库里也不能被标成已删 —— 否则卡片会显示「本地已删」而文件还在
    row = db.conn.execute(
        "SELECT COALESCE(local_deleted, 0) AS d FROM media_files WHERE filepath = ?",
        (str(video),),
    ).fetchone()

    assert row is None or row["d"] == 0


def test_delete_empty_dirs_blocks_outside_fixed_disk(tmp_path, monkeypatch):
    """空目录清理走同一条卷安全检查。"""

    import services.file_service as fs

    db = Database(str(tmp_path / "t.db"))

    svc = FileService(db, str(tmp_path / "torrents"))

    empty = tmp_path / "empty_dir"

    empty.mkdir()

    monkeypatch.setattr(
        fs, "recyclable_reason", lambda p: (False, "网络盘没有回收站")
    )

    ok, failed = svc.delete_empty_dirs([str(empty)])

    assert ok == 0
    assert failed and "未删除" in failed[0]
    assert empty.is_dir(), "被拦下的目录必须还在"
