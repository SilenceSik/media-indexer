# -*- coding: utf-8 -*-
"""`adapters.javdb_cli_compat` 的单元测试 —— 仿真层的语义契约。

用**假的原生客户端**驱动，不联网，所以任何机器上都能确定复现。

## 这些测试锁住什么

仿真层的价值在于「让上层 15 个方法一行不改也能跑」，所以它必须忠实
复刻 CLI 的**命令行语义**，尤其是那些靠**报错文本**分支的地方：

| 用例 | 锁住的行为 |
|---|---|
| 歧义番号 | 必须非零退出 + 输出含「多个精确匹配」
  —— `detail_checked` 正是靠这句话去走 `_detail_ambiguous` 的 |
| 查不到 | 非零退出 + 输出含 `NOT_FOUND_MARKERS` 里的特征词
  —— 上层**靠它决定能不能删库**，错一个字就可能误删真番号 |
| `--id` | 按内部 id 取，不走番号解析 |
| `assets list` | 输出 `TYPE\\tURL` 的管道格式；无内容时非零退出 |
| `assets download` | **明确不支持** —— 不能假装成功 |
| JSON 不转义非 ASCII | 与 CLI 一致（`ensure_ascii=False`），
  否则下游取的日文标题会变成 `\\uXXXX` |
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from adapters.javdb_cli_compat import (  # noqa: E402
    JavDBCommandCompat,
    assets_download_supported,
)


# ═══════════════════════ 假的原生客户端

class FakeNative:
    """按需返回预设结果，并记录被调用的次数。"""

    def __init__(self, search=None, details=None, magnets=None,
                 comments=None):

        self._search = search or {}
        self._details = details or {}
        self._magnets = magnets or {}
        self._comments = comments or {}

        self.calls = []

    def search(self, token):
        self.calls.append(("search", token))
        return self._search.get(token, [])

    def detail(self, movie_id):
        self.calls.append(("detail", movie_id))
        return self._details.get(movie_id)

    def magnets(self, movie_id):
        self.calls.append(("magnets", movie_id))
        return self._magnets.get(movie_id, [])

    def comments(self, movie_id, page=1, limit=20):
        self.calls.append(("comments", movie_id, page, limit))
        return self._comments.get(movie_id, [])

    @staticmethod
    def cover_url(movie):
        return (movie or {}).get("cover_url")

    @staticmethod
    def preview_images(movie):
        return (movie or {}).get("_images") or []

    @staticmethod
    def preview_video_url(movie):
        return (movie or {}).get("preview_video_url")


def _movie(mid, number, **extra):
    d = {"id": mid, "number": number, "title": "タイトル {}".format(mid)}
    d.update(extra)
    return d


def _compat(**kw):

    fake = FakeNative(**kw)

    return JavDBCommandCompat(client=fake), fake


# ═══════════════════════ search

def test_search_returns_movies_key():
    c, _ = _compat(search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]})

    r = c.run(["search", "SSIS-001", "--json"])

    assert r.returncode == 0

    assert json.loads(r.stdout)["movies"][0]["id"] == "ZY5eq"


def test_search_honours_limit():
    movies = [_movie("a{}".format(i), "X-001") for i in range(10)]

    c, _ = _compat(search={"X-001": movies})

    r = c.run(["search", "X-001", "--limit", "3", "--json"])

    assert len(json.loads(r.stdout)["movies"]) == 3


def test_search_keeps_non_ascii_unescaped():
    """与 CLI 一致：日文标题不能变成 \\uXXXX。

    转义了不影响 json.loads，但会让读日志、比对输出的人看不懂，
    也与 CLI 的字节输出不一致。
    """

    c, _ = _compat(search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]})

    r = c.run(["search", "SSIS-001", "--json"])

    assert "タイトル" in r.stdout
    assert "\\u" not in r.stdout


# ═══════════════════════ detail

def test_detail_by_number_happy_path():
    c, _ = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        details={"ZY5eq": _movie("ZY5eq", "SSIS-001", duration=150)},
        magnets={"ZY5eq": [{"hash": "aa", "size": 100}]},
    )

    r = c.run(["detail", "SSIS-001", "--magnets", "--json"])

    assert r.returncode == 0

    d = json.loads(r.stdout)

    assert d["id"] == "ZY5eq"
    assert d["duration"] == 150
    assert d["magnets"][0]["hash"] == "aa"


def test_detail_without_magnets_flag_has_no_magnets_key():
    c, _ = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        details={"ZY5eq": _movie("ZY5eq", "SSIS-001")},
    )

    r = c.run(["detail", "SSIS-001", "--json"])

    assert "magnets" not in json.loads(r.stdout)


def test_ambiguous_number_reports_multiple_exact_matches():
    """**关键**：歧义必须非零退出 + 输出含「多个精确匹配」。

    `detail_checked` 正是靠这句文本去走 `_detail_ambiguous`；
    改成别的措辞或返回 0，歧义番号会被当成「查不到」——
    早前就因此让整批抓取落库 0。
    """

    c, _ = _compat(search={"ABF-001": [
        _movie("9bx8", "ABF-001"),
        _movie("76MM91", "ABF-001"),
    ]})

    r = c.run(["detail", "ABF-001", "--magnets", "--json"])

    assert r.returncode != 0
    assert "多个精确匹配" in (r.stdout + r.stderr)


def test_not_found_reports_marker():
    """查不到要带特征词 —— 上层靠它决定能不能删库。"""

    c, _ = _compat(search={"NOPE-999": []})

    r = c.run(["detail", "NOPE-999", "--magnets", "--json"])

    assert r.returncode != 0
    assert "没有找到" in (r.stdout + r.stderr)


def test_detail_by_id_skips_number_resolution():
    """`--id` 表示参数据已是内部 id，不该再去 search。"""

    c, fake = _compat(details={"ZY5eq": _movie("ZY5eq", "SSIS-001")})

    r = c.run(["detail", "ZY5eq", "--id", "--magnets", "--json"])

    assert r.returncode == 0
    assert json.loads(r.stdout)["id"] == "ZY5eq"

    assert not [x for x in fake.calls if x[0] == "search"], \
        "带 --id 时不该触发 search"


def test_detail_by_id_not_found():
    c, _ = _compat()

    r = c.run(["detail", "zzzz", "--id", "--json"])

    assert r.returncode != 0
    assert "没有找到" in (r.stdout + r.stderr)


def test_fuzzy_search_results_are_not_exact_matches():
    """搜索会给模糊结果，只有 number 完全相同的才算候选。

    不过滤会把**别的番号**的磁力并进来（数据污染）。
    """

    c, _ = _compat(search={"ABF-001": [
        _movie("aaa", "ABF-008"),
        _movie("bbb", "ABF-010"),
    ]})

    r = c.run(["detail", "ABF-001", "--magnets", "--json"])

    assert r.returncode != 0
    assert "没有找到" in (r.stdout + r.stderr)


# ═══════════════════════ comments

def test_comments_returns_reviews_key():
    c, fake = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        comments={"ZY5eq": [{"id": "r1", "content": "good"}]},
    )

    r = c.run(["comments", "SSIS-001", "--json", "--limit", "5", "--page", "2"])

    assert r.returncode == 0
    assert json.loads(r.stdout)["reviews"][0]["id"] == "r1"

    assert ("comments", "ZY5eq", 2, 5) in fake.calls


def test_comments_verbatim_reports_ambiguity():
    c, _ = _compat(search={"ABF-001": [
        _movie("9bx8", "ABF-001"),
        _movie("76MM91", "ABF-001"),
    ]})

    r = c.run(["comments", "ABF-001", "--json"])

    assert r.returncode != 0
    assert "多个精确匹配" in (r.stdout + r.stderr)


def test_comments_by_id():
    c, _ = _compat(comments={"9bx8": [{"id": "x"}]})

    r = c.run(["comments", "9bx8", "--id", "--json", "--limit", "3"])

    assert r.returncode == 0
    assert json.loads(r.stdout)["reviews"][0]["id"] == "x"


# ═══════════════════════ assets

def test_assets_list_emits_type_tab_url():
    """管道格式必须是 `TYPE<TAB>URL` —— 上层按 `\\t` 切。"""

    c, _ = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        details={"ZY5eq": _movie(
            "ZY5eq", "SSIS-001",
            cover_url="https://x/cover.jpg",
            preview_video_url="https://x/v.m3u8",
            _images=["https://x/s1.jpg", "https://x/s2.jpg"],
        )},
    )

    r = c.run(["assets", "list", "SSIS-001", "--type", "image"])

    assert r.returncode == 0

    lines = r.stdout.splitlines()

    assert lines[0] == "image\thttps://x/cover.jpg"
    assert "image\thttps://x/s1.jpg" in lines

    # image 类型不该混进 video
    assert not [x for x in lines if x.startswith("video\t")]


def test_assets_list_video_only():
    c, _ = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        details={"ZY5eq": _movie("ZY5eq", "SSIS-001",
                                 preview_video_url="https://x/v.m3u8")},
    )

    r = c.run(["assets", "list", "SSIS-001", "--type", "video"])

    assert r.returncode == 0
    assert r.stdout.strip() == "video\thttps://x/v.m3u8"


def test_assets_list_empty_is_nonzero():
    """没有资源时 CLI 非零退出 —— 上层会按「没下到」处理。"""

    c, _ = _compat(
        search={"SSIS-001": [_movie("ZY5eq", "SSIS-001")]},
        details={"ZY5eq": _movie("ZY5eq", "SSIS-001")},
    )

    r = c.run(["assets", "list", "SSIS-001", "--type", "image"])

    assert r.returncode != 0


def test_assets_download_is_explicitly_unsupported():
    """不得假装成功 —— 说清楚了，上层才能给准确提示。"""

    assert assets_download_supported() is False

    c, _ = _compat()

    r = c.run(["assets", "download", "-d", "somewhere"])

    assert r.returncode != 0
    assert "不支持" in (r.stdout + r.stderr) or "CLI" in (r.stdout + r.stderr)


# ═══════════════════════ 健壮性

def test_unknown_command_is_error():
    c, _ = _compat()

    r = c.run(["rankings", "movies"])

    assert r.returncode != 0


def test_empty_args_is_error():
    c, _ = _compat()

    assert c.run([]).returncode != 0


def test_upstream_error_becomes_failure_result():
    """上游抛错要变成非零退出 + stderr，**不能把异常漏给上层**。"""

    class Boom(FakeNative):

        def search(self, token):
            from core.javdb_native import JavDBNativeError
            raise JavDBNativeError("网络炸了")

    c = JavDBCommandCompat(client=Boom())

    r = c.run(["search", "SSIS-001", "--json"])

    assert r.returncode != 0
    assert "网络炸了" in r.stderr


def test_missing_token_is_error():
    c, _ = _compat()

    assert c.run(["detail", "--magnets", "--json"]).returncode != 0
