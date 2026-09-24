# -*- coding: utf-8 -*-
"""JavBus 适配器（**javdb 查不到时的兜底**）。

两个后端，产出同样的 JSON 形状，所以下面的翻译层两边共用：

  native  -> `core.javbus_native`，直接抓 JavBus 网页（**默认**，零外部服务）
  service -> 调自托管的 javbus-api，默认 `http://127.0.0.1:8922`

在 Web UI 的 `/settings` 里切换，或用环境变量 `LMM_JAVBUS_BACKEND`。

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
import os
import urllib.error
import urllib.parse
import urllib.request


# ═══════════════════════ 后端开关（2026-09-24）

# `JavBusClient` 现在有两个后端，**产出同样形状**，所以翻译层（本文件）
# 两边共用：
#
#   service -> 调本机自托管 `javbus-api`（原行为，仍是默认）
#   native  -> `core.javbus_native` 直接抓网页（零外部服务）
#
# 默认仍是 `service`：**不改变现有行为**，等对拍通过再切。
#
# 配置：config.yaml 的 `javbus.backend`，或环境变量
# `LMM_JAVBUS_BACKEND`（环境变量优先，便于临时试验）。
_VALID_BACKENDS = ("service", "native")


def resolve_backend(configured=None):
    """决定用哪个后端。

    优先级：构造参数 > 环境变量 `LMM_JAVBUS_BACKEND` > config.yaml > 默认
    service。
    """

    if configured:
        value = str(configured).strip().lower()
        if value in _VALID_BACKENDS:
            return value

    from core.datasource_config import get_backend

    # 默认 native：直接抓网页，不必先部署那个 Node 服务。
    # 实测与 service 后端 6 个番号字段全一致（11/11 + 磁力 hash 集合全同）。
    # ⚠️ native 需要配代理（JavBus 国内直连超时）—— 设置页可填。
    return get_backend("javbus", "LMM_JAVBUS_BACKEND",
                       _VALID_BACKENDS, "native")


def resolve_proxy(configured=None):
    """native 后端抓 JavBus 用的代理。

    ⚠️ JavBus 在国内**直连超时**（实测）—— 这里返回 None 表示
    「看系统环境变量」，而环境里默认没有 proxy 变量，所以实际使用时
    应当在 config.yaml 的 `javbus_proxy` 里显式给。
    """

    if configured:
        return str(configured).strip() or None

    from core.datasource_config import get_proxy

    return get_proxy("javbus", "LMM_JAVBUS_PROXY")


class JavBusClient:
    """JavBus 客户端。接口对齐 `JavDBCLIClient.detail()`。

    `backend` 见上文模块级说明；两个后端产出的原始 JSON 形状一致，
    所以 `detail()` 里的翻译逻辑只有一份。
    """

    def __init__(self, base="http://127.0.0.1:8922", timeout=90,
                 backend=None, proxy=None):

        self.base = base.rstrip("/")
        self.timeout = timeout

        self.backend = resolve_backend(backend)

        self._native = None

        if self.backend == "native":

            from core.javbus_native import JavBusNativeClient

            self._native = JavBusNativeClient(
                timeout=timeout, proxy=resolve_proxy(proxy))

    # ------------------------------------------------------------ 底层

    def _api(self, path, **params):
        """一次取值：交给当前后端，返回 **javbus-api 形状**的 JSON。

        任何异常都返回 None —— 兜底路径不该把主流程带崩。
        """

        if self.backend == "native":
            return self._native_api(path, **params)

        return self._service_api(path, **params)

    # -- service 后端（原行为）--

    def _service_api(self, path, **params):

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

    # -- native 后端（直接抓网页）--

    def _native_api(self, path, **params):

        from core.javbus_native import JavBusNativeError

        try:

            # /api/movies/{id}
            if path.startswith("/api/movies/"):

                num = urllib.parse.unquote(path[len("/api/movies/"):])

                return self._native.detail(num)

            # /api/magnets/{id}?gid=&uc=
            if path.startswith("/api/magnets/"):

                mid = urllib.parse.unquote(path[len("/api/magnets/"):])

                return self._native.magnets(mid, params.get("gid"),
                                            params.get("uc"))

            # /api/movies?page=N（存活探测）
            if path == "/api/movies":
                return [] if self._native.available() else None

        except (JavBusNativeError, OSError, ValueError):
            return None

        return None

    # ------------------------------------------------------------ 对外

    def available(self):
        """后端在不在。不在就别走兜底，省得每次白等超时。"""

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
