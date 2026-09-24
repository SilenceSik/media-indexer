# -*- coding: utf-8 -*-
"""JavDB CLI 兼容层 —— 用原生 HTTP 客户端**仿真 `javdb` 命令的 stdout**。

## 为什么要做「仿真」而不是重写适配器

`adapters/javdb_adapter.py` 有 15 个方法，全都建立在 CLI 的**命令行语义**上：

    `_run([...]).returncode` / `.stdout` / `.stderr`

而且不少分支依赖 CLI 的**人类可读报错文本**，例如：

    if "多个精确匹配" in (result.stdout + result.stderr): ...

重写适配器 = 把这 15 个方法的语义在 Python 里重做一遍，再用真实语料
逐条验证。而这一层只需要**产出同样的 stdout**，上层一行不用改 ——
风险面小一个量级。

## 一致性（2026-09-24 实测）

API 的 movie 对象与 CLI 的 `detail --json` **只差 `magnets` 一个键**
（API 的磁力要单独查一次 `/magnets`）。其余 38 个字段逐值一致，
连键名大小写、`size` 的单位（MB）都相同 —— CLI 本来就是转发这个 API。

## 覆盖范围

| 命令 | 支持 |
|---|---|
| `search <番号> --json` | ✅ |
| `search <番号> --limit N --json` | ✅ |
| `detail <番号> --magnets --json` | ✅ |
| `detail <id> --id --magnets --json` | ✅ |
| `comments <番号> --json --limit --page` | ✅ |
| `comments <id> --id --json --limit` | ✅ |
| `assets list <番号> --type image\\|video` | ✅（返回 `TYPE\tURL` 行） |
| `assets download` | ❌ **不支持** —— 见下 |

## ⚠️ `assets download` 故意不实现

它要把 HLS（m3u8 + AES-128 分片）重封装成 mp4。那是**几百行解复用代码**
（上游 javdb-cli 的 `internal/javdb/appapi/media/` 有 4356 行），正是本次
移植明确要**跳过**的部分。

所以宣传视频这块**保留 javdb CLI 作为可选依赖**：需要录宣传视频的人
装 CLI；不需要的人（只查元数据与磁力）完全不用装。
`assets_download_supported()` 让上层能据此给出准确提示，而不是静默失败。
"""

import json
import re

from core.javdb_native import (
    JavDBNativeClient,
    JavDBNativeError,
    normalize_number,
)


class _Result:
    """冒充 `subprocess.CompletedProcess`（只用到这几个属性）。"""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def assets_download_supported():
    """本层能不能下载资源（录宣传视频）。**否** —— 见模块文档。"""

    return False


# JavDB 内部编号的形态：字母数字混合、通常 5-8 位（如 `ZY5eq`、`NQwPYw`）
_MOVIE_ID_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")


def _looks_like_movie_id(token):

    if not token:
        return False

    # 番号里带 `-`，内部 id 不带
    if "-" in token or "_" in token:
        return False

    return bool(_MOVIE_ID_RE.match(token))


def _dump(obj):
    """与 CLI 一致的输出：UTF-8 JSON（不转义非 ASCII）。"""

    return json.dumps(obj, ensure_ascii=False)


