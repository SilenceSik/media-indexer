# -*- coding: utf-8 -*-
"""卡片分页（无限滚动）的回归锁。

## 为什么需要

主人 09-24 报「还是卡，一次性渲染几百张卡片的卡顿」。296 张实测：
HTML 580KB、DOM 6343 节点、纯布局 1.3s（CPU 4x）。库里几千部时会崩。

改成首屏一页 + 滚动按需取。这里锁住三件事：

### 一、分页必须按**番号**，不能按文件行（**踩过的坑**）

`LIST_SQL` 是 `titles LEFT JOIN media_files`，一个番号多文件时有多行；
而 `rows_to_videos()` 按番号聚合成**一张卡**。若直接对行做 LIMIT/OFFSET：

  * 每页实际卡片数少于 limit
  * 页码漂移 —— 下一批会**重复**上一批的番号，还会连环漏

本库实测 18 个番号是多文件的（326 行 / 296 番号），必踩。
所以判据是：所有页拼起来，番号**不重不漏**，且数量等于 total。

### 二、首屏只渲染一页

不能又回到「渲染全部」—— 那正是要解决的问题。

### 三、分页会跨筛选条件用

所以 URL 里要带上 has_magnet / missing_meta / favorite / q 与权重，
否则第二页开始会串到别的筛选结果、或置信分跟首屏不一致。
"""

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()

IDX = io.open(os.path.join(ROOT, "web", "templates", "index.html"),
              encoding="utf-8").read()


# ═══════════════════════ 一、按番号分页

def test_fetch_cards_paginates_by_title_not_rows():
    """**回归**：分页先取番号、再取行 —— 不能直接对 JOIN 结果做 LIMIT。"""

    m = re.search(r"def fetch_cards\(.*?\n(?=\n\ndef |\n\n@app|@app)", APP, re.S)

    assert m, "找不到 fetch_cards"

    body = m.group(0)

    assert "FROM titles" in body, \
        "fetch_cards 应当先对 titles 分页取番号"

    assert "titles.id IN" in body, \
        "fetch_cards 应当再用这些番号取完整行（否则多文件番号会漏）"

    # 反面：不能对 LIST_SQL 直接加 LIMIT/OFFSET
    assert not re.search(r"LIST_SQL\s*\n?\s*\+ where\s*\n?\s*\+ \"\"\"\s*\n"
                         r"\s*ORDER BY[^\"]*LIMIT \? OFFSET \?", body), \
        "fetch_cards 又对 JOIN 结果直接 LIMIT/OFFSET 了 —— 页码会漂"


def test_count_cards_matches_title_count():
    """count 要数**番号**（与分页同一套条件），否则进度显示会错。"""

    m = re.search(r"def count_cards\(.*?\n(?=\n\ndef )", APP, re.S)

    assert m, "找不到 count_cards"

    body = m.group(0)

    assert "COUNT(*)" in body, "count_cards 要返回数量"

    assert "_cards_where" in body, \
        "count_cards 必须复用 _cards_where —— 口径不能和分页各写一套"


def test_where_clause_is_shared():
    """筛选条件只能有一处定义 —— 首页、分页、计数三处必须一致。"""

    assert APP.count("def _cards_where") == 1

    # fetch_cards 与 count_cards 都要用它
    for fn in ("fetch_cards", "count_cards"):
        m = re.search(r"def %s\(.*?\n(?=\n\ndef |\n\n@app|@app)" % fn,
                      APP, re.S)
        assert m and "_cards_where" in m.group(0), \
            "{} 没有复用 _cards_where".format(fn)


# ═══════════════════════ 二、首屏只一页

def test_index_uses_page_size():
    """首页只能渲染一页，不能又全量。"""

    m = re.search(r"def index\(.*?\n(?=\n\ndef |\n\n@app|@app)", APP, re.S)

    assert m, "找不到 index"

    body = m.group(0)

    assert "CARDS_PAGE_SIZE" in body, \
        "首页应当按 CARDS_PAGE_SIZE 取一页"

    assert "limit=CARDS_PAGE_SIZE" in body or "limit=CARDS_PAGE_SIZE," in body, \
        "首页的 limit 必须是 CARDS_PAGE_SIZE"

    # 反面：不能出现 LIMIT 500 那种全量取法
    assert "LIMIT 500" not in body, \
        "首页还残留 LIMIT 500（等于全量渲染）"


def test_page_size_is_sane():
    """每页张数要合理 —— 太小会频繁请求，太大又回到卡顿。"""

    m = re.search(r"CARDS_PAGE_SIZE\s*=\s*(\d+)", APP)

    assert m, "找不到 CARDS_PAGE_SIZE"

    n = int(m.group(1))

    assert 8 <= n <= 60, \
        "CARDS_PAGE_SIZE={} 不合理（8~60 之间）".format(n)


# ═══════════════════════ 三、前端承接

def test_grid_carries_pagination_context():
    """网格要把筛选条件与总数带给 JS，否则第二批会串。"""

    for attr in ("data-total", "data-page-size", "data-has-magnet",
                 "data-missing-meta", "data-favorite", "data-w-magnets"):
        assert attr in IDX, "index.html 少了 {}".format(attr)


def test_infinite_scroll_request_includes_filters():
    """请求下一批时要带上筛选条件与权重。"""

    i = IDX.find("function params(offset)")
    assert i > 0, "找不到 params()"

    body = IDX[i:i + 900]

    for k in ("has_magnet", "missing_meta", "favorite", "w_magnets"):
        assert k in body, "params() 没带 {}".format(k)


def test_cards_are_inserted_into_grid():
    """**回归**：新卡片必须插进 grid 内。

    踩过：哨兵是 grid 的**兄弟**（放网格外才不会变成网格项），
    若 `sentinel.insertAdjacentHTML('beforebegin', ...)` 把卡片插在哨兵前，
    新卡片会落到**网格外** —— 样式全丢、选择器也数不到。
    """

    assert "grid.insertAdjacentHTML" in IDX, \
        "新卡片应当插进 grid（beforeend）"

    assert "sentinel.insertAdjacentHTML" not in IDX, \
        "别插在哨兵前面 —— 那会落到网格外"


def test_sentinel_outside_grid():
    """哨兵要在 grid **外面**，否则它自己会占一个网格项。"""

    i = IDX.find('id="grid"')
    j = IDX.find('id="load-more"')
    k = IDX.find('{% endif %}', i)

    assert 0 < i < j, "找不到 grid / load-more"

    # grid 的收尾 </div> 应当在哨兵之前
    end = IDX.find("</div>", IDX.find("{% endfor %}", i))

    assert end < j, \
        "load-more 在 grid 内部了 —— 它会被当成一个网格项"


def test_search_page_provides_same_context():
    """**回归**：/search 曾经因为 weights 未定义而一直 500。

    两个页面共用 index.html，所以上下文变量必须给齐 ——
    漏一个就是 NameError。
    """

    m = re.search(r"def search\(.*?\n(?=\n\ndef |\n\n@app|@app)", APP, re.S)

    assert m, "找不到 search"

    body = m.group(0)

    for name in ("normalize_weights", "fetch_cards", "count_cards"):
        assert name in body, "/search 少了 {}".format(name)

    # 模板要用的键必须都在
    for key in ('"videos"', '"total"', '"page_size"', '"weights"',
                '"has_magnet"', '"missing_meta"', '"favorite"'):
        assert key in body, "/search 的上下文少了 {}".format(key)
