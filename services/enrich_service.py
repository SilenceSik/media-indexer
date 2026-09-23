"""元数据抓取服务：番号 -> JavDB 元数据 + 磁力 + 封面 + 内容截图。

**只在用户显式触发时运行**（Web 上的按钮 / CLI 脚本），不做扫描后自动抓取 ——
自动抓取会在用户没要求时联网、耗时且难以预期。

落库一律走 core.database_v2.Database，不自己拼 SQL。
"""

import json
import os

from adapters.javdb_adapter import JavDBCLIClient


class EnrichService:

    def __init__(
        self,
        db,
        covers_dir,
        screenshots_dir,
        client=None
    ):

        self.db = db

        self.covers_dir = covers_dir

        self.screenshots_dir = screenshots_dir

        self.client = client or JavDBCLIClient()

    # ------------------------------------------------------------ 对外

    def enrich(self, number, want="all"):
        """抓取并落库一个番号。

        want: "all"     -> 元数据 + 磁力 + 封面 + 截图
              "magnets" -> 只抓磁力（+ 元数据，因为 detail 一次返回）
        """

        result = {
            "number": number,
            "metadata": False,
            "magnets": 0,
            "cover": None,
            "screenshots": 0,
            "error": None,
        }

        detail = self.client.detail(number)

        if not detail:

            result["error"] = "javdb 查不到该番号"

            return result

        # ── 素材：封面 + 截图（want=all 才抓，失败不影响主记录）──
        cover_local = None
        screenshot_names = []

        if want == "all":

            try:

                cover_local, screenshot_names = self.fetch_media(number)

            except Exception as exc:                            # noqa: BLE001

                result["error"] = f"素材下载失败：{exc}"

        # ── 元数据 ──
        acts = detail.get("actors") or []

        tags = detail.get("tags") or []

        self.db.save_metadata(
            {
                "number": number,
                "title": detail.get("title") or detail.get("origin_title"),
                "cover": detail.get("cover_url"),
                "cover_local": cover_local,
                "release_date": detail.get("release_date"),
                "maker": detail.get("maker_name"),
                "actresses": acts,
                "tags": tags,
                "screenshots": screenshot_names,
            }
        )

        result["metadata"] = True

        result["cover"] = cover_local

        result["screenshots"] = len(screenshot_names)

        # ── 磁力 ──
        magnets = detail.get("magnets")

        if not magnets:

            magnets = detail.get("magnets_list") or []

        if isinstance(magnets, list):

            for m in magnets:

                if not isinstance(m, dict):

                    continue

                uri = self.magnet_uri(m)

                if not uri:

                    continue

                size_text = self.size_text(m.get("size"))

                self.db.add_magnet(
                    number,
                    uri,
                    source=m.get("name") or "",
                    size_text=size_text,
                    verified=1,
                    commit=False,
                )

                result["magnets"] += 1

            self.db.conn.commit()

        return result

    # ------------------------------------------------------------ 工具

    @staticmethod
    def magnet_uri(m):
        """从 JavDB 的磁力对象拼 magnet URI。

        优先用现成的 magnet 字段；否则用 hash 拼（含 dn 名称）。
        """

        uri = m.get("magnet") or m.get("link")

        if uri:

            return uri

        h = m.get("hash")

        if not h:

            return None

        uri = "magnet:?xt=urn:btih:" + str(h)

        if m.get("name"):

            uri += "&dn=" + str(m["name"])

        return uri

    @staticmethod
    def size_text(size_mb):
        """JavDB 的 size 单位是 **MB**（实测 5880 -> 5.74 GB），不是字节。"""

        if not size_mb:

            return ""

        try:

            value = float(size_mb)

        except (TypeError, ValueError):

            return str(size_mb)

        if value >= 1024:

            return f"{value / 1024:.2f} GB"

        return f"{value:.0f} MB"

    def fetch_media(self, number):
        """下载封面 + 内容截图，返回 (封面文件名, 截图文件名列表)。

        官方管道 `assets list --type image` 给三类：
          small_cover / cover / samples（正片截图，约 10 张）
        前 2 条当封面，其余 /samples/ 当截图。
        """

        assets = self.client.assets(number, "image")

        if not assets:

            return None, []

        urls = [f"{t}\t{u}" for t, u in assets]

        cover_lines = urls[:2]

        shot_lines = [l for l in urls[2:] if "/samples/" in l]

        cover_name = None

        shot_names = []

        cover_dir = os.path.join(self.covers_dir, number)

        if cover_lines and not self._has_files(cover_dir):

            got = self.client.download_assets(cover_lines, cover_dir)

            if got:

                got.sort(
                    key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
                    reverse=True,
                )

                cover_name = os.path.basename(got[0])

        elif cover_lines:

            files = self._files(cover_dir)

            if files:

                files.sort(
                    key=lambda p: os.path.getsize(p),
                    reverse=True,
                )

                cover_name = os.path.basename(files[0])

        shot_dir = os.path.join(self.screenshots_dir, number)

        if shot_lines and not self._has_files(shot_dir):

            got = self.client.download_assets(shot_lines, shot_dir)

            shot_names = [os.path.basename(p) for p in got]

        elif shot_lines:

            shot_names = [os.path.basename(p) for p in self._files(shot_dir)]

        return cover_name, shot_names

    @staticmethod
    def _files(directory):

        if not os.path.isdir(directory):

            return []

        return [
            os.path.join(directory, n)
            for n in sorted(os.listdir(directory))
            if os.path.isfile(os.path.join(directory, n))
        ]

    @staticmethod
    def _has_files(directory):

        return bool(EnrichService._files(directory))