class JavDBCommandCompat:
    """把 native 调用包装成「像 CLI 那样返回 CompletedProcess」。"""

    def __init__(self, client=None, **kwargs):

        self.client = client or JavDBNativeClient(**kwargs)

        # 番号 -> [(id, number)]，避免同一番号反复 search
        self._resolve_cache = {}

    # ── 内部：把番号或 id 定位到 movie 对象 ──

    def _resolve(self, token, is_id=False):
        """返回候选 [(id, number)]。"""

        if is_id:
            return [(token, None)]

        if token in self._resolve_cache:
            return self._resolve_cache[token]

        movies = self.client.search(token)

        cands = []

        want = normalize_number(token).upper().replace(" ", "")

        for m in movies:

            mid = m.get("id")
            num = (m.get("number") or "").upper().replace(" ", "")

            if mid and num == want:
                cands.append((mid, m.get("number")))

        self._resolve_cache[token] = cands

        return cands

    # ── 仿真入口 ──

    def run(self, args, timeout=None):
        """`args` 与传给 CLI 的参数列表一致（不含命令名）。"""

        args = [str(a) for a in args]

        if not args:
            return _Result(2, "", "缺少参数")

        verb = args[0]

        try:

            if verb == "search":
                return self._cmd_search(args)

            if verb == "detail":
                return self._cmd_detail(args)

            if verb == "comments":
                return self._cmd_comments(args)

            if verb == "assets":
                return self._cmd_assets(args)

        except JavDBNativeError as e:

            return _Result(1, "", "请求失败：{}".format(e))

        except Exception as e:                                    # noqa: BLE001

            return _Result(1, "", "{}: {}".format(type(e).__name__, e))

        return _Result(2, "", "不支持的命令：{}".format(verb))

    # ── search ──

    def _cmd_search(self, args):

        token = args[1] if len(args) > 1 else ""

        limit = None

        for i, a in enumerate(args):

            if a == "--limit" and i + 1 < len(args):

                try:
                    limit = int(args[i + 1])
                except ValueError:
                    limit = None

        movies = self.client.search(token)

        # CLI 的 `search --json` 形状就是 {"movies": [...]}
        payload = {"movies": movies[:limit] if limit else movies}

        return _Result(0, _dump(payload))

    # ── detail ──

    def _cmd_detail(self, args):

        is_id = "--id" in args

        with_magnets = "--magnets" in args

        tokens = [a for a in args[1:] if not a.startswith("--")]

        if not tokens:
            return _Result(2, "", "缺少番号")

        token = tokens[0]

        # 歧义番号：CLI 会报这句并且**非零退出**（有意设计）。
        # 上层靠这句话去走 `_detail_ambiguous`，所以要原样复刻。
        if is_id:

            movie = self.client.detail(token)

            if not movie:
                return _Result(3, "", "番号 {} 没有找到".format(token))

        else:

            cands = self._resolve(token)

            if not cands:
                return _Result(3, "", "番号 {} 没有找到".format(token))

            if len(cands) > 1:
                return _Result(
                    1, "",
                    "番号 {} 有多个精确匹配：{}".format(
                        token, ", ".join(c[0] for c in cands)))

            movie = self.client.detail(cands[0][0])

            if not movie:
                return _Result(3, "", "番号 {} 没有找到".format(token))

        out = dict(movie)

        if with_magnets:

            mid = out.get("id")

            out["magnets"] = self.client.magnets(mid) if mid else []

        return _Result(0, _dump(out))

    # ── comments ──

    def _cmd_comments(self, args):

        is_id = "--id" in args

        limit = 20
        page = 1

        for i, a in enumerate(args):

            if a == "--limit" and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    pass

            if a == "--page" and i + 1 < len(args):
                try:
                    page = int(args[i + 1])
                except ValueError:
                    pass

        tokens = [a for a in args[1:] if not a.startswith("--")]

        if not tokens:
            return _Result(2, "", "缺少番号")

        token = tokens[0]

        if is_id:

            mid = token

        else:

            cands = self._resolve(token)

            if not cands:
                return _Result(3, "", "番号 {} 没有找到".format(token))

            if len(cands) > 1:
                return _Result(
                    1, "",
                    "番号 {} 有多个精确匹配：{}".format(
                        token, ", ".join(c[0] for c in cands)))

            mid = cands[0][0]

        reviews = self.client.comments(mid, page=page, limit=limit)

        return _Result(0, _dump({"reviews": reviews}))

    # ── assets list ──

    def _cmd_assets(self, args):

        if len(args) < 2:
            return _Result(2, "", "缺少子命令")

        sub = args[1]

        if sub == "download":

            return _Result(
                1, "",
                "assets download 需要 javdb CLI（本层不支持 HLS 重封装）")

        if sub != "list":
            return _Result(2, "", "不支持：assets {}".format(sub))

        kind = None

        for i, a in enumerate(args):

            if a == "--type" and i + 1 < len(args):
                kind = args[i + 1]

        tokens = [a for a in args[2:] if not a.startswith("--")]

        # `--type video` 后面那个值是 kind，不算位置参数
        if kind:
            tokens = [t for t in tokens if t != kind]

        if not tokens:
            return _Result(2, "", "缺少番号")

        token = tokens[0]

        cands = self._resolve(token)

        if not cands:
            return _Result(3, "", "番号 {} 没有找到".format(token))

        movie = self.client.detail(cands[0][0])

        if not movie:
            return _Result(3, "", "番号 {} 没有找到".format(token))

        lines = []

        if kind in (None, "image"):

            cover = self.client.cover_url(movie)

            if cover:
                lines.append("image\t{}".format(cover))

            for url in self.client.preview_images(movie):
                lines.append("image\t{}".format(url))

        if kind in (None, "video"):

            video = self.client.preview_video_url(movie)

            if video:
                lines.append("video\t{}".format(video))

        # 上游是 `TYPE<TAB>URL` 的管道格式；没内容时 CLI 非零退出
        if not lines:
            return _Result(1, "", "没有可用的资源")

        return _Result(0, "\n".join(lines))
