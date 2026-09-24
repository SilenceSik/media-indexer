# -*- coding: utf-8 -*-
"""JavBus 纯 Python 客户端 —— 替掉对 `javbus-api` 自托管服务的依赖。

## 为什么要自己实现

原来这条路要跑一个 Node 服务（`X:\\Apps\\javbus-api`，约 90MB 常驻）。
公开仓的使用者不可能为了「JavDB 查不到时的兜底」去部署一个 Node 服务。
本模块把这条链路拿回来，**只依赖标准库**（`html.parser` + `urllib`）。

## 关键设计：产出与 javbus-api **完全相同的 JSON 形状**

不在这里做「JavBus 字段 -> JavDB 字段」的翻译 —— 那是
`adapters/javbus_adapter.py` 的职责。本模块只负责
「抓页面 -> 解出 javbus-api 会返回的那几个字段」，于是：

    adapters/javbus_adapter.py 的翻译层     <- 两个后端共用
      ^                    ^
      |                    |
    JavBusClient        JavBusNativeClient
    (HTTP 调本地服务)    (本模块，直接抓)

换后端不用改翻译逻辑，也就不会出现「两个后端字段口径不一致」这类 bug。

## 来源与许可

页面结构与端点契约来自 **javbus-api**（ovnrain，**MIT**，
package.json 声明；该仓库未放 LICENSE 文件）：

    https://github.com/ovnrain/javbus-api
    api/javbus-parser.ts   (parseMoviesPage / getMovieDetail /
                            convertMagnetsHTML)
    api/client.ts          (请求头：Accept-Language / User-Agent)

本项目按 MIT 条款复用其接口契约，**未复制其代码**（TypeScript → Python
重实现），并在此声明来源。

## 实测关键点（别再踩）

1. **年龄门靠 `Accept-Language` 请求头，不是靠 IP** —— 不带这个头时
   任何出口都 302 到年龄验证页，`age=verified` cookie 也救不了。
   只加 `Accept-Language: zh-CN,zh;q=0.9` 一个头就通。
2. **磁力是两步**：详情页里的 `gid` / `uc` 是查磁力的必需参数，
   服务端强校验，不带就 400。
3. **磁力在 `title`、番号在 `id`** —— 这是 JavBus 与 JavDB 最反直觉的差异，
   翻译层负责纠正。
"""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# ═══════════════════════ 常量

HOST = "https://www.javbus.com"

# Chrome on macOS —— 与上游 javbus-api 一致（部分站点会对 UA 做粗筛）
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/91.0.4472.114 Safari/537.36")

# ⚠️ 年龄门判据。少这一个头就 302 到 /doc/driver-verify。
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"

DEFAULT_TIMEOUT = 30

_TAG_RE = re.compile(r"<[^>]+>")

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.I | re.S)

_WS_RE = re.compile(r"\s+")


def _text(fragment):
    """去掉标签、还原实体、压掉空白。"""

    if not fragment:
        return ""

    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", fragment))).strip()


def _attr(attrs, name):
    """从属性串里取某个属性的值（大小写不敏感、容忍单双引号）。"""

    if not attrs:
        return None

    m = re.search(
        r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % re.escape(name),
        attrs, re.I,
    )

    if not m:
        return None

    return html.unescape(m.group(1) or m.group(2) or m.group(3) or "")


def _abs_url(url, base=HOST):
    """把相对地址补全。"""

    if not url:
        return None

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return base.rstrip("/") + url

    return url


# ═══════════════════════ 详情页解析

_GID_RE = re.compile(r"var\s+gid\s*=\s*(\d+)\s*;")

_UC_RE = re.compile(r"var\s+uc\s*=\s*(\d+)\s*;")


def _info_region(html_text):
    """框出详情 info 区。

    ⚠️ 页面上的类是 `class="col-md-3 info"`（前面带 grid 类），
    所以不能精确匹配 `class="info"` —— 早前那样写在真实页面上框不到，
    退化成「整页搜」并导致类别/演员丢失。
    """

    m = re.search(r'<div\b[^>]*class="[^"]*\binfo\b[^"]*"[^>]*>', html_text)

    if not m:
        return html_text

    region = html_text[m.end():]

    # info 区在截图区/同类推荐处收尾
    for end_marker in ('id="sample-waterfall"', 'id="related-waterfall"',
                       'class="container"', '<div class="row movie"'):

        cut = region.find(end_marker)

        if cut > 0:
            region = region[:cut]
            break

    return region


