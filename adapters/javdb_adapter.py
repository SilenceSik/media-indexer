import json
import subprocess

import time

# 截图缺失重试。2026-09-24 实测：批量跑时 `assets list` 偶发只返回封面两条，
# 第 3 条起的 /samples/ 全丢（FC2-PPV-1115273 批量 0 张、单跑 10 张）。
# 重试 3 次、每次退避 sleep 1s * n，避免撞上同一时刻的服务端抖动。
ASSETS_ATTEMPTS = 3

ASSETS_RETRY_WAIT = 1.0


class JavDBAdapter:

    def __init__(
        self,
        client
    ):

        self.client = client

    def search(
        self,
        number
    ):

        result = self.client.search(
            number
        )

        if not result:

            return None

        return {

            "number":
            number,

            "title":
            result.get(
                "title"
            ),

            "cover":
            result.get(
                "cover"
            ),

            "actresses":
            result.get(
                "actresses",
                []
            ),

            "tags":
            result.get(
                "tags",
                []
            ),

            "maker":
            result.get(
                "maker"
            )

        }


class JavDBCLIClient:

    def __init__(
        self,
        command="javdb",
        extra_path=None,  # 装好 javdb CLI 后放进 PATH 即可
        timeout=120,
        home=None
    ):

        self.command = command

        self.extra_path = extra_path

        self.timeout = timeout

        # 隔离 HOME：给了就覆盖 HOME/USERPROFILE，让子进程读另一份
        # `~/.javdb-cli/`（**没有 auth.json**）。
        #
        # 用途是**并发**：磁力查询实测不需要登录态，隔离后既不会并发写坏
        # auth.json，也没有登录账号可被风控（主人 2026-09-24 定的方案）。
        self.home = home

    def _env(self):
        """javdb 装在用户 bin 目录，子进程要能找得到。"""

        import os

        env = dict(os.environ)

        if self.extra_path and self.extra_path not in env.get("PATH", ""):

            env["PATH"] = env.get("PATH", "") + ";" + self.extra_path

        if self.home:

            env["HOME"] = self.home
            env["USERPROFILE"] = self.home

        return env

    def _run(self, args, timeout=None):

        result = subprocess.run(
            [self.command] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env(),
            timeout=timeout or self.timeout,
            shell=False,
        )

        return result

    def search(
        self,
        number
    ):

        try:

            result = self._run(["search", self._cli_number(number), "--json"], timeout=60)

            if result.returncode != 0:

                return None

            return json.loads(
                result.stdout
            )

        except Exception:

            return None

    def reviews(self, number, limit=20, page=1):
        """抓一页评论。返回 `[{content,score,likes_count,username,created_at,id}]`。

        官方 `comments NUMBER [--id] [--json] [--limit N] [--page N]`。
        歧义番号（多个精确匹配）要先定到具体 id，否则 CLI 直接报错 —— 这里
        复用 `_search_exact_ids` 挑一个，跟 assets 的回退路子一致。

        **失败返回 []**，不抛 —— 评论是展示用的旁路数据，抓不到不该弄挂页面。
        """

        try:

            result = self._run(
                ["comments", self._cli_number(number),
                 "--json", "--limit", str(int(limit)),
                 "--page", str(int(page))],
                timeout=90,
            )

            if result.returncode != 0 or not (result.stdout or "").strip():

                err = (result.stdout or "") + (result.stderr or "")

                if "多个精确匹配" in err:

                    ids = self._search_exact_ids(number)

                    for mid in ids:

                        r2 = self._run(
                            ["comments", mid, "--id", "--json",
                             "--limit", str(int(limit))]
                        )

                        if r2.returncode == 0 and (r2.stdout or "").strip():

                            return (json.loads(r2.stdout) or {}).get("reviews") or []

                return []

            return (json.loads(result.stdout) or {}).get("reviews") or []

        except Exception:                                       # noqa: BLE001

            return []

    def preview_video_url(self, number):
        """宣传视频的直接地址。没有就 None。

        歧义番号走 detail 的 `preview_video_url` 字段（与 assets 回退同源）。
        """

        try:

            r = self._run(
                ["assets", "list", self._cli_number(number), "--type", "video"],
                timeout=90,
            )

            if r.returncode == 0 and (r.stdout or "").strip():

                for line in r.stdout.splitlines():

                    if "\t" in line:

                        _t, url = line.split("\t", 1)

                        if (url or "").strip():

                            return url.strip()

            for mid in self._search_exact_ids(number):

                try:

                    d = json.loads(
                        self._run(["detail", mid, "--id", "--json"]).stdout
                    )

                except Exception:                               # noqa: BLE001
                    continue

                if d.get("preview_video_url"):

                    return d["preview_video_url"]

        except Exception:                                       # noqa: BLE001
            pass

        return None

    # 「确定查不到」的判据 —— 只有命中这些才算番号不存在。
    #
    # ⚠️ 这条区分**至关重要**：`detail()` 在「网络故障」和「番号真的不存在」
    # 两种情况下都返回 None。如果调用方直接按 None 判定「查不到」并据此
    # 删除入库记录，一次网络抖动就会把**真番号**清掉。
    #
    # 所以只有返回码非零**且**输出里出现这些特征词，才敢说「不存在」。
    NOT_FOUND_MARKERS = (
        "找不到番号",
        "没有找到",
        "not found",
        "no result",
        "404",
    )

    def detail_checked(self, number):
        """比 `detail()` 多一层区分：返回 `(detail, status)`。

        status:
          * `"ok"`        拿到数据
          * `"notfound"`  **确定**番号不存在（可以据此按 D8 剔除）
          * `"error"`     网络/超时/解析失败 —— **绝不能据此删任何东西**
        """

        import re

        query = self._cli_number(number)

        try:

            result = self._run(["detail", query, "--magnets", "--json"])

        except Exception as exc:                                # noqa: BLE001

            return None, "error:{}".format(type(exc).__name__)

        out = (result.stdout or "") + (result.stderr or "")

        if result.returncode == 0 and (result.stdout or "").strip():

            try:
                return json.loads(result.stdout), "ok"
            except Exception:                                   # noqa: BLE001
                return None, "error:parse"

        if "多个精确匹配" in out:

            got = self._detail_ambiguous(query)

            return (got, "ok") if got else (None, "error:ambiguous")

        # 返回码非零 —— 是「不存在」还是「请求出问题」，看输出特征
        low = out.lower()

        if any(m in low or m in out for m in self.NOT_FOUND_MARKERS):

            return None, "notfound"

        # 认不出来的一律当 error —— 保守，宁可漏剔不可错剔
        return None, "error:unrecognized"

    def detail(self, number):
        """一次拿全：元数据 + 磁力（`detail <番号> --magnets --json`）。

        实测单条约 2.6s，不要拆成 detail + magnets 两次调用。
        FC2 写法归一：`FC2-PPV-N` -> `FC2-N`（JavDB 内部编号）。

        **同番号多个精确匹配时不能当作查不到** —— CLI 会报
        「番号 X 有多个精确匹配」并非零退出（有意设计，见 javdb-cli skill
        陷阱 4/9）。实测 ABF-001 / ABF-087 这类老片号常有两三个版本
        （有码/无厂牌/中字），早前把这种当 notfound 处理，导致整批抓取
        落库 0。这里走 search 拿候选 id 再逐个取。

        需要区分「查不到」与「请求失败」时用 `detail_checked()`。
        """

        return self.detail_checked(number)[0]

    def _detail_legacy(self, number):
        """旧的 detail 实现（保留备查，不再使用）。"""

        import re

        query = self._cli_number(number)

        try:

            result = self._run(["detail", query, "--magnets", "--json"])

            if result.returncode == 0 and result.stdout.strip():

                return json.loads(result.stdout)

            if "多个精确匹配" in (result.stdout + result.stderr):

                return self._detail_ambiguous(query)

            return None

        except Exception:

            return None

    def _search_exact_ids(self, number):
        """search 出**精确匹配**的影片 ID 列表。

        ⚠️ `search` 会连模糊结果一起返回（实测搜 `ABF-001` 给 20 条，
        只有前 2 条 number 真的是 `ABF-001`，其余是 ABF-008/ABF-010…）。
        不过滤就会出现两个后果：一是把**别的番号**的磁力并进来（数据污染），
        二是白抓十几个无关条目（慢）。
        """

        try:

            s = self._run(
                    ["search", self._cli_number(number), "--limit", "20", "--json"],
                    timeout=60,
                )

            if s.returncode != 0 or not s.stdout.strip():

                return []

            movies = json.loads(s.stdout).get("movies") or []

        except Exception:

            return []

        want = number.upper().replace(" ", "")

        out = []

        for m in movies:

            if not isinstance(m, dict):

                continue

            got = (m.get("number") or "").upper().replace(" ", "")

            if got == want and m.get("id"):

                out.append(m["id"])

        return out

    def _detail_ambiguous(self, number):
        """番号对应多个影片 ID：取回全部版本并合并磁力。

        为什么合并而不是只取一个：这些版本是**同一部片的发行变体**
        （有码/无厂牌/中字），番号相同。磁力信息本身没有歧义 —— 多拿到
        几版本只会让「这片还能不能重新获取」的判断更充分。
        `ambiguous_versions` 记录版本数，供上层按需做更细的对齐。
        """

        ids = self._search_exact_ids(number)

        if not ids:

            return None

        merged = None

        magnets = []

        for mid in ids:

            try:

                r = self._run(["detail", mid, "--id", "--magnets", "--json"])

                if r.returncode != 0 or not r.stdout.strip():

                    continue

                d = json.loads(r.stdout)

            except Exception:

                continue

            if merged is None:

                merged = d

            ms = d.get("magnets")

            if isinstance(ms, list):

                magnets.extend(ms)

        if merged is None:

            return None

        # 按 hash 去重（同一磁力可能在多版本里都出现）
        seen = set()

        unique = []

        for m in magnets:

            if not isinstance(m, dict):

                continue

            key = m.get("hash") or m.get("magnet")

            if key and key not in seen:

                seen.add(key)

                unique.append(m)

        merged["magnets"] = unique

        merged["ambiguous_versions"] = len(ids)

        return merged

    def _cli_number(self, number):
        """把番号转成 CLI 认的写法。

        ⚠️ **所有走 CLI 的调用都必须过这里。** 实测 FC2 的坑：

            detail   FC2-1003647       -> ok
            detail   FC2-PPV-1003647   -> 找不到番号
            assets   FC2-1003647       -> 12 个资产
            assets   FC2-PPV-1003647   -> 找不到番号

        即 CLI 内部编号是 `FC2-<数字>`，不认 `FC2-PPV-<数字>` 这种网页
        写法。早前只有 `detail()` 做了这层归一，`assets()` 和 `search()`
        直接传原始番号 —— 结果 **15 个 FC2 全都抓到了标题和磁力，却一个
        封面、一张截图都没有**：`assets list` 报「找不到番号」返回空列表，
        下载环节自然无物可下。
        """

        import re

        return re.sub(r"^FC2-PPV-", "FC2-", number or "")

    def _assets_once(self, number, kind="image"):
        """列出媒体资产，返回 `[(type, url), ...]`。

        官方管道 `assets list` 的输出是 `TYPE<TAB>URL`（非 TTY 时）。
        前 2 条是 small_cover / cover，其余 `/samples/` 是正片截图。

        **歧义番号走 detail 字段回退** —— `assets list` 只吃番号、且没有
        `--id` 参数（实测 `unknown flag: --id`），传影片 id 也会
        `找不到番号`。所以歧义时改用 `detail <id> --id --json` 返回的
        cover_url / preview_images 拼出 URL 列表，再喂给同一条官方下载管道
        （Referer / 并发 / 重试仍由 CLI 自理）。
        """

        query = self._cli_number(number)

        try:

            result = self._run(
                ["assets", "list", query, "--type", kind], timeout=90
            )

            if result.returncode != 0 or not result.stdout.strip():

                if "多个精确匹配" in (result.stdout + result.stderr):

                    return self._assets_from_detail(number, kind)

                return []

        except Exception:

            return []

        out = []

        for line in result.stdout.splitlines():

            if "\t" not in line:

                continue

            ctype, url = line.split("\t", 1)

            out.append((ctype.strip(), url.strip()))

        return out

    def _has_preview_images(self, number):
        """detail 里说这部有没有截图。判断不了就返回 None。

        用来区分「assets 结果被截断了」和「这部本来就没截图」——
        **不知道就不重试**，别为了保险白花两次网络调用。
        """

        try:

            d = self.detail(number)

        except Exception:                                       # noqa: BLE001

            return None

        if not isinstance(d, dict):

            return None

        if "has_preview_images" in d:

            return bool(d["has_preview_images"])

        return None

    def assets(self, number, kind="image", attempts=None):
        """列出媒体资产；**结果像是被截断就重试**。

        2026-09-24 修的偶发 bug：批量跑时 `assets list` 有时只回封面那两条，
        第 3 条起的 `/samples/` 全丢 —— 下游 `shot_lines` 于是为空，表现为
        「有封面、0 截图」。实证 FC2-PPV-1115273 批量跑 0 张、单跑立刻 10 张。

        判据用 detail 的 `has_preview_images`：**只有它说有截图、而 assets
        没给到 `/samples/`** 才重试。这样：
          * 真被截断 → 重试（绝大多数第二次就正常）
          * 本来就没截图 → 一次都不多花

        重试仍拿不到就**原样返回**（不编造），由调用方按「没截图」处理。
        """

        n = ASSETS_ATTEMPTS if attempts is None else int(attempts)

        n = max(1, n)

        for i in range(n):

            out = self._assets_once(number, kind)

            if kind != "image":

                return out

            if not out:

                return out

            # 有 /samples/ 就没事（注意 assets 行是 TYPE<TAB>URL）
            if any("/samples/" in (u or "") for _t, u in out):

                return out

            if i == n - 1:

                return out

            if self._has_preview_images(number) is False:

                # 明说没有截图，那就是没截图，别再试
                return out

            time.sleep(ASSETS_RETRY_WAIT * (i + 1))

        return out

    def _assets_from_detail(self, number, kind="image"):
        """歧义番号：从 detail 字段拼出资产 URL 列表。

        多个版本里**挑截图最多的那个当正身** —— 实测有 preview_images 的
        版本不一定是第一个：
          ABF-001: 76MM91 有 11 张 / 9bx8 有 0 张
          ABF-087: VwGnq 有 0 张  / NQwPYw 有 13 张
        取第一个会拿到没有截图的那版。
        """

        best = None

        best_previews = -1

        for mid in self._search_exact_ids(number):

            try:

                r = self._run(["detail", mid, "--id", "--json"])

                if r.returncode != 0 or not r.stdout.strip():

                    continue

                d = json.loads(r.stdout)

            except Exception:

                continue

            previews = d.get("preview_images") or []

            if len(previews) > best_previews:

                best_previews = len(previews)

                best = d

        if not best:

            return []

        out = []

        if kind == "image":

            if best.get("cover_url"):

                out.append(("image", best["cover_url"]))

            if best.get("thumb_url"):

                out.append(("image", best["thumb_url"]))

            for p in best.get("preview_images") or []:

                if not isinstance(p, dict):

                    continue

                url = p.get("large_url") or p.get("thumb_url")

                if url:

                    out.append(("image", url))

        elif best.get("preview_video_url"):

            out.append(("video", best["preview_video_url"]))

        return out

    def download_assets(self, lines, directory, timeout=300, filename=None):
        """把 `assets list` 的行下载到 directory，返回落盘路径列表。

        `assets download` 目标不能已存在（CLI 有意设计），所以调用方
        必须按「目录已存在即跳过」处理，否则第二次跑会整体失败。

        `filename`：给了就用 `-o <directory>/<filename>` 指定落盘名。
        `assets list` 的默认落盘名由 CLI 自己决定（封面/截图那样正好），
        但**宣传视频要在模板里用固定路径引用**，所以得能点名。
        """

        import os

        if not lines:

            return []

        os.makedirs(directory, exist_ok=True)

        if filename:

            cmd = [self.command, "assets", "download",
                   "-o", os.path.join(directory, filename)]

        else:

            cmd = [self.command, "assets", "download", "-d", directory]

        try:

            result = subprocess.run(
                cmd,
                input="\n".join(lines) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
                timeout=timeout,
                shell=False,
            )

        except Exception:

            return []

        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
