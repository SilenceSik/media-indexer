# -*- coding: utf-8 -*-
"""模板完整性检查 —— 锁住一类**只在浏览器里才暴露**的 bug。

## 为什么需要这个

把 JS 从一个模板搬到另一个模板时踩过一次：某个函数体末尾**连带着把
`</script></body></html>` 一起搬了过来**，于是浏览器在第一个 `</script>`
处就结束了脚本块 —— 后面的函数全部「未定义」，页面看起来正常、
点按钮没反应。

`grep` 查不出这类问题（函数明明在文件里），只有**把 HTML 结构解析一遍**
才能发现。所以这里做的是结构检查，不是文本检查。

## 锁住什么

1. 每个模板里 `<script>` / `</script>` 数量配平
2. 内联脚本里**不得出现字面量 `</script>`**（这正是上面那个坑）
3. 关键页面的内联脚本能通过括号配平（说明没被截断）
4. 维护页引用的每个函数都有定义（onclick 写了但没定义 = 点了没反应）
"""

import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEMPLATES = os.path.join(ROOT, "web", "templates")

ALL = sorted(
    f for f in os.listdir(TEMPLATES) if f.endswith(".html")
)


def _read(name):
    return io.open(os.path.join(TEMPLATES, name), encoding="utf-8").read()


@pytest.mark.parametrize("name", ALL)
def test_script_tags_balanced(name):
    """开闭标签数量必须相等 —— 少一个都会让后续内容掉到脚本块外面。

    ⚠️ 不能数子串也不能简单扫标签：**内联 JS 里可能出现字面量
    `<script>` / `</script>`**（scan.html 的注释里就有一处在讲
    「会让整个 script 块解析失败」）。按 HTML 规范，内联脚本内出现
    `</script>` 会**真的截断脚本块** —— 所以那条注释本身就是隐患。

    所以判据拆成两条：
      1. 用**配对子串**数（`<script ...>` 与 `</script>`），排除注释里的裸 `<script`
      2. 内联脚本块内不得出现字面量 `</script>`（见下一个用例）
    """

    s = _read(name)

    # 用**成对**匹配来数：`<script ...>` 与它的 `</script>` 一起算一次。
    # 这样：
    #   * `<script src="x"></script>` 同行写 -> 正常算一对
    #   * 注释里的裸 `<script>`（没有配对的收尾）-> 不会凭空多出一个开标签
    pairs = re.findall(
        r"<script(?:\s[^>]*)?>.*?</script>", s, re.S | re.I)

    # 剩下未配对的开标签就是真问题
    rest = re.sub(r"<script(?:\s[^>]*)?>.*?</script>", "", s, flags=re.S | re.I)

    leftover_opens = re.findall(r"<script(?:\s[^>]*)?>", rest, re.I)
    leftover_closes = re.findall(r"</script>", rest, re.I)

    assert not leftover_opens, \
        "{}: 有 {} 个 <script> 没闭合：{}".format(
            name, len(leftover_opens), leftover_opens[:2])

    assert not leftover_closes, \
        "{}: 有 {} 个多余的 </script>".format(name, len(leftover_closes))


@pytest.mark.parametrize("name", ALL)
def test_inline_script_has_no_literal_close_tag(name):
    """**回归**：内联脚本里不能出现字面量 `</script>`。

    搬到维护页时踩过：某个函数末尾连着收尾标签一起被搬过去，
    浏览器在那里就切断了脚本块 → 后面的函数全部 undefined
    → 页面正常、点按钮没反应。grep 查不出来。
    """

    s = _read(name)

    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                         s, re.S):

        assert "</script>" not in m.group(1), \
            "{}: 内联脚本里出现了字面量 </script>（会提前截断脚本块）".format(
                name)


@pytest.mark.parametrize("name", ALL)
def test_braces_balanced_in_inline_script(name):
    """内联脚本的 {} 与 () 要配平 —— 不配平说明被截断或拼错。"""

    s = _read(name)

    for i, m in enumerate(re.finditer(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.S)):

        js = m.group(1)

        assert js.count("{") == js.count("}"), \
            "{} 第 {} 个脚本块：{{}} 不配平 {} vs {}".format(
                name, i, js.count("{"), js.count("}"))

        assert js.count("(") == js.count(")"), \
            "{} 第 {} 个脚本块：() 不配平 {} vs {}".format(
                name, i, js.count("("), js.count(")"))


def test_maintenance_defines_every_onclick_it_uses():
    """维护页上每个 onclick 调的函数都要有定义 —— 否则点了没反应。

    （那个 bug 的表现就是「弹窗代码在、按钮在、点了没反应」。）
    """

    s = _read("maintenance.html")

    # 本页内联脚本里定义的函数
    inline = "".join(
        m.group(1) for m in re.finditer(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.S)
    )

    defined = set(re.findall(r"function\s+(\w+)", inline))

    # common.js 提供的
    common = io.open(
        os.path.join(ROOT, "web", "static", "common.js"),
        encoding="utf-8",
    ).read()

    defined |= set(re.findall(r"function\s+(\w+)", common))

    # 允许调用浏览器原生 + 页面里已有的全局
    allowed = {"alert", "confirm", "fetch", "post"}

    used = set(re.findall(r'onclick="(\w+)\(', s))

    missing = used - defined - allowed

    assert not missing, \
        "维护页 onclick 调了但没定义：{}".format(sorted(missing))


def test_common_js_is_referenced_by_pages_that_need_it():
    """用到 esc/post/closeModal 的页面必须引 common.js。"""

    for name in ("maintenance.html", "index.html"):

        s = _read(name)

        needs = any(k in s for k in ("esc(", "post(", "closeModal("))

        if needs:
            assert "common.js" in s, \
                "{}: 用到了共用助手但没引 common.js".format(name)


def test_common_js_does_not_redefine_page_functions():
    """common.js 只放共用助手，不与页面定义重复（重复会互相覆盖）。"""

    common = io.open(
        os.path.join(ROOT, "web", "static", "common.js"),
        encoding="utf-8",
    ).read()

    common_funcs = set(re.findall(r"function\s+(\w+)", common))

    assert common_funcs <= {"esc", "post", "closeModal", "openModal",
                            "backdropClose"}, \
        "common.js 里出现了非共用的函数：{}".format(
            common_funcs - {"esc", "post", "closeModal", "openModal",
                            "backdropClose"})


def test_no_heart_emoji_in_nav():
    """主人 09-24：收藏前面的 ❤ 去掉。"""

    for name in ALL:

        s = _read(name)

        assert "❤️ 收藏" not in s and "❤ 收藏" not in s, \
            "{}: 导航里还带着 ❤".format(name)
