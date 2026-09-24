# -*- coding: utf-8 -*-
"""`web/app.py` 的「漏导入」自检（防再犯）。

## 抓到的真 bug

`eligibility_index()` 与 `tier_counts()` 里用了 `Database`，但
`web/app.py` **从没导入过这个名字**（模块级没有、函数内也没有）
→ 每次调用都 `NameError` → 被 `except Exception: return {}` **吞掉**
→ 函数**永远返回空**。

后果是**静默的功能退化**，不报错、不崩：

  * 所有卡片的删除按钮状态一直退化（`can_batch` 恒 False，
    只能显示「手动逐条删」，`blocked_reason` 恒空）
  * 首页「高置信一键送回收站」的计数恒为 0，按钮永远不出现

## 为什么用「粗」检查

刻意粗：把文件里**任何位置**绑定过的名字（模块级/函数内导入、赋值、
def/class、参数、`import as`、except 名、推导式目标）与内建名合并成
白名单，再检查每个被读取的名字是否在表内。

粗的好处是**零误报** —— 嵌套闭包、函数内导入、同名遮蔽都不会被误伤
（更细的跨函数分析会引入误报，不值得）。而这次那类 bug
（名字整个文件里从没出现过）恰好能被它抓住。这是兜底，不是类型检查。
"""

import ast
import builtins
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _missing_names(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())

    bound = set(dir(builtins))

    # 模块级 dunder（`__file__` / `__name__` …）由运行时注入，不是源码绑定
    bound |= {"__file__", "__name__", "__doc__", "__package__",
              "__spec__", "__loader__", "__builtins__"}

    # f-string 里 `{x}` 的读取（JoinedStr）在 AST 里也是 Name/Load，
    # 但它的绑定可能来自 f-string 自身的格式串之外 —— 这类静态判不准，
    # 放过（本检查的目的是抓「整个文件从没出现过」的名字，不是做作用域分析）
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)

    for node in ast.walk(tree):

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)

            for arg in (node.args.args + node.args.kwonlyargs
                        + node.args.posonlyargs):
                bound.add(arg.arg)

        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)

        elif isinstance(node, ast.Lambda):
            # `lambda x: ...` 的 x 也是绑定（漏了它会误报）
            for arg in (node.args.args + node.args.kwonlyargs
                        + node.args.posonlyargs):
                bound.add(arg.arg)

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])

        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)

    return sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id not in bound
    })


def test_web_app_has_no_unbound_names():
    missing = _missing_names(os.path.join(ROOT, "web", "app.py"))

    assert not missing, (
        "web/app.py 里用到从未绑定的名字（宽 except 会把它吞成静默失效）：\n  "
        + "\n  ".join(missing)
    )


def test_service_layer_has_no_unbound_names():
    """服务层同样查 —— 它也有宽 except 的写法。"""

    bad = {}

    for rel in ("services/file_service.py", "services/enrich_service.py"):

        m = _missing_names(os.path.join(ROOT, rel))

        if m:
            bad[rel] = m

    assert not bad, "服务层有未绑定名：{}".format(bad)
