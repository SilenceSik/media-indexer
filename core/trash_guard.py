# -*- coding: utf-8 -*-
"""删除前的卷安全检查：这个位置删掉，东西能不能真的进回收站。

## 为什么需要它

`README` 和界面都承诺「走回收站，随时可恢复」。但 `send2trash` 在
Windows 上用的旗标是 **「如果可能就送回收站」** —— 目标卷没有回收站时
（网络盘、exFAT/FAT32 卷、部分移动盘），它做不到「回收」这件事。
承诺失真比缺个免责声明危险得多，所以在动手之前先确认一次。

## 三层判据（任一不过就拒绝删除）

| 层 | 查什么 | 拒绝条件 |
|---|---|---|
| 1 | 盘类型 `GetDriveType` | 不是本地固定盘（网络盘 / 可移动盘 / 光驱…） |
| 2 | 文件系统 | 不是 NTFS / ReFS（exFAT、FAT32 **没有回收站**） |
| 3 | 总线类型 `BusType` | USB / 1394 / SD / MMC / iSCSI / 虚拟盘 |

第 3 层是**产品判断**，不只是技术判断：移动盘通常是用户的**备份盘**，
在上面「省空间」本身就没意义（主人 2026-09-25：
「别人放在移动盘本身为了备份，没必要动移动盘省空间」）。
注意 U 盘/移动硬盘常被 Windows 报成 `DRIVE_FIXED`、文件系统也常是
NTFS —— 只查前两层会漏，必须查总线。

## 失败方向

**拿不准就拒绝。** 与本项目「宁可漏删，不可错删」一致：检测不到卷信息时
不放行，而不是默认对方是普通内置盘。

纯标准库（`ctypes`），不引入任何新依赖。
"""

import ctypes
import os
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# ── 盘类型 ──────────────────────────────────────────────────────────

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4

_DRIVE_NAMES = {
    0: "未知类型",
    1: "不是有效盘符",
    2: "可移动盘",
    3: "本地固定盘",
    4: "网络盘",
    5: "光驱",
    6: "内存盘",
}

# 这些文件系统**没有回收站**（回收站依赖 NTFS/ReFS 的 $Recycle.Bin）
_RECYCLABLE_FS = {"NTFS", "REFS"}

# 这些总线一律视为「不是内置盘」：USB 硬盘、移动盘、读卡器、网络映射
_UNSAFE_BUS = {
    "USB",
    "1394",
    "SD",
    "MMC",
    "iSCSI",
    "virtual",
    "fileBackedVirtual",
    "Spaces",
    "SCM",
}

_BUS_NAMES = {
    1: "SCSI",
    2: "ATAPI",
    3: "ATA",
    4: "1394",
    5: "SSA",
    6: "FIBRE",
    7: "USB",
    8: "RAID",
    9: "iSCSI",
    10: "SAS",
    11: "SATA",
    12: "SD",
    13: "MMC",
    14: "virtual",
    15: "fileBackedVirtual",
    16: "Spaces",
    17: "NVMe",
    18: "SCM",
    19: "UFS",
}

# ── Win32 绑定 ──────────────────────────────────────────────────────

_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

if IS_WINDOWS:

    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT

    _k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _k32.GetVolumeInformationW.restype = wintypes.BOOL

    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]

    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]

    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

_INVALID_HANDLE = ctypes.c_void_p(-1).value

_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_STORAGE_DEVICE_PROPERTY = 0
_OPEN_EXISTING = 3
_FILE_SHARE_READ_WRITE = 0x1 | 0x2

# ⚠️ 路径必须用 chr(92) 拼，不能写字符串字面量 ——
#    `\\.\X:` 这种形式在部分 shell / 转义链路下会被吃掉反斜杠，
#    结果是 CreateFileW 拿到非法路径、恒定报错 123（实测踩过）。
_BS = chr(92)


class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_int),
        ("QueryType", ctypes.c_int),
        ("AdditionalParameters", ctypes.c_byte * 1),
    ]


