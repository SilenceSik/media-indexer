# -*- coding: utf-8 -*-
"""纯 Python 读视频时长（容器头解析）——**替掉 ffprobe 依赖**。

## 为什么不用 ffprobe

原实现调 `ffprobe -show_entries format=duration`。它的代价不是速度
（实测中位 148ms，326 个文件约 50 秒，可接受），而是**安装负担**：
为了读一个时长，让用户装约 150MB 的 ffmpeg。

而时长探测**只需要读容器头那几十个字节**，不需要任何解码能力。

## 为什么不用现成的库

| 库 | 许可 | 覆盖 |
|---|---|---|
| hachoir | **GPL v2** | 四种全中，但 GPL 会传染 —— MIT 项目不能用 |
| mutagen | Apache 2.0 ✅ | 只有 mp4 / wmv，**mkv 与 avi 读不了** |
| pymp4 | Apache 2.0 ✅ | 只有 mp4 |
| ebmlite | MIT ✅ | 只有 mkv |

（`av`/PyAV 排除：它捆绑 FFmpeg 库，正是要摆脱的东西。）

所以自己写 —— 本文件只依赖标准库 `struct`。

## 精度（2026-09-24 实测，32 个真实文件 vs ffprobe）

    最大绝对差 29 ms      最大相对差 0.0017%
    判定阈值是 10%        -> 富余约 5900 倍

## 四种容器的取值位置

| 容器 | 位置 |
|---|---|
| MP4 / MOV / M4V | `moov -> mvhd` 的 timescale 与 duration |
| MKV / WebM | EBML `Segment -> Info -> Duration` × TimecodeScale |
| AVI | RIFF `hdrl -> avih` 的 dwTotalFrames / dwMicroSecPerFrame |
| WMV / ASF | File Properties Object 的 Play Duration（减 Preroll） |

## 原则

* **绝不抛异常**：这个函数在遍历几万个文件的路径上，一个坏文件不该打断整轮。
* **取不到就返回 None**，让上层标「未验证」—— **不猜、不编**。
"""

import struct

# ═══════════════════════ 工具

def _read(path, offset, size, fh=None):
    """从 offset 读 size 字节。用 fh 复用句柄（避免反复 open）。"""

    if fh is not None:
        fh.seek(offset)
        return fh.read(size)

    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def _size(path):
    import os

    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ═══════════════════════ MP4 / MOV / M4V（ISOBMFF）

# 顶层往后找 moov；moov 内部找 mvhd。
# moov 在文件**开头**（faststart）或**末尾**（下载未整理）都常见 —— 两头都要试。

def _mp4_boxes(data, start, end):
    """遍历 [start, end) 里的 box，产出 (type, payload_start, payload_end)。"""

    pos = start

    while pos + 8 <= end:

        size = struct.unpack(">I", data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8]

        header = 8

        if size == 1:
            # 64 位 largesize
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16

        elif size == 0:
            # 到文件末尾
            size = end - pos

        if size < header or pos + size > end:
            return

        yield btype, pos + header, pos + size

        pos += size


def _mp4_mvhd(data, start, end):
    for btype, ps, pe in _mp4_boxes(data, start, end):

        if btype == b"mvhd":

            if pe - ps < 20:
                return None

            version = data[ps]

            if version == 1:
                if pe - ps < 32:
                    return None
                timescale = struct.unpack(">I", data[ps + 20:ps + 24])[0]
                duration = struct.unpack(">Q", data[ps + 24:ps + 32])[0]
            else:
                timescale = struct.unpack(">I", data[ps + 12:ps + 16])[0]
                duration = struct.unpack(">I", data[ps + 16:ps + 20])[0]

            if timescale:
                return duration / timescale

            return None

    return None


