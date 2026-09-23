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
        """

        import re

        query = re.sub(r"^FC2-PPV-", "FC2-", number)

        try:

            result = self._run(["detail", query, "--magnets", "--json"])

            if result.returncode != 0 or not result.stdout.strip():

                return None

            return json.loads(result.stdout)

        except Exception:

            return None

    def assets(self, number, kind="image"):
        """列出媒体资产，返回 `[(type, url), ...]`。

        官方管道 `assets list` 的输出是 `TYPE<TAB>URL`（非 TTY 时）。
        前 2 条是 small_cover / cover，其余 `/samples/` 是正片截图。
        """

        try:

            result = self._run(["assets", "list", number, "--type", kind], timeout=90)

            if result.returncode != 0 or not result.stdout.strip():

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
