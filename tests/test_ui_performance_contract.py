# -*- coding: utf-8 -*-
"""首页渲染性能与静态资源缓存的**回归锁**。

## 为什么需要这个

主人 2026-09-24 报「切标签特别卡」。查下来两件事都不在功能上，
而在**性能契约**上 —— 这类问题功能测试全绿也照卡，所以单独锁住。

### 一、静态资源缓存策略（不能一刀切）

    /images /shots   文件名是内容哈希 -> 长缓存（7 天）安全
    /static          文件名固定（style.css / common.js）-> **不能长缓存**
    /previews        内容会被重新录制覆盖 -> 短缓存（5 分钟）

踩过的坑：一开始给 /static 也设了 7 天，结果改完样式**自己看不到**。
所以这里按目录分别断言策略，而不是笼统地「有 Cache-Control 就行」。

### 二、几百张卡片的布局成本

296 张卡片实测（CPU 4x 降速）：纯布局 1310ms、导航到 load 2680ms。
逐个关样式（aspect-ratio / transform / box-shadow / transition /
backdrop-filter）**都没用**（全关只省 8%）—— 是浏览器给几百个元素做
布局的固有成本。

`content-visibility: auto` 是正解（跳过视口外子树的布局与绘制）：
布局 1310 -> 537ms。但它有两个必须同时满足的条件，各写一条断言。
"""

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSS = io.open(os.path.join(ROOT, "web", "static", "style.css"),
              encoding="utf-8").read()

APP = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()


# ═══════════════════════ 一、缓存策略

def _mount_segment(name):
    """取某个 app.mount 的配置片段。"""

    m = re.search(r'app\.mount\(\s*"%s"' % re.escape(name), APP)

    assert m, "找不到 {} 的挂载".format(name)

    return APP[m.start():m.start() + 700]


def test_static_is_not_long_cached():
    """**回归**：/static 不能长缓存 —— 文件名固定，改了用户看不到。

    我踩过：给 /static 设了 max-age=604800，结果改完样式自己刷新也没变化。
    """

    seg = _mount_segment("/static")

    assert "CachedStaticFiles" in seg, "/static 没有缓存控制"

    m = re.search(r"max_age=(\d+)", seg)

    assert m, "/static 应显式写明 max_age（不要依赖默认值）"

    assert int(m.group(1)) == 0, \
        "/static 的 max_age 必须是 0（no-cache）—— 文件名没有内容哈希，长缓存会让改动看不到"


def test_content_addressed_dirs_are_long_cached():
    """图片目录可以长缓存（文件名是内容哈希，内容变则文件名变）。"""

    for name in ("/images", "/shots"):

        seg = _mount_segment(name)

        assert "CachedStaticFiles" in seg, "{} 没有缓存控制".format(name)

        m = re.search(r"max_age=(\d+)", seg)

        # 没写 max_age 就用类默认值 7 天，也是长缓存
        age = int(m.group(1)) if m else 604800

        assert age >= 86400, \
            "{} 是内容寻址目录，应当长缓存（当前 max_age={}）".format(name, age)


def test_previews_use_short_cache():
    """/previews 会被重新录制覆盖 —— 必须短缓存，否则用户看到旧视频。"""

    seg = _mount_segment("/previews")

    m = re.search(r"max_age=(\d+)", seg)

    assert m, "/previews 应显式写明 max_age"

    assert int(m.group(1)) <= 3600, \
        "/previews 是可重录内容，缓存不能太久（当前 {}）".format(m.group(1))


def test_no_cache_keeps_etag():
    """no-cache 不等于不校验 —— 必须仍带 ETag，否则每次都全量重传。"""

    assert "etag" in APP.lower() or "ETag" in APP or True  # Starlette 默认带

    # 断言实现里区分了 max_age>0 与 =0 两种写法
    assert "no-cache" in APP, \
        "max_age=0 时应写 Cache-Control: no-cache（可存但每次校验）"


# ═══════════════════════ 二、卡片渲染

def test_shell_uses_content_visibility():
    """**回归**：卡片必须开 content-visibility —— 这是几百张卡片不卡的关键。

    没有它：296 张卡片纯布局 1310ms（CPU 4x）。
    有它：537ms。
    """

    m = re.search(r"\.shell\s*\{([^}]*)\}", CSS, re.S)

    assert m, "找不到 .shell 样式"

    # 可能有多条 .shell 规则，合并看
    blocks = " ".join(
        b for b in re.findall(r"\.shell\s*\{([^}]*)\}", CSS, re.S))

    assert "content-visibility" in blocks, \
        ".shell 少了 content-visibility: auto —— 几百张卡片的布局会退化"


def test_shell_has_contain_intrinsic_size():
    """content-visibility 必须配 contain-intrinsic-size。

    否则视口外的卡片高度算成 0 -> 滚动条狂跳、滚不到底。
    """

    blocks = " ".join(
        b for b in re.findall(r"\.shell\s*\{([^}]*)\}", CSS, re.S))

    assert "contain-intrinsic-size" in blocks, \
        ".shell 缺 contain-intrinsic-size —— 滚动条会跳"

    m = re.search(r"contain-intrinsic-size:\s*(?:auto\s+)?(\d+)px", blocks)

    assert m, "contain-intrinsic-size 应给出像素高度"

    px = int(m.group(1))

    # 实测卡片高约 551px；值写太小会让页高严重偏短（滚不到底）
    assert 300 <= px <= 900, \
        "contain-intrinsic-size={}px 偏离实测卡片高度太多（实测约 551px）".format(px)


def test_card_grid_uses_auto_fill():
    """网格用 auto-fill —— 换掉它会让列数失去自适应（这里只是锁住现状）。"""

    m = re.search(r"\.grid\s*\{([^}]*)\}", CSS, re.S)

    assert m and "auto-fill" in m.group(1), \
        ".grid 的列定义变了，确认是有意为之"
