# -*- coding: utf-8 -*-
"""JavBus 适配器（本机自托管 API，**javdb 查不到时的兜底**）。

部署与端点细节见 skill `devops/javbus-api-ops`：本机
`X:\\Apps\\javbus-api`，监听 `http://127.0.0.1:8922`。

## 为什么要适配层

JavBus 的字段名与 JavDB 完全不同，而且**磁力名在 `title`、番号在 `id`**。
下游（enrich / 判定器 / 门控）都按 JavDB 的形状写，所以这里负责翻译：

| 含义 | JavDB | JavBus |
|---|---|---|
| 番号 | `number` | **`id`** |
| 磁力名 | `name` | **`title`**（`name` 是空的） |
| 磁力 hash | `hash` | **`id`** |
| 磁力链接 | — | `link` |
| 封面 | `cover_url` | `img` |
| 日期 | `release_date` | `date` |
| 厂牌 | `maker_name` | `producer.name` / `publisher.name` |
| 演员 / 标签 | `actors` / `tags` | `stars` / `genres` |
| 体积 | `size`（**MB**） | `numberSize`（**字节**）+ `size`（文本） |
| 评论数 | `comments_count` | **没有** |

## 兜底特有的取舍

* **没有评论数** -> `comments_count` 留 None。后果：靠 JavBus 收录的番号
  定档拿不到「极高」（`tier_of` 在评论为 None 时给「高」）。
  这是**安全方向**：不凭没有的数据声称极高。
* **有 `videoLength`（分钟）** —— JavDB 那边没有。留着，将来做
  「文件↔元数据对应」校验（比对本地视频时长）时用得上。
* 两步取磁力：先详情拿 `gid`/`uc`，再查磁力。服务端强校验这两个参数。
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class JavBusClient:
    """本机 javbus-api 客户端。接口对齐 `JavDBCLIClient.detail()`。"""

    def __init__(self, base="http://127.0.0.1:8922", timeout=90):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ 底层

    def _api(self, path, **params):
        """一次 GET。任何异常都返回 None —— 兜底路径不该把主流程带崩。"""

        url = self.base + path

        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        )

        try:

            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))

        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                ValueError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------ 对外

    def available(self):
        """服务在不在。不在就别走兜底，省得每次白等超时。"""

        return self._api("/api/movies", page=1) is not None

    def detail(self, number):
        """番号 -> 与 `JavDBCLIClient.detail()` 同形的 dict；查不到返回 None。"""

        if not number:
            return None

        d = self._api("/api/movies/{}".format(
            urllib.parse.quote(str(number).strip())
        ))

        if not isinstance(d, dict) or not d.get("id"):
            return None

        return {
            # JavBus 的 id 就是番号
            "number": d.get("id"),

            "title": d.get("title"),
            "origin_title": d.get("title"),

            "cover_url": d.get("img"),

            "release_date": d.get("date"),

            "maker_name": (
                ((d.get("producer") or {}).get("name"))
                or ((d.get("publisher") or {}).get("name"))
                or ((d.get("director") or {}).get("name"))
            ),

            "actors": [
                {"name": s.get("name")}
                for s in (d.get("stars") or [])
                if isinstance(s, dict) and s.get("name")
            ],

            "tags": [
                {"name": g.get("name")}
                for g in (d.get("genres") or [])
                if isinstance(g, dict) and g.get("name")
            ],

            # JavBus 不提供评论数 —— 留 None，别编一个 0
            # （0 会让 tier_of 把它当「有数据但为零」，None 才是「没数据」）
            "comments_count": None,

            # 兜底来源标记，落库后能看出这条是谁给的
            "lookup_source": "javbus",

            # JavDB 没有的字段，留着给「文件↔元数据对应」校验用
            "video_length": d.get("videoLength"),

            "magnets": self._magnets(d),
        }

    def _magnets(self, d):
        """两步取磁力，并翻译成 JavDB 形状。

        JavBus 磁力对象的字段：`id`(btih) / `link` / `title`(名字) /
        `size`(文本 "7.74GB") / `numberSize`(字节) / `isHD` / `hasSubtitle`。

        下游要的是 `{hash, name, size(MB)}`，这里补齐。
        """

        gid = d.get("gid")
        uc = d.get("uc")

        if gid is None or uc is None:
            return []

        raw = self._api(
            "/api/magnets/{}".format(urllib.parse.quote(str(d["id"]))),
            gid=gid,
            uc=uc,
        )

        if not isinstance(raw, list):
            return []

        out = []

        for m in raw:

            if not isinstance(m, dict):
                continue

            out.append({
                "hash": m.get("id"),
                "name": m.get("title") or "",
                "size": self._to_mb(m),
                # 原样带上，将来做「磁力名形态多样度」之类的证据维度用得上
                "magnet": m.get("link"),
                "is_hd": m.get("isHD"),
                "has_subtitle": m.get("hasSubtitle"),
                "share_date": m.get("shareDate"),
            })

        return out

    @staticmethod
    def _to_mb(m):
        """体积统一成 **MB**（与 JavDB 一致；下游 `size_text()` 按 MB 解释）。

        `numberSize` 是字节，优先用它；没有就从文本 `size`（"7.74GB"）反解。
        两个都没有时返回 None —— 宁可缺，不要编 0。
        """

        n = m.get("numberSize")

        if isinstance(n, (int, float)) and n > 0:
            return n / 1024.0 / 1024.0

        text = (m.get("size") or "").strip().upper()

        if not text:
            return None

        mult = None

        for unit, k in (("TB", 1024.0 ** 2), ("GB", 1024.0),
                        ("MB", 1.0), ("KB", 1.0 / 1024)):
            if text.endswith(unit):
                mult = k
                text = text[:-len(unit)]
                break

        if mult is None:
            return None

        try:
            return float(text.strip()) * mult
        except ValueError:
            return None