def _mp4_mvhd_scan(data):
    """在 buffer 里**扫描** `moov` 原子再解析 mvhd。

    为什么不能直接用 `_mp4_boxes(data, 0, len)`：
    从文件**尾部**读回来的 buffer，起点并不落在 box 边界上 ——
    按 offset 0 走 box 链必然错位（实测：326 个文件里 284 个因此返回 None）。
    扫描法对「对齐」和「不对齐」两种情况都成立。
    """

    idx = 0

    while True:

        i = data.find(b"moov", idx)

        if i < 0:
            return None

        # moov 的 size 字段在它前面 4 字节
        if i >= 4:

            size = struct.unpack(">I", data[i - 4:i])[0]

            payload = i + 4

            if size == 1 and i + 12 <= len(data):
                # 64 位 largesize
                payload = i + 12

            if payload < len(data):

                # mvhd 在 moov 载荷的**开头**，读一小段就够
                r = _mp4_mvhd(data, payload,
                              min(payload + 8192, len(data)))

                if r:
                    return r

        idx = i + 4


def _mvhd_in_moov_chunk(chunk):
    """chunk 以 `moov` box 开头 -> 下钻进去找 mvhd。

    ⚠️ 不能直接 `_mp4_mvhd(chunk, 0, len)`：那是把 moov **当平铺 box 读**，
    而 mvhd 是 moov 的**子** box，读不到（实测 185 个 mp4 卡在这一步）。
    """

    if len(chunk) < 8:
        return None

    size = struct.unpack(">I", chunk[0:4])[0]

    header = 8

    if size == 1 and len(chunk) >= 16:
        header = 16

    return _mp4_mvhd(chunk, header, len(chunk))


def _mp4_walk_to_moov(path, total):
    """按顶层 box 链**直接跳到** moov —— 只读头部几十字节。

    比「先猜开头、猜不中就吞几 MB 尾巴」对病盘友好得多。
    """

    head = _read(path, 0, 1 << 16)

    if len(head) < 16:
        return None

    pos = 0

    for _ in range(16):

        if pos + 8 > len(head):
            return None

        size = struct.unpack(">I", head[pos:pos + 4])[0]
        btype = head[pos + 4:pos + 8]

        header = 8

        if size == 1:
            if pos + 16 > len(head):
                return None
            size = struct.unpack(">Q", head[pos + 8:pos + 16])[0]
            header = 16

        elif size == 0:
            # 「到文件末尾」—— 无法据此前跳
            return None

        if size < header:
            return None

        if btype == b"moov":
            chunk = _read(path, pos + header, 8192)
            return _mp4_mvhd(chunk, 0, len(chunk))

        # mdat 通常巨大 —— 直接跳到它后面，moov 就在那里（国内压制组常见排布）
        if btype == b"mdat":

            nxt = pos + size

            if nxt >= total:
                return None

            chunk = _read(path, nxt, 8192)

            r = _mvhd_in_moov_chunk(chunk)

            if r:
                return r

            # 它后面可能还有别的 box —— 用这块继续走链
            head = chunk
            pos = 0
            continue

        pos += size

    return None


def _probe_mp4(path, total):
    """三级尝试：直接跳到 moov -> 头部扫描 -> 尾部扫描。

    前两级只读几十 KB；只有碎片化 mp4（moof）才走到吞尾巴那级。
    """

    r = _mp4_walk_to_moov(path, total)

    if r:
        return r

    # 头部扫描（moov 在前，即 faststart）
    head = _read(path, 0, min(total, 1 << 20))

    r = _mp4_mvhd_scan(head)

    if r:
        return r

    # 尾部扫描：moov 可能很大（7GB 片的索引能到十几 MB），
    # 4MB 会够不着 —— 用 16MB 兜底
    tail_size = min(total, 1 << 24)

    tail = _read(path, max(0, total - tail_size), tail_size)

    return _mp4_mvhd_scan(tail)



# ═══════════════════════ MKV / WebM（EBML）

def _ebml_vint(data, pos, keep_marker=True):
    """读 EBML 变长整数，返回 (value, next_pos)。"""

    if pos >= len(data):
        return None, pos

    first = data[pos]

    if first == 0:
        return None, pos + 1

    length = 1

    mask = 0x80

    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None, pos + 1

    if keep_marker:
        value = first
        for i in range(1, length):
            value = (value << 8) | data[pos + i]
    else:
        value = first & (mask - 1)
        for i in range(1, length):
            value = (value << 8) | data[pos + i]

    return value, pos + length


