# -*- coding: utf-8 -*-
"""这一轮「回看 bug」抓到的四个问题的回归锁。

每一个都是**实际发生过的**，不是假想风险：

| # | 问题 | 表现 |
|---|---|---|
| 1 | `/cleanup` 路由注册两次 | 我加的 302 跳转**从未生效**（FastAPI 用先注册的） |
| 2 | 维护页的「空目录」调接口不带 `roots` | 接口直接 400 —— 这个功能**从来没work过** |
| 3 | 维护页读 `j.removed`（实际字段 `j.deleted`） | 提示永远显示「已送回收站 **undefined** 个」 |
| 4 | `/detail/{不存在的番号}` 渲染 index.html 时上下文不齐 | **500 而不是 404** —— 看起来像服务器坏了 |

共同点：**都不报错、都能"看起来正常"**，只有真去点/真去访问才暴露。
所以这里用静态检查 + 契约断言锁住，而不是只靠手测。
"""

import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()

T = os.path.join(ROOT, "web", "templates")

MNT = io.open(os.path.join(T, "maintenance.html"), encoding="utf-8").read()

IDX = io.open(os.path.join(T, "index.html"), encoding="utf-8").read()


# ═══════════════════════ 1. 路由不能重复注册

def _routes():
    """所有 (method, path) 及其注册位置。"""

    out = []

    for m in re.finditer(
            r'@app\.(get|post)\(\s*\n?\s*["\']([^"\']+)["\']', APP):

        fn_start = APP.find("def ", m.end())

        fn = re.match(r"def (\w+)", APP[fn_start:fn_start + 80])

        out.append((m.group(1).upper(), m.group(2),
                    fn.group(1) if fn else "?"))

    return out


def test_no_duplicate_routes():
    """**回归**：同一个 (方法, 路径) 只能注册一次。

    踩过：`/cleanup` 注册了两次（旧页面在前、我加的跳转在后），
    FastAPI 只用先注册的 —— 于是跳转**从未生效**，而我手测只看了
    状态码 200 没看 Location，漏过去了。
    """

    import collections

    by_key = collections.defaultdict(list)

    for method, path, fn in _routes():
        by_key[(method, path)].append(fn)

    dup = {k: v for k, v in by_key.items() if len(v) > 1}

    assert not dup, "重复注册的路由：{}".format(
        {k: v for k, v in dup.items()})


def test_cleanup_redirects_to_maintenance():
    """/cleanup 应当只有跳转（功能已并入维护页）。"""

    hits = [r for r in _routes() if r[1] == "/cleanup"]

    assert len(hits) == 1, "/cleanup 应当只注册一次"

    assert "redirect" in hits[0][2].lower() or "legacy" in hits[0][2].lower(), \
        "/cleanup 应当是跳转，不是页面"


# ═══════════════════════ 2/3. 维护页的空目录

def test_empty_dirs_ui_lets_user_pick_roots():
    """**回归**：必须让用户自己选扫描目录。

    不传 roots 接口直接 400 —— 而且不默认用 config.scan_paths 是**有意**
    的设计（那些通常是整个盘符，遍历代价极大、还可能碰坏道）。
    原页面特意加了这个安全阀，我搬的时候漏了。
    """

    assert 'id="ed-roots"' in MNT, "维护页缺「要扫描的目录」输入框"

    assert "pickEmptyRoots" in MNT, "缺选择目录的处理函数"

    # 扫描前必须先检查 roots，而不是直接发请求
    i = MNT.find("function loadEmptyDirs()")
    assert i > 0, "找不到 loadEmptyDirs"

    body = MNT[i:i + 700]

    assert "ed-roots" in body, "loadEmptyDirs 没读用户选的目录"

    assert re.search(r"if\s*\(\s*!roots\s*\)", body), \
        "loadEmptyDirs 没有「未选目录」的前置检查"

    assert "roots=" in body, "loadEmptyDirs 没把 roots 传给接口"


def test_empty_dirs_delete_reads_correct_field():
    """**回归**：接口返回 `deleted`，不是 `removed`。

    读错字段 -> 提示永远显示 undefined（不报错，只是数字是错的）。
    """

    i = MNT.find("function deleteEmptyDirs()")
    assert i > 0, "找不到 deleteEmptyDirs"

    body = MNT[i:i + 900]

    assert "j.deleted" in body, \
        "deleteEmptyDirs 应当读 j.deleted（接口的真实字段名）"

    assert "j.removed" not in body, \
        "deleteEmptyDirs 又读了不存在的 j.removed"


def test_empty_dirs_api_requires_roots():
    """接口契约：不给 roots 必须 400（**有意**，不是漏写）。"""

    i = APP.find("def api_empty_dirs(")
    assert i > 0

    body = APP[i:i + 1600]

    assert 'if not roots' in body, "api_empty_dirs 缺 roots 必填检查"

    assert "status_code=400" in body, "缺 roots 时应当返回 400"


# ═══════════════════════ 4. 渲染 index.html 的上下文必须齐

# index.html 需要的上下文键 —— 少一个就是 UndefinedError -> 500
IDX_KEYS = (
    "videos", "total", "page_size", "stats", "q",
    "fav_only", "has_magnet", "missing_meta", "favorite",
    "weights", "weights_default", "batch_count",
    # 分页接口要用同样的权重 —— 否则第二批开始置信分会跟首屏不一致。
    # 这四个是「原始 URL 参数值」（可为空字符串），由模板写进 data-* 给 JS。
    "w_magnets", "w_match", "w_source", "w_comments",
)


def test_index_template_only_uses_known_keys():
    """模板里引用的上下文变量要在白名单内（防止加了变量没人给）。"""

    used = set(re.findall(r'\{\{\s*(\w+)', IDX))

    # 允许的额外项（循环变量、Jinja 全局）
    allowed = {"v", "loop", "range", "dict", "url_for"}

    # 纯数字是字面量比较（如 `== 1`），不是变量
    unknown = {u for u in used - set(IDX_KEYS) - allowed
               if not u.isdigit()}

    assert not unknown, \
        "index.html 引用了没在白名单里的变量：{}（若是有意新增，"\
        "请同步更新 tests 里的 IDX_KEYS）".format(sorted(unknown))


@pytest.mark.parametrize("fn_name", ["index", "search"])
def test_page_handlers_pass_all_keys(fn_name):
    """渲染 index.html 的路由必须把上下文给齐。"""

    m = re.search(r"def %s\(.*?\n(?=\n\ndef |\n\n@app|@app)" % fn_name,
                  APP, re.S)

    assert m, "找不到 {}".format(fn_name)

    body = m.group(0)

    missing = [k for k in IDX_KEYS if '"{}"'.format(k) not in body]

    assert not missing, "{}() 的上下文少了：{}".format(fn_name, missing)


def test_detail_404_branch_passes_all_keys():
    """**回归**：/detail/{不存在的番号} 曾返回 500 而不是 404。

    它渲染 index.html 只给了 videos/stats/q，缺 total/weights 等 ->
    UndefinedError。表现是「番号不存在」看起来像「服务器坏了」。
    """

    i = APP.find("def detail(")
    assert i > 0, "找不到 detail"

    # 只看 404 分支
    j = APP.find("status_code=404", i)
    assert j > 0, "detail 里找不到 404 分支"

    branch = APP[i:j]

    missing = [k for k in IDX_KEYS if '"{}"'.format(k) not in branch]

    assert not missing, \
        "detail 的 404 分支上下文少了：{}（会变成 500）".format(missing)