def _parse_genres(region):
    """类别：`<span class="genre">` 里嵌的 `<a href="/genre/X">名字</a>`。

    ⚠️ 判别器是 **href 前缀 `/genre/`**，不是所在容器 ——
    页面上「女優」那段的链接也会被渲染进 `span.genre`，
    只按容器收会把演员名当类别（实测 SSIS-001 混进了两个演员名）。
    """

    out = []
    seen = set()

    for m in re.finditer(r'<span\b[^>]*class="[^"]*\bgenre\b[^"]*"[^>]*>'
                         r'(.*?)</span>', region, re.S | re.I):

        for a in re.finditer(r'<a\b([^>]*)>(.*?)</a>', m.group(1),
                             re.S | re.I):

            href = _attr(a.group(1), "href") or ""

            # 结构判别：类别链接一定指向 /genre/
            if "/genre/" not in href:
                continue

            name = _text(a.group(2))

            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "id": href})

    return out


def _parse_stars(region):
    """演员：`id="star_XXX"` 的盒子 -> `div.star-name` 里的链接。

    ⚠️ 不用 `class="star-box"` 匹配 —— 多个盒子挨着时，用
    `</div></div>` 收尾的正则会把第二个盒子吃掉（实测只抓到 1 个演员，
    而页面上有 2 个）。用 `id="star_..."` 定位精确得多。
    """

    out = []
    seen = set()

    for m in re.finditer(r'<div\b[^>]*\bid="star_([^"]+)"[^>]*>(.*?)</div>',
                         region, re.S | re.I):

        block = m.group(2)

        # star-name 里那个链接最准
        a = re.search(r'<div\b[^>]*class="[^"]*star-name[^"]*"[^>]*>'
                      r'\s*<a\b([^>]*)>(.*?)</a>', block, re.S | re.I)

        if not a:
            a = re.search(r'<a\b([^>]*)>(.*?)</a>', block, re.S | re.I)

        if not a:
            continue

        name = _text(a.group(2))

        if not name:
            name = _attr(a.group(1), "title") or ""

        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "id": _attr(a.group(1), "href")})

    return out



def _find_value(paragraphs, *labels):
    """按标签取该段的纯文本值。"""

    for label, block in paragraphs:

        for want in labels:

            if label.startswith(want) or want in label:

                value = _text(block)

                if value:
                    return value

    return None


def _find_link(paragraphs, labels):
    """按标签取该段里第一个链接的 (文本, href)。"""

    for label, block in paragraphs:

        for want in labels:

            if label.startswith(want) or want in label:

                m = re.search(r"<a\b([^>]*)>(.*?)</a>", block, re.S | re.I)

                if not m:
                    continue

                name = _text(m.group(2))

                if name:
                    return {"name": name,
                            "id": _attr(m.group(1), "href")}

    return None


def _genre_items(block):
    """从一段 HTML 里取出所有 `span.genre` 的 {name, id}。"""

    out = []

    for m in re.finditer(r"<span\b([^>]*class=\"[^\"]*\bgenre\b[^\"]*\"[^>]*)>"
                         r"(.*?)</span>", block, re.S | re.I):

        attrs, inner = m.group(1), m.group(2)

        a = re.search(r"<a\b([^>]*)>(.*?)</a>", inner, re.S | re.I)

        if not a:
            continue

        name = _text(a.group(2))

        if name:
            out.append({
                "name": name,
                "id": _attr(a.group(1), "href"),
                # 演员的 span 带 onmouseover（用于悬停头像），类别不带
                "_hover": "onmouseover" in attrs.lower(),
            })

    return out


