# -*- coding: utf-8 -*-
"""截图缺失重试：`assets()` 偶发只回封面两条时要自己重试。

背景（2026-09-24）：批量跑时 `assets list` 有时只返回封面那两条，
第 3 条起的 `/samples/` 全丢 —— 下游 `shot_lines` 于是为空，表现为
「有封面、0 截图」。实证 FC2-PPV-1115273 批量跑 0 张、单跑立刻 10 张。

判据用 detail 的 `has_preview_images`：只有它说有截图、而 assets 没给到
`/samples/` 才重试。这样「本来就没截图」的片子一次都不多花。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adapters.javdb_adapter as M  # noqa: E402
from adapters.javdb_adapter import ASSETS_ATTEMPTS, JavDBCLIClient  # noqa: E402

COVER_ONLY = [("image", "https://x/covers/a.jpg"),
              ("image", "https://x/small_covers/a.jpg")]

FULL = COVER_ONLY + [("image", "https://x/samples/a_l_0.jpg"),
                     ("image", "https://x/samples/a_l_1.jpg")]


class Probe:
    """替身：记录调用次数，按脚本吐结果。"""

    def __init__(self, sequence, has_preview_images):
        self.sequence = list(sequence)
        # ⚠️ 不能叫 has_preview —— 会和下面的同名方法互相覆盖，
        # 于是 `self._has_preview_images(...)` 变成「对 bool 做调用」。
        self.preview_images = has_preview_images
        self.assets_calls = 0
        self.preview_checks = 0

    def assets_once(self, number, kind="image"):
        self.assets_calls += 1

        if not self.sequence:
            return [("video", "https://x/preview.m3u8")]

        if len(self.sequence) > 1:
            return self.sequence.pop(0)

        return self.sequence[0]

    def has_preview_images(self, number):
        self.preview_checks += 1

        return self.preview_images


def _run(sequence, has_preview, kind="image", attempts=ASSETS_ATTEMPTS):
    """挂上替身跑一次 `assets()`；把 sleep 打掉免得测试变慢。"""

    p = Probe(sequence, has_preview)

    cli = JavDBCLIClient.__new__(JavDBCLIClient)      # 不跑 __init__（不碰子进程）

    cli._assets_once = p.assets_once
    cli._has_preview_images = p.has_preview_images

    real_sleep = M.time.sleep
    M.time.sleep = lambda _s: None

    try:
        out = JavDBCLIClient.assets(cli, "X-1", kind=kind, attempts=attempts)
    finally:
        M.time.sleep = real_sleep

    return out, p


def _has_samples(out):
    return any("/samples/" in (u or "") for _t, u in out)


def test_happy_path_no_retry():
    """第一次就正常 —— 不重试，也不查 detail。"""

    out, p = _run([FULL], True)

    assert p.assets_calls == 1
    assert p.preview_checks == 0, "正常情况不该多花一次 detail 调用"
    assert _has_samples(out)


def test_truncated_then_ok_retries_and_succeeds():
    """被截断 -> 重试一次就拿到截图。"""

    out, p = _run([COVER_ONLY, FULL], True)

    assert p.assets_calls == 2, "应该重试到第 2 次"
    assert _has_samples(out)


def test_persistently_truncated_gives_up_honestly():
    """一直截断 -> 试满次数后**原样返回**，不编造截图。"""

    out, p = _run([COVER_ONLY], True, attempts=ASSETS_ATTEMPTS)

    assert p.assets_calls == ASSETS_ATTEMPTS
    assert not _has_samples(out), "拿不到就是拿不到"
    assert out == COVER_ONLY, "应原样返回封面两条"


def test_no_preview_images_skips_retry_entirely():
    """detail 明说没有截图 -> 一次都不重试（省调用）。

    这条是「别为了保险白花两次网络调用」的守卫 —— 批量抓时差别很大。
    """

    out, p = _run([COVER_ONLY], False)

    assert p.assets_calls == 1, "本来没截图不该重试"
    assert p.preview_checks == 1, "应该查过一次 detail 才排除"
    assert not _has_samples(out)


def test_empty_result_is_final():
    """空结果视为明确答案，不重试。"""

    out, p = _run([[]], True)

    assert p.assets_calls == 1
    assert out == []


def test_video_kind_not_subject_to_retry():
    """视频资产不套这套截图逻辑（它本来也没有 /samples/）。"""

    out, p = _run([], True, kind="video", attempts=3)

    assert p.assets_calls == 1
    assert out and out[0][0] == "video"
