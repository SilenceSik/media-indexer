# -*- coding: utf-8 -*-
"""JavDB App API 纯 Python 客户端 —— 替掉对 `javdb` CLI 二进制的依赖。

## 为什么要自己实现

公开仓的 `adapters/javdb_adapter.py` 靠 subprocess 调 `javdb` 命令（17.8 MB
的 Go 二进制）。别人克隆下来**没有这个 CLI**，元数据抓取会静默返回 None
（`except` 吞掉异常）—— 公开仓因此**跑不起来**。本模块把这条链路拿回来，
让仓库自带可运行的数据源。

## 来源与许可（重要）

签名算法与端点/参数契约来自 **javdb-cli**（FlanChanXwO，**MIT**）：

    https://github.com/FlanChanXwO/javdb-cli
    internal/javdb/protocol/signature/sign.go
    internal/javdb/appapi/{client,endpoint,model}/

其中 `PREFIX`/`SUFFIX` 两个常量是该上游项目从 JavDB 官方 APK 1.9.28
逆向所得。本项目按 MIT 条款复用其接口契约，**未复制其代码**（Go → Python
重实现），并在此声明来源。

⚠️ 该签名是**上游逆向**的产物，JavDB 改动 App 协议即会失效 ——
本模块因此只作为**数据源之一**，失败时上层可回退。

## 请求配方（已实测打通）

    GET {host}/api/v4/movies/{内id}          -> data.movie
    GET {api}/v2/search?q={番号}&page=N      -> data.movies
    GET {api}/v1/movies/{内id}/magnets       -> data.magnets
    GET {api}/v1/movies/{内id}/reviews       -> data.reviews

    头: jdsignature / accept-language / user-agent
    签名: {ts}.{SUFFIX}.{md5(ts + PREFIX)}
    参数: 一组固定的 app/平台/设备字段（见 _public_params）

响应外层是信封 `{success, action, message, data}`：`success` 为假即失败，
真值取 `data`。**不拆信封会拿不到任何内容**（踩过：一度以为签名无效）。
"""

import hashlib
import json
import os
import re
import time
import uuid

# ═══════════════════════ 常量（来源见模块文档）

PREFIX = ("71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e"
          "7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa")

SUFFIX = "lpw6vgqzsp"

HOST_MIRROR = "https://jdforrepam.com"

HOST_MAIN = "https://javdb.com"

USER_AGENT = "Dart/3.4 (dart:io)"

APP_VERSION = "1.9.28"

APP_VERSION_NUMBER = "10928"

# 信封里代表「需要登录」的 action（沿用上游判定）
_AUTH_ACTIONS = frozenset({
    "unauthorized",
    "login_required",
    "token_invalid",
    "auth_required",
})


class JavDBNativeError(Exception):
    """原生客户端失败。调用方据此决定「降级」还是「报错」。"""


# ═══════════════════════ 签名

def sign(timestamp):
    """jdsignature（纯函数，便于单测对照上游 golden 值）。"""

    digest = hashlib.md5(
        "{}{}".format(timestamp, PREFIX).encode("utf-8")).hexdigest()

    return "{}.{}.{}".format(timestamp, SUFFIX, digest)


# ═══════════════════════ 番号归一

# FC2 在 JavDB 内部的编号是 FC2-<数字>，文件名里的 FC2-PPV-<数字> 查不到。
# （来源：skill javdb-cli 的实测结论 —— 直接查 FC2-PPV-* 会整批误判为「查不到」）
_FC2_PPV = re.compile(r"^FC2[-_]?PPV[-_]?(\d+)$", re.IGNORECASE)


def normalize_number(number):
    """把常见写法归一成 JavDB 认的写法。"""

    if not number:
        return number

    s = str(number).strip()

    m = _FC2_PPV.match(s)

    if m:
        return "FC2-{}".format(m.group(1))

    return s