def parse_detail(html_text, base=HOST):
    """详情页 HTML -> **javbus-api 形状**的 dict。

    形状与 `GET /api/movies/{id}` 的返回一致（见模块文档的「关键设计」）。
    """

    if not html_text:
        return None

    region = _info_region(html_text)

    # ── 标题：容器内第一个 <h3> ──
    title = None

    m = re.search(r"<h3\b[^>]*>(.*?)</h3>", html_text, re.S | re.I)

    if m:
        title = _text(m.group(1))

    # ── 封面：`a.bigImage` 里的 img ──
    img = None

    m = re.search(r'<a\b[^>]*class="[^"]*bigImage[^"]*"[^>]*>(.*?)</a>',
                  html_text, re.S | re.I)

    if m:
        inner = re.search(r"<img\b([^>]*)>", m.group(1), re.I)
        if inner:
            img = _abs_url(_attr(inner.group(1), "src"), base)

    # ── 按 <p><span class="header">标签:</span> 值</p> 取值 ──
    fields = {}

    for m in re.finditer(r"<p\b([^>]*)>(.*?)</p>", region, re.S | re.I):

        block = m.group(2)

        label_m = re.search(r'<span\b[^>]*class="[^"]*header[^"]*"[^>]*>'
                            r"(.*?)</span>", block, re.S | re.I)

        if label_m:
            label = _text(label_m.group(1)).rstrip(":：").strip()
            rest = block[label_m.end():]
        else:
            # <p class="header">類別:...</p> 这种标签直接在 p 上的
            raw = _text(block)
            if ":" in raw or "：" in raw:
                parts = re.split(r"[:：]", raw, 1)
                label, rest = parts[0].strip(), parts[1]
            else:
                continue

        if label and label not in fields:
            fields[label] = rest

    def _value(*labels):
        for want in labels:
            for label, block in fields.items():
                if label.startswith(want) or want in label:
                    v = _text(block)
                    if v:
                        return v
        return None

    def _link(*labels):
        for want in labels:
            for label, block in fields.items():
                if label.startswith(want) or want in label:
                    a = re.search(r"<a\b([^>]*)>(.*?)</a>", block,
                                  re.S | re.I)
                    if a and _text(a.group(2)):
                        return {"name": _text(a.group(2)),
                                "id": _attr(a.group(1), "href")}
        return None

    number = _value("識別碼", "识别码", "番號", "番号")

    length_text = _value("長度", "长度")

    video_length = None

    if length_text:
        m = re.search(r"(\d+)", length_text)
        if m:
            video_length = int(m.group(1))

    date = _value("發行日期", "发行日期")

    director = _link("導演", "导演")
    producer = _link("製作商", "制作商", "メーカー")
    publisher = _link("發行商", "发行商")
    series = _link("系列")

    # 类别与演员都按**结构**解析（不靠标签猜、也不靠标签名剔除）
    genres = _parse_genres(region)
    stars = _parse_stars(region)

    # gid / uc：查磁力必需
    gid = None
    uc = None

    m = _GID_RE.search(html_text)
    if m:
        gid = m.group(1)

    m = _UC_RE.search(html_text)
    if m:
        uc = m.group(1)

    return {
        "id": number,
        "title": title,
        "img": img,
        "date": date,
        "videoLength": video_length,
        "director": director,
        "producer": producer,
        "publisher": publisher,
        "series": series,
        "genres": genres,
        "stars": stars,
        "gid": gid,
        "uc": uc,
    }


# ═══════════════════════ 磁力解析

# JavBus 的体积文本 -> 字节
_SIZE_UNITS = (("TB", 1024 ** 4), ("GB", 1024 ** 3),
               ("MB", 1024 ** 2), ("KB", 1024))


def size_text_to_bytes(text):
    """`"7.74GB"` -> 字节数。解不出返回 None（**不编 0**）。"""

    if not text:
        return None

    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?B)\s*$", text.strip().upper())

    if not m:
        return None

    try:
        amount = float(m.group(1))
    except ValueError:
        return None

    for unit, mult in _SIZE_UNITS:
        if m.group(2) == unit:
            return amount * mult

    return None


_MAGNET_HASH_RE = re.compile(r"magnet:\?xt=urn:btih:(\w+)", re.I)


def parse_magnets(html_text):
    """磁力页 HTML -> **javbus-api 形状**的 list。

    形状与 `GET /api/magnets/{id}?gid=&uc=` 一致：
    `{id(hash), link, title(名字), size(文本), numberSize(字节),
      isHD, hasSubtitle, shareDate}`。
    """

    if not html_text:
        return []

    out = []

    for tr_m in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html_text, re.S | re.I):

        row = tr_m.group(1)

        # 第一个 td 里的第一个 <a> 才是磁力
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)

        if not tds:
            continue

        first = tds[0]

        a = re.search(r"<a\b([^>]*)>(.*?)</a>", first, re.S | re.I)

        if not a:
            continue

        link = _attr(a.group(1), "href") or ""

        if "magnet:" not in link.lower():
            continue

        m = _MAGNET_HASH_RE.search(link)

        if not m:
            continue

        magnet_hash = m.group(1)

        inner = a.group(2)

        is_hd = "高清" in _text(inner)
        has_subtitle = "字幕" in _text(inner)

        title = _text(inner)

        size_text = _text(tds[1]) if len(tds) > 1 else None
        share_date = _text(tds[2]) if len(tds) > 2 else None

        number_size = size_text_to_bytes(size_text)

        out.append({
            "id": magnet_hash,
            "link": html.unescape(link),
            "title": title,
            "size": size_text or None,
            "numberSize": number_size,
            "isHD": is_hd,
            "hasSubtitle": has_subtitle,
            "shareDate": share_date or None,
        })

    # 上游按体积降序（`convertMagnetsHTML` 的 toSorted）
    out.sort(key=lambda x: (x.get("numberSize") or 0), reverse=True)

    return out


# ═══════════════════════ 客户端

class JavBusNativeError(Exception):
    """抓取失败。调用方据此决定降级。"""