class _STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("DeviceType", ctypes.c_byte),
        ("DeviceTypeModifier", ctypes.c_byte),
        ("RemovableMedia", ctypes.c_byte),
        ("CommandQueueing", ctypes.c_byte),
        ("VendorIdOffset", ctypes.c_ulong),
        ("ProductIdOffset", ctypes.c_ulong),
        ("ProductRevisionOffset", ctypes.c_ulong),
        ("SerialNumberOffset", ctypes.c_ulong),
        ("BusType", ctypes.c_int),
        ("RawPropertiesLength", ctypes.c_ulong),
        ("RawProperties", ctypes.c_byte * 1),
    ]


def _bus_type(letter):
    """查盘符的总线类型。查不到返回 None（调用方按拒绝处理）。"""

    if not IS_WINDOWS:
        return None

    volume = _BS + _BS + "." + _BS + letter + ":"

    handle = _k32.CreateFileW(
        volume, 0, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None
    )

    if handle == _INVALID_HANDLE or not handle:
        return None

    try:

        query = _STORAGE_PROPERTY_QUERY(_STORAGE_DEVICE_PROPERTY, 0)

        buf = ctypes.create_string_buffer(1024)

        returned = wintypes.DWORD(0)

        ok = _k32.DeviceIoControl(
            handle,
            _IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query),
            ctypes.sizeof(query),
            buf,
            1024,
            ctypes.byref(returned),
            None,
        )

        if not ok:
            return None

        desc = ctypes.cast(buf, ctypes.POINTER(_STORAGE_DEVICE_DESCRIPTOR)).contents

        return _BUS_NAMES.get(desc.BusType, "bus_%d" % desc.BusType)

    finally:

        _k32.CloseHandle(handle)


def _volume_info(root):
    """返回 (盘类型, 文件系统名)；读不到文件系统时给 None。"""

    drive_type = _k32.GetDriveTypeW(ctypes.c_wchar_p(root))

    fs_buf = ctypes.create_unicode_buffer(261)

    ok = _k32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0, None, None, None, fs_buf, 261
    )

    return drive_type, (fs_buf.value.upper() if ok else None)


def recyclable_reason(path):
    """这个路径删掉能不能进回收站。

    返回 `(ok, reason)`：`ok=False` 时 `reason` 是给人看的中文原因。

    非 Windows 平台不做判断（没有盘符概念，交给 send2trash 自己处理）。
    """

    if not IS_WINDOWS:
        return True, ""

    if not path:
        return False, "路径为空"

    # 相对路径先绝对化，否则拿不到盘符
    target = os.path.abspath(path)

    drive = os.path.splitdrive(target)[0]

    if not drive:
        return False, "无法解析盘符，拒绝删除（拿不准就不动手）"

    root = drive + _BS

    letter = drive[0].upper()

    try:

        drive_type, fs = _volume_info(root)

    except Exception as exc:                                    # noqa: BLE001

        return False, "读不到卷信息（%s），拒绝删除" % exc

    # ── 第 1 层：盘类型 ──
    if drive_type != DRIVE_FIXED:

        name = _DRIVE_NAMES.get(drive_type, "未知类型(%d)" % drive_type)

        if drive_type == DRIVE_REMOTE:

            return False, "网络盘（%s）没有回收站，删了就找不回来" % name

        return False, "%s 没有回收站，删了就找不回来" % name

    # ── 第 2 层：文件系统 ──
    if fs is None:

        return False, "读不到文件系统类型，拒绝删除（拿不准就不动手）"

    if fs not in _RECYCLABLE_FS:

        return False, "文件系统 %s 不支持回收站（只有 NTFS / ReFS 有）" % fs

    # ── 第 3 层：总线（识别 U 盘 / 移动硬盘 —— 它们常被报成固定盘）──
    bus = _bus_type(letter)

    if bus is None:

        return False, "查不到磁盘总线类型，拒绝删除（拿不准就不动手）"

    if bus in _UNSAFE_BUS:

        return False, "这是移动盘 / 外接盘（%s），通常是备份盘，不在上面省空间" % bus

    return True, ""
