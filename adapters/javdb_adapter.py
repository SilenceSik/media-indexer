import json
import subprocess


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
        extra_path=r"C:\Users\Administrator\bin",
        timeout=120
    ):

        self.command = command

        self.extra_path = extra_path

        self.timeout = timeout

    def _env(self):
        """javdb 装在用户 bin 目录，子进程要能找得到。"""

        import os

        env = dict(os.environ)

        if self.extra_path and self.extra_path not in env.get("PATH", ""):

            env["PATH"] = env.get("PATH", "") + ";" + self.extra_path

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

            result = self._run(["search", number, "--json"], timeout=60)

            if result.returncode != 0:

                return None

            return json.loads(
                result.stdout
            )

        except Exception:

            return None

    def detail(self, number):
        """一次拿全：元数据 + 磁力（`detail <番号> --magnets --json`）。

        实测单条约 2.6s，不要拆成 detail + magnets 两次调用。
        FC2 写法归一：`FC2-PPV-N` -> `FC2-N`（JavDB 内部编号）。

        **同番号多个精确匹配时不能当作查不到** —— CLI 会报
        「番号 X 有多个精确匹配」并非零退出（有意设计，见 javdb-cli skill
        陷阱 4/9）。实测 ABF-001 / ABF-087 这类老片号常有两三个版本
        （有码/无码/中字），早前把这种当 notfound 处理，导致整批抓取
        落库 0。这里走 search 拿候选 id 再逐个取。
        """

        import re

        query = re.sub(r"^FC2-PPV-", "FC2-", number)

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

            s = self._run(["search", number, "--limit", "20", "--json"], timeout=60)

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
        （有码/无码/中字），番号相同。磁力信息本身没有歧义 —— 多拿到
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

    def assets(self, number, kind="image"):
        """列出媒体资产，返回 `[(type, url), ...]`。

        官方管道 `assets list` 的输出是 `TYPE<TAB>URL`（非 TTY 时）。
        前 2 条是 small_cover / cover，其余 `/samples/` 是正片截图。

        **歧义番号走 detail 字段回退** —— `assets list` 只吃番号、且没有
        `--id` 参数（实测 `unknown flag: --id`），传影片 id 也会
        `找不到番号`。所以歧义时改用 `detail <id> --id --json` 返回的
        cover_url / preview_images 拼出 URL 列表，再喂给同一条官方下载管道
        （Referer / 并发 / 重试仍由 CLI 自理）。
        """

        try:

            result = self._run(["assets", "list", number, "--type", kind], timeout=90)

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

    def download_assets(self, lines, directory, timeout=300):
        """把 `assets list` 的行下载到 directory，返回落盘路径列表。

        `assets download` 目标不能已存在（CLI 有意设计），所以调用方
        必须按「目录已存在即跳过」处理，否则第二次跑会整体失败。
        """

        import os

        if not lines:

            return []

        os.makedirs(directory, exist_ok=True)

        try:

            result = subprocess.run(
                [self.command, "assets", "download", "-d", directory],
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