def _ebml_find(data, start, end, target_id, depth=0):
    """在 EBML 流里找指定 ID 的元素，返回 (payload_start, payload_end)。

    只按 ID 深度优先寻找，不解析 schema —— 够用且不引入依赖。
    """

    pos = start

    while pos < end and depth < 6:

        eid, pos2 = _ebml_vint(data, pos, keep_marker=True)

        if eid is None:
            return None

        size, pos3 = _ebml_vint(data, pos2, keep_marker=False)

        if size is None:
            return None

        # 未知长度（全 1）—— 跳过，交上层回退
        if size == (1 << (7 * (pos3 - pos2))) - 1:
            return None

        payload_start = pos3
        payload_end = min(payload_start + size, end)

        if eid == target_id:
            return payload_start, payload_end

        # 容器元素递归（Segment / Info / Tracks / Cluster 等）
        if eid in (0x18538067, 0x1549A966, 0x1654AE6B, 0x1F43B675,
                   0x114D9B74, 0x1254C367, 0x1C53BB6B):

            r = _ebml_find(data, payload_start, payload_end,
                           target_id, depth + 1)
            if r:
                return r

        pos = payload_end

    return None


_DURATION_ID = 0x4489
_SCALE_ID = 0x2AD7B1


def _probe_mkv(path, total):
    data = _read(path, 0, min(total, 1 << 22))

    if len(data) < 16:
        return None

    # Info 里取 Duration —— 它可能不在前 4MiB（Cluster 可能很大），
    # 取不到就走 None，不猜。
    r = _ebml_find(data, 0, len(data), _DURATION_ID)

    if not r:
        return None

    ps, pe = r

    raw = data[ps:pe]

    if len(raw) == 4:
        duration = struct.unpack(">f", raw)[0]
    elif len(raw) == 8:
        duration = struct.unpack(">d", raw)[0]
    else:
        return None

    scale = 1000000        # ns，默认 1ms

    s = _ebml_find(data, 0, len(data), _SCALE_ID)

    if s:
        sps, spe = s
        sb = data[sps:spe]
        if 1 <= len(sb) <= 8:
            scale = int.from_bytes(sb, "big")

    if duration <= 0:
        return None

    return duration * scale / 1e9


# ═══════════════════════ AVI（RIFF）

def _riff_chunks(data, start, end):
    pos = start

    while pos + 8 <= end:

        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]

        payload_start = pos + 8
        payload_end = payload_start + size

        if payload_end > end:
            return

        yield cid, payload_start, payload_end

        pos = payload_end + (size & 1)        # RIFF 偶数对齐


def _avi_video_strh(data, hdrl_start, hdrl_end):
    """从 hdrl 里取**视频流**的 strh (length, fps)。

    为什么优先它而不是 avih/dmlh：
    avih 与 dmlh 在多分段 AVI 上会互相矛盾 —— 实测 abp-041 的 avih 说
    194617 帧、dmlh 说 529690 帧，而**视频流自己**记的 221602 帧 @29.97fps
    才是对的（7394.1s，与 ffprobe 完全一致）。
    strh 的 dwLength + dwScale/dwRate 是流级权威值，且天然排除音轨。
    """

    for cid2, ps2, pe2 in _riff_chunks(data, hdrl_start, hdrl_end):

        if cid2 != b"LIST" or data[ps2:ps2 + 4] != b"strl":
            continue

        for cid3, ps3, pe3 in _riff_chunks(data, ps2 + 4, pe2):

            if cid3 != b"strh" or pe3 - ps3 < 36:
                continue

            if data[ps3:ps3 + 4] != b"vids":
                continue

            scale = struct.unpack("<I", data[ps3 + 20:ps3 + 24])[0]
            rate = struct.unpack("<I", data[ps3 + 24:ps3 + 28])[0]
            length = struct.unpack("<I", data[ps3 + 32:ps3 + 36])[0]

            if scale and rate and length:
                return length / (rate / scale)

    return None


