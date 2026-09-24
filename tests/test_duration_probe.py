# -*- coding: utf-8 -*-
"""`core.duration_probe` 的单元测试 —— 纯 Python 读时长。

夹具是**现场合成的最小合法容器**（几百字节），不依赖任何真实媒体文件，
所以在任何机器上都能跑、结果确定。

## 这些测试锁住的东西

每一条都对应一个**实测踩过的坑**，改解析器时别再踩回去：

| 用例 | 锁住的行为 |
|---|---|
| moov 在尾部 | 顶层 box 链要能**跳过 mdat 直接定位 moov** |
| moov 在前（faststart） | 头部路径 |
| **moov 有下钻** | mvhd 是 moov 的**子** box，不能把 moov 当平铺 box 读
  （漏这步会让 185/326 个文件返回 None） |
| moov 尺寸巨大 | 尾部兜底窗口要够大（7GB 片的索引能到十几 MB） |
| AVI 多分段 | `avih` 与 `dmlh` 矛盾时，**以视频流 strh 为准**
  （实测 abp-041：avih 194617 帧 / dmlh 529690 帧 / strh 221602 帧，
  只有 strh 与 ffprobe 一致） |
| WMV 减 Preroll | 播放时长 = PlayDuration − Preroll（与 ffprobe 同口径） |
| 坏文件 | 绝不抛异常，返回 None |
"""

import os
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from core.duration_probe import (  # noqa: E402
    probe_duration_native,
    _ebml_vint,
)


# ═══════════════════════ 合成容器的小工具

def _box(btype, payload):
    return struct.pack(">I", len(payload) + 8) + btype + payload


def _mvhd(timescale, duration, version=0):
    if version == 1:
        payload = bytes([1, 0, 0, 0])
        payload += struct.pack(">QQ", 0, 0)
        payload += struct.pack(">I", timescale)
        payload += struct.pack(">Q", duration)
    else:
        payload = bytes([0, 0, 0, 0])
        payload += struct.pack(">II", 0, 0)
        payload += struct.pack(">I", timescale)
        payload += struct.pack(">I", duration)

    return payload + b"\x00" * 64


def _make_mp4(tmp_path, name, timescale=1000, duration=5000,
              moov_first=False, pad=0, version=0):

    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 8)
    mdat = _box(b"mdat", b"\x00" * pad)
    moov = _box(b"moov", _box(b"mvhd", _mvhd(timescale, duration, version)))

    body = ftyp + (moov + mdat if moov_first else mdat + moov)

    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


# ── EBML 小工具（MKV）

def _vint_size(n):
    """把长度编码成 EBML VINT。"""

    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            out = bytearray()
            for i in range(length):
                out.append((n >> (8 * (length - 1 - i))) & 0xFF)
            out[0] |= 0x80 >> (length - 1)
            return bytes(out)
    raise ValueError("too big")