class JavBusNativeClient:
    """直接抓 JavBus 网页的客户端（**产出 javbus-api 的 JSON 形状**）。"""

    def __init__(self, base=HOST, timeout=DEFAULT_TIMEOUT, proxy=None,
                 session=None):

        self.base = (base or HOST).rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self._session = session

        # 抓过一次的 gid/uc 缓存 —— 磁力要两步，别为同一个番号抓两遍详情
        self._gid_cache = {}

    # ── 底层 ──

    def _session_obj(self):

        if self._session is not None:
            return self._session

        import requests

        self._session = requests.Session()

        return self._session

    def _request(self, url, referer=None, cookie=None, timeout=None):
        """一次 GET，返回解码后的 HTML。失败抛 `JavBusNativeError`。

        ## 为什么用 requests 而不是 urllib

        实测：国内直连 JavBus **超时**，必须走代理。而 urllib 的
        `ProxyHandler` **不支持 SOCKS**（需要先装 PySocks 再把 handler
        换成 socks 版本）。requests 直接吃 `http://` 与 `socks5h://` 两种
        写法，少一层自己拼装，也少一个坑。
        """

        import requests

        headers = {
            "User-Agent": USER_AGENT,
            # ⚠️ 年龄门判据，删掉就 302 到 /doc/driver-verify
            "Accept-Language": ACCEPT_LANGUAGE,
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,*/*;q=0.8"),
        }

        if referer:
            headers["Referer"] = referer

        if cookie:
            headers["Cookie"] = cookie

        proxies = None

        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        try:

            r = self._session_obj().get(
                url,
                headers=headers,
                timeout=timeout or self.timeout,
                proxies=proxies,
                allow_redirects=True,
            )

        except Exception as e:

            raise JavBusNativeError(
                "{}: {}".format(type(e).__name__, e))

        if r.status_code == 404:
            raise JavBusNativeError("not found（404）")

        if r.status_code >= 400:
            raise JavBusNativeError("HTTP {}".format(r.status_code))

        # 年龄验证页会以 200 返回 —— 认出来，别把它当详情页解析
        if "driver-verify" in r.url or "age-verified" in r.url:
            raise JavBusNativeError(
                "被年龄门拦下（检查 Accept-Language 头）")

        return r.text

    # ── 判断服务是否可用 ──

    def available(self):
        """能不能连上。连不上就别走兜底，省得每次白等超时。"""

        try:
            self._request(self.base + "/", timeout=min(self.timeout, 10))
            return True
        except JavBusNativeError:
            return False

    # ── 对外：详情 ──

    def detail(self, number):
        """番号 -> javbus-api 形状的详情 dict；查不到返回 None。"""

        if not number:
            return None

        num = str(number).strip()

        url = "{}/{}".format(self.base, urllib.parse.quote(num))

        try:
            page = self._request(url)
        except JavBusNativeError:
            return None

        parsed = parse_detail(page, base=self.base)

        if not parsed:
            return None

        # 页面没有识别碼时，用请求的番号兜底（JavBus 的 id 就是番号）
        if not parsed.get("id"):
            parsed["id"] = num

        # 校验：拿到的页面确实是这个番号（避免重定向到别人的页）
        got = str(parsed.get("id") or "").strip().upper()
        want = num.upper()

        if got and got != want:
            # 允许大小写与分隔符差异
            norm = lambda s: re.sub(r"[^A-Z0-9]", "", s)  # noqa: E731
            if norm(got) != norm(want):
                return None

        if parsed.get("gid"):
            self._gid_cache[parsed["id"]] = (parsed.get("gid"),
                                             parsed.get("uc"))

        return parsed

    # ── 对外：磁力 ──

    def magnets(self, movie_id, gid=None, uc=None):
        """磁力列表（javbus-api 形状）。

        `gid`/`uc` 是服务端强校验的必需参数；不传就先用 id 抓详情补上。
        """

        if not movie_id:
            return []

        if gid is None or uc is None:

            cached = self._gid_cache.get(movie_id)

            if cached:
                gid, uc = cached

            else:

                d = self.detail(movie_id)

                if not d:
                    return []

                gid, uc = d.get("gid"), d.get("uc")

        if gid is None or uc is None:
            return []

        url = "{}?{}".format(
            self.base + "/ajax/uncledatoolsbyajax.php",
            urllib.parse.urlencode({"lang": "zh", "gid": gid, "uc": uc}),
        )

        try:
            page = self._request(
                url,
                referer="{}/{}".format(self.base,
                                       urllib.parse.quote(str(movie_id))),
                cookie="existmag=mag",
            )
        except JavBusNativeError:
            return []

        return parse_magnets(page)

    # ── 对外：合成（对齐 adapter 的用法）──

    def detail_with_magnets(self, number):
        """详情 + 磁力。对应 `JavBusClient.detail()` 的完整产出。"""

        d = self.detail(number)

        if not d:
            return None

        d = dict(d)

        d["magnets"] = self.magnets(d.get("id"), d.get("gid"), d.get("uc"))

        return d


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)