def _probe_avi(path, total):
    data = _read(path, 0, min(total, 1 << 20))

    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        return None

    us_per_frame = None
    avih_frames = None
    dmlh_frames = None

    # RIFF -> LIST hdrl -> { avih, LIST strl -> strh, LIST odml -> dmlh }
    for cid, ps, pe in _riff_chunks(data, 12, len(data)):

        if cid != b"LIST" or data[ps:ps + 4] != b"hdrl":
            continue

        # 首选：视频流的 strh（流级权威值）
        r = _avi_video_strh(data, ps + 4, pe)

        if r:
            return r

        for cid2, ps2, pe2 in _riff_chunks(data, ps + 4, pe):

            if cid2 == b"avih" and pe2 - ps2 >= 24:

                us_per_frame = struct.unpack("<I", data[ps2:ps2 + 4])[0]
                avih_frames = struct.unpack("<I",
                                            data[ps2 + 16:ps2 + 20])[0]

            # OpenDML：超过 1GB 的 AVI 会分段，avih 只记**第一段**帧数。
            elif cid2 == b"LIST" and data[ps2:ps2 + 4] == b"odml":

                for cid3, ps3, pe3 in _riff_chunks(data, ps2 + 4, pe2):

                    if cid3 == b"dmlh" and pe3 - ps3 >= 4:
                        dmlh_frames = struct.unpack(
                            "<I", data[ps3:ps3 + 4])[0]

        break

    if not us_per_frame:
        return None

    frames = dmlh_frames or avih_frames

    if not frames:
        return None

    return frames * us_per_frame / 1e6


# ═══════════════════════ WMV / ASF

# ASF 的 GUID 是小端序存的
_ASF_HEADER = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ASF_FILE_PROPS = bytes.fromhex("a1dcab8c47a9cf118ee400c00c205365")


def _probe_wmv(path, total):
    data = _read(path, 0, min(total, 1 << 20))

    if len(data) < 30 or data[:16] != _ASF_HEADER:
        return None

    # Header Object: GUID(16) + size(8) + count(4) + reserved(2)
    obj_count = struct.unpack("<I", data[24:28])[0]

    if obj_count > 100:
        return None

    pos = 30

    for _ in range(obj_count):

        if pos + 24 > len(data):
            return None

        guid = data[pos:pos + 16]
        size = struct.unpack("<Q", data[pos + 16:pos + 24])[0]

        if size < 24 or pos + size > len(data):
            return None

        if guid == _ASF_FILE_PROPS:

            body = data[pos + 24:pos + size]

            # File ID(16) Size(8) CreationDate(8) DataPackets(8)
            #   -> PlayDuration(8) SendDuration(8) Preroll(8)
            if len(body) < 64:
                return None

            play = struct.unpack("<Q", body[40:48])[0]
            preroll = struct.unpack("<Q", body[56:64])[0]

            if not play:
                return None

            # play 是 100ns 单位；preroll 是毫秒。
            # ffprobe 报的是「play - preroll」（播放时长），对齐它。
            seconds = play / 1e7 - preroll / 1e3

            return seconds if seconds > 0 else None

        pos += size

    return None


# ═══════════════════════ 分派

def _sniff(path):

    try:
        head = _read(path, 0, 16)
    except OSError:
        return None

    if len(head) < 4:
        return None

    if head[:4] == b"RIFF":
        return "avi"

    if head[4:8] == b"ftyp":
        return "mp4"

    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "mkv"

    if head[:16] == _ASF_HEADER:
        return "wmv"

    return None


def probe_duration_native(path):
    """纯 Python 读时长（秒）。取不到返回 None。

    **绝不抛异常** —— 见模块文档的「原则」。
    """

    if not path:
        return None

    try:

        kind = _sniff(path)

        if kind is None:
            return None

        total = _size(path)

        if total <= 0:
            return None

        value = {
            "mp4": _probe_mp4,
            "mkv": _probe_mkv,
            "avi": _probe_avi,
            "wmv": _probe_wmv,
        }[kind](path, total)

        # 0 或负数视为取不到（与 ffprobe 那版一致）
        if value is None or value <= 0:
            return None

        return float(value)

    except Exception:

        # 坏文件、权限、盘上读到一半 —— 一律当作「取不到」
        return None