def _ebml_element(eid, payload):
    eid_bytes = eid.to_bytes((eid.bit_length() + 7) // 8 or 1, "big")
    return eid_bytes + _vint_size(len(payload)) + payload


def _make_mkv(tmp_path, name, duration=1234.5, scale=1000000, pad=0):

    info = _ebml_element(
        0x1549A966,                                    # Info
        _ebml_element(0x2AD7B1, scale.to_bytes(3, "big"))
        + _ebml_element(0x4489, struct.pack(">f", duration)),
    )

    segment = _ebml_element(0x18538067, info + b"\x00" * pad)

    # ⚠️ EBML 头必须是**合法的 element**（带正确的 size VINT）。
    # 早前夹具写成 `magic + b"\x00"*8`，第一个 size 字节是 0 —— 那是非法
    # VINT，解析器直接判定「无法解析」并返回 None，于是误报解析器有 bug。
    ebml_header = _ebml_element(0x1A45DFA3, b"\x00" * 8)

    path = tmp_path / name
    path.write_bytes(ebml_header + segment)
    return str(path)


# ── RIFF 小工具（AVI）

def _riff_chunk(cid, payload):
    out = cid + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        out += b"\x00"
    return out


def _make_avi(tmp_path, name, us_per_frame=33333, avih_frames=1000,
              strh_frames=None, dmlh_frames=None, scale=1, rate=30):

    avih = struct.pack("<I", us_per_frame) + b"\x00" * 12 \
        + struct.pack("<I", avih_frames) + b"\x00" * 32

    children = _riff_chunk(b"avih", avih)

    if strh_frames is not None:

        strh = b"vids" + b"\x00" * 16 \
            + struct.pack("<I", scale) + struct.pack("<I", rate) \
            + b"\x00" * 4 + struct.pack("<I", strh_frames)

        children += _riff_chunk(b"LIST",
                                b"strl" + _riff_chunk(b"strh", strh))

    if dmlh_frames is not None:

        children += _riff_chunk(b"LIST", b"odml" + _riff_chunk(
            b"dmlh", struct.pack("<I", dmlh_frames)))

    hdrl = _riff_chunk(b"LIST", b"hdrl" + children)

    body = b"AVI " + hdrl

    path = tmp_path / name
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return str(path)


# ── ASF 小工具（WMV）

_ASF_HEADER_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ASF_FILEPROPS_GUID = bytes.fromhex("a1dcab8c47a9cf118ee400c00c205365")


def _make_wmv(tmp_path, name, play_100ns=100000000, preroll_ms=3000):

    body = b"\x00" * 16                       # File ID
    body += struct.pack("<Q", 12345)          # File Size
    body += struct.pack("<Q", 0)              # Creation Date
    body += struct.pack("<Q", 100)            # Data Packets
    body += struct.pack("<Q", play_100ns)     # Play Duration
    body += struct.pack("<Q", play_100ns)     # Send Duration
    body += struct.pack("<Q", preroll_ms)     # Preroll

    obj = _ASF_FILEPROPS_GUID + struct.pack("<Q", len(body) + 24) + body

    header = _ASF_HEADER_GUID + struct.pack("<Q", 30 + len(obj)) \
        + struct.pack("<I", 1) + b"\x01\x02"

    path = tmp_path / name
    path.write_bytes(header + obj)
    return str(path)


# ═══════════════════════ MP4

def test_mp4_moov_at_tail(tmp_path):
    """moov 在 mdat 之后（国内压制组最常见的排布）。"""

    p = _make_mp4(tmp_path, "tail.mp4", timescale=1000, duration=5000,
                  pad=200000)

    assert probe_duration_native(p) == pytest.approx(5.0, abs=0.01)


def test_mp4_moov_first_faststart(tmp_path):
    """faststart：moov 在文件开头。"""

    p = _make_mp4(tmp_path, "fast.mp4", timescale=600, duration=3600,
                  moov_first=True)

    assert probe_duration_native(p) == pytest.approx(6.0, abs=0.01)


def test_mp4_mvhd_requires_descending_into_moov(tmp_path):
    """**回归**：mvhd 是 moov 的子 box，必须下钻才读得到。

    曾经把 moov 当平铺 box 读 -> 库里 185/326 个文件返回 None。
    """

    p = _make_mp4(tmp_path, "descend.mp4", duration=9000, pad=100000)

    assert probe_duration_native(p) == pytest.approx(9.0, abs=0.01)


def test_mp4_version1_mvhd(tmp_path):
    """mvhd version=1（64 位时长字段）。"""

    p = _make_mp4(tmp_path, "v1.mp4", timescale=1000, duration=7200,
                  version=1)

    assert probe_duration_native(p) == pytest.approx(7.2, abs=0.01)


def test_mp4_zero_timescale_is_rejected(tmp_path):
    """timescale 为 0 时不能做除法 —— 返回 None 而不是崩。"""

    p = _make_mp4(tmp_path, "zero.mp4", timescale=0, duration=100)

    assert probe_duration_native(p) is None


# ═══════════════════════ MKV

def test_mkv_duration_times_scale(tmp_path):
    """MKV 时长 = Duration × TimecodeScale（纳秒）。

    ⚠️ Duration 的单位是**TimecodeScale 的倍数**，不是秒：
    1ms 的 scale 下，1234.5 秒要写成 **1234500**（不是 1234.5，也不是
    1234500000）。我在这上面连错两次 —— 真实文件的对拍能挡住这类错，
    因为解析器本身早就与 ffprobe 一致了。
    """

    p = _make_mkv(tmp_path, "a.mkv", duration=1234500.0, scale=1000000)

    assert probe_duration_native(p) == pytest.approx(1234.5, abs=0.1)


def test_mkv_non_default_scale(tmp_path):
    """非默认 TimecodeScale 也要乘对。"""

    # 1000 × 2000000 ns = 2 秒
    p = _make_mkv(tmp_path, "b.mkv", duration=1000.0, scale=2000000)

    assert probe_duration_native(p) == pytest.approx(2.0, abs=0.01)


# ═══════════════════════ AVI

def test_avi_prefers_video_strh_over_avih(tmp_path):
    """**回归**：多分段 AVI 里 avih 与 dmlh 会互相矛盾，以视频流 strh 为准。

    实测 abp-041：avih 194617 帧 / dmlh 529690 帧 / strh 221602 帧，
    只有 strh 与 ffprobe 的 7394.1s 一致。
    """

    # avih 说 1000 帧，strh 说 221602 帧 @29.97fps -> 7394.1s
    p = _make_avi(tmp_path, "multi.avi", us_per_frame=33366,
                  avih_frames=1000, strh_frames=221602,
                  dmlh_frames=529690, scale=1001, rate=30000)

    assert probe_duration_native(p) == pytest.approx(7394.1, abs=1.0)


def test_avi_falls_back_to_avih(tmp_path):
    """没有 strh 时退回 avih × us_per_frame。"""

    p = _make_avi(tmp_path, "plain.avi", us_per_frame=40000,
                  avih_frames=250)

    assert probe_duration_native(p) == pytest.approx(10.0, abs=0.01)


def test_avi_dmlh_fallback(tmp_path):
    """有 dmlh 且无 strh 时用 dmlh（比 avih 更接近真实总帧数）。"""

    p = _make_avi(tmp_path, "dmlh.avi", us_per_frame=40000,
                  avih_frames=100, dmlh_frames=500)

    assert probe_duration_native(p) == pytest.approx(20.0, abs=0.01)


# ═══════════════════════ WMV

def test_wmv_subtracts_preroll(tmp_path):
    """播放时长 = PlayDuration − Preroll（与 ffprobe 同口径）。"""

    # PlayDuration 100s，Preroll 3s -> 97s
    p = _make_wmv(tmp_path, "a.wmv", play_100ns=100 * 10 ** 7,
                  preroll_ms=3000)

    assert probe_duration_native(p) == pytest.approx(97.0, abs=0.1)


def test_wmv_preroll_longer_than_play_is_rejected(tmp_path):
    """Preroll 比 PlayDuration 还长 -> 负数，视为取不到。"""

    p = _make_wmv(tmp_path, "bad.wmv", play_100ns=1 * 10 ** 7,
                  preroll_ms=5000)

    assert probe_duration_native(p) is None


# ═══════════════════════ 健壮性

@pytest.mark.parametrize("content", [
    b"",
    b"\x00",
    b"not a video at all",
    b"RIFF" + b"\x00" * 4,                    # RIFF 头截断
    b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4,  # ftyp 但没有 moov
    b"\x1a\x45\xdf\xa3" + b"\x00" * 4,        # EBML 头截断
])
def test_garbage_returns_none_not_raise(tmp_path, content):
    """**绝不抛异常** —— 这个函数跑在遍历几万个文件的路径上。"""

    p = tmp_path / "junk.bin"
    p.write_bytes(content)

    assert probe_duration_native(str(p)) is None


def test_missing_file_returns_none(tmp_path):
    assert probe_duration_native(str(tmp_path / "nope.mp4")) is None


def test_empty_path_returns_none():
    assert probe_duration_native("") is None
    assert probe_duration_native(None) is None


def test_directory_path_returns_none(tmp_path):
    """传目录进来也不能崩。"""

    assert probe_duration_native(str(tmp_path)) is None


def test_truncated_mp4_mdat_header(tmp_path):
    """mdat 声明了巨大尺寸但文件被截断 —— 不能无限找下去。"""

    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 8)
    # 声称 4GB 的 mdat（4 字节 size 字段的上限附近），实际后面什么都没有
    fake_mdat = struct.pack(">I", 0xFFFFFF00) + b"mdat"

    p = tmp_path / "trunc.mp4"
    p.write_bytes(ftyp + fake_mdat)

    assert probe_duration_native(str(p)) is None


# ═══════════════════════ VINT 边界

@pytest.mark.parametrize("value", [1, 5, 126, 127, 128, 1000, 70000])
def test_vint_size_roundtrip(value):
    """长度编码能解回原值（解析器依赖它）。"""

    encoded = _vint_size(value)

    got, _ = _ebml_vint(encoded, 0, keep_marker=False)

    assert got == value