def number_variants(number):
    """同一番号的候选写法（去/补前导零等）。

    实测：文件名常带多余零填充（`MIAA-00049`），直接查会 notfound，
    去零后能救回一部分。
    """

    n = normalize_number(number)

    out = [n]

    m = re.match(r"^([A-Za-z]+)-(\d+)$", n)

    if m:

        prefix, digits = m.group(1), m.group(2)

        stripped = digits.lstrip("0") or "0"

        for cand in ("{}-{}".format(prefix, stripped),
                     "{}-{:03d}".format(prefix, int(digits)),
                     "{}-{:05d}".format(prefix, int(digits))):

            if cand not in out:
                out.append(cand)

    return out


# ═══════════════════════ 客户端

class JavDBNativeClient:
    """JavDB App API 客户端（无需登录即可查元数据与磁力）。"""

    def __init__(self, host=None, timeout=20, retries=2,
                 device_uuid=None, state_dir=None, proxy=None,
                 session=None):

        # 默认用镜像 —— 实测主站对 App API 不一定可达
        self.host = (host or HOST_MIRROR).rstrip("/")

        self.timeout = timeout
        self.retries = max(0, int(retries))

        self.proxy = proxy

        self.device_uuid = device_uuid or self._load_or_create_uuid(state_dir)

        self._session = session

    # ── device_uuid：持久化，避免每次跑都伪装成新设备 ──

    @staticmethod
    def _load_or_create_uuid(state_dir):

        path = None

        if state_dir:

            try:
                os.makedirs(state_dir, exist_ok=True)
                path = os.path.join(state_dir, "device_uuid")
            except OSError:
                path = None

        if path and os.path.exists(path):

            try:
                with open(path, encoding="utf-8") as f:
                    value = f.read().strip()
                if value:
                    return value
            except OSError:
                pass

        value = str(uuid.uuid4())

        if path:

            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(value + "\n")
            except OSError:
                pass

        return value

    # ── 请求 ──

    def _session_obj(self):

        if self._session is not None:
            return self._session

        import requests

        self._session = requests.Session()

        return self._session

    def _public_params(self):

        return {
            "app_channel": "official",
            "app_version": APP_VERSION,
            "app_version_number": APP_VERSION_NUMBER,
            "platform": "android",
            "system_version": "13",
            "device_model": "Pixel 6",
            "device_name": "Pixel",
            "device_uuid": self.device_uuid,
        }

    def _get(self, path, extra=None, timeout=None):
        """发一次签名请求，返回拆信封后的 payload（dict 或 list）。"""

        params = self._public_params()

        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})

        proxies = None

        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        last = None

        for attempt in range(self.retries + 1):

            try:

                ts = int(time.time())

                headers = {
                    "jdsignature": sign(ts),
                    "accept-language": "en",
                    "connection": "keep-alive",
                    "user-agent": USER_AGENT,
                }

                r = self._session_obj().get(
                    self.host + path,
                    params=params,
                    headers=headers,
                    timeout=timeout or self.timeout,
                    proxies=proxies,
                )

                if r.status_code >= 400:
                    raise JavDBNativeError(
                        "HTTP {}: {}".format(r.status_code, r.text[:200]))

                payload = r.json()

            except JavDBNativeError as e:

                last = e

            except Exception as e:

                last = JavDBNativeError(
                    "{}: {}".format(type(e).__name__, e))

            else:

                # ── 拆信封 ──
                if not isinstance(payload, dict):
                    return payload

                success = payload.get("success")

                ok = success in (1, True, "1", "true", "True")

                if not ok:

                    action = payload.get("action")
                    message = payload.get("message") or ""

                    if action in _AUTH_ACTIONS:
                        raise JavDBNativeError(
                            "需要登录（action={}）".format(action))

                    raise JavDBNativeError(
                        "接口返回失败：{} {}".format(action, message)[:200])

                data = payload.get("data")

                if data is None:
                    return {}

                return data

            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))

        raise last or JavDBNativeError("请求失败")

    # ── search ──

    def search_raw(self, keyword, page=1, limit=None):
        """搜索，返回原始 movies 列表（不判歧义）。"""

        data = self._get("/api/v2/search", {
            "q": keyword,
            "page": page,
            "limit": limit,
        })

        if isinstance(data, dict):
            movies = data.get("movies") or []
        else:
            movies = []

        return [m for m in movies if isinstance(m, dict)]

    def search(self, number, limit=None):
        """按番号搜索，返回候选列表（每个含 id/number/title 等）。"""

        for variant in number_variants(number):

            try:
                movies = self.search_raw(variant, limit=limit)
            except JavDBNativeError:
                movies = []

            if movies:
                return movies

        return []

    def resolve(self, number):
        """番号 -> 恰好一个 movie 的内部 id。

        多个精确匹配时**返回全部**，由调用方决定 —— 不静默取首个
        （与 CLI 的 `detail` 歧义即失败语义一致，但把选择权留给上层）。
        """

        movies = self.search(number)

        if not movies:
            return []

        # 优先精确番号匹配
        target = normalize_number(number).upper()

        exact = [m for m in movies
                 if str(m.get("number", "")).strip().upper() == target]

        return exact or movies

    # ── detail ──

    def detail(self, movie_id):
        """按内部 id 取详情。返回 movie dict（不存在返回 None）。"""

        try:
            data = self._get("/api/v4/movies/" + movie_id)
        except JavDBNativeError:
            return None

        if isinstance(data, dict):
            movie = data.get("movie")
            if isinstance(movie, dict):
                return movie

        return None

    def detail_by_number(self, number):
        """番号 -> 详情。歧义或查不到返回 None，候选另取 `resolve()`。"""

        cands = self.resolve(number)

        if len(cands) != 1:
            return None

        return self.detail(cands[0]["id"])

    # ── magnets ──

    def magnets(self, movie_id):
        """磁力列表（无需登录）。"""

        try:
            data = self._get("/api/v1/movies/{}/magnets".format(movie_id))
        except JavDBNativeError:
            return []

        if isinstance(data, dict):
            items = data.get("magnets") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        return [m for m in items if isinstance(m, dict)]

    # ── comments ──

    def comments(self, movie_id, page=1, limit=20):
        """评论一页（只有一页，不自动翻页 —— 与 CLI 语义一致）。"""

        try:
            data = self._get(
                "/api/v1/movies/{}/reviews".format(movie_id),
                {"page": page, "limit": limit},
            )
        except JavDBNativeError:
            return []

        if isinstance(data, dict):
            items = data.get("reviews") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        return [x for x in items if isinstance(x, dict)]

    # ── assets ──

    def preview_video_url(self, movie):
        """宣传视频地址。只在 detail 的 movie 对象里取 —— 不额外发请求。"""

        if isinstance(movie, dict):
            return movie.get("preview_video_url") or None

        return None

    def preview_images(self, movie):
        """截图列表（大图优先）。"""

        if not isinstance(movie, dict):
            return []

        out = []

        for item in movie.get("preview_images") or []:

            if isinstance(item, dict):
                url = item.get("large_url") or item.get("thumb_url")
            else:
                url = item

            if url:
                out.append(url)

        return out

    def cover_url(self, movie):

        if isinstance(movie, dict):
            return movie.get("cover_url") or movie.get("thumb_url")

        return None

    @staticmethod
    def duration_minutes(movie):
        """元数据时长（**分钟**）—— 与 CLI 输出的 duration 同口径。"""

        if not isinstance(movie, dict):
            return None

        v = movie.get("duration")

        try:
            v = int(v)
        except (TypeError, ValueError):
            return None

        return v if v > 0 else None


def dumps(obj):
    """调试用。"""

    return json.dumps(obj, ensure_ascii=False, indent=2)
