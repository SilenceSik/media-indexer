"""P2-2 / P2-3 回归测试：权限表一致性 + 依赖声明真实性。

两个 bug 的共同成因是「声明与实际两套名单，无人校对」：
- P2-2：agent/permission.py 列的写工具（move_media / delete_media）从未存在，
  真实注册的写工具（add_media_file / fetch_metadata / scan_library）一个都没列
- P2-3：requirements.txt 声明 tqdm，全仓库零 import

所以这里的断言不是"检查某个值等于某个值"，而是**结构上不可能再漂移**。
"""

import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

if ROOT not in sys.path:

    sys.path.insert(0, ROOT)

from agent import permission  # noqa: E402
from agent.tools_v2 import TOOLS  # noqa: E402


# ---------------------------------------------------------------------------
# P2-2：权限表 vs 实际注册工具
# ---------------------------------------------------------------------------

def test_permission_table_matches_registered_tools():

    """权限表覆盖的工具集合，必须与 tools_v2.TOOLS 完全一致。"""

    declared = set(
        permission.READ_ONLY_TOOLS
    ) | set(
        permission.WRITE_TOOLS
    )

    assert declared == set(TOOLS), (

        f"权限表与注册工具不一致\n"

        f"  只在权限表: {sorted(declared - set(TOOLS))}\n"

        f"  只在注册表: {sorted(set(TOOLS) - declared)}"

    )


def test_no_tool_is_classified_twice():

    overlap = set(
        permission.READ_ONLY_TOOLS
    ) & set(
        permission.WRITE_TOOLS
    )

    assert not overlap, f"工具被同时归入只读与写操作: {sorted(overlap)}"


def test_write_tools_are_not_declared_read_only():

    """三个已知写操作必须在 WRITE_TOOLS 里——这是 P2-2 的原样复现。"""

    for name in ["add_media_file", "fetch_metadata", "scan_library"]:

        assert permission.is_write_tool(name), (
            f"{name} 会改变库内数据，却被判为只读"
        )


def test_read_only_tools_are_not_write():

    for name in ["search_media", "check_library_quality"]:

        assert not permission.is_write_tool(name), (
            f"{name} 不写库，却被判为写操作"
        )


def test_validate_registered_accepts_real_tool_list():

    assert permission.validate_registered(TOOLS) is True


def test_validate_registered_rejects_unclassified_tool():

    """新增工具忘记分类时必须炸，而不是无声放行。"""

    with pytest.raises(ValueError) as exc:

        permission.validate_registered(
            list(TOOLS) + ["brand_new_tool"]
        )

    assert "brand_new_tool" in str(exc.value)


def test_validate_registered_rejects_stale_names():

    """权限表声明了未注册的工具（正是 P2-2 的旧状态）也必须炸。"""

    saved = permission.WRITE_TOOLS

    try:

        permission.WRITE_TOOLS = saved + ["move_media"]

        with pytest.raises(ValueError) as exc:

            permission.validate_registered(TOOLS)

        assert "move_media" in str(exc.value)

    finally:

        permission.WRITE_TOOLS = saved


def test_mcp_entry_validates_on_import():

    """P2-2 的接线点：server.py 在注册后立刻校验，import 即校验。"""

    import agent.server  # noqa: F401

    assert permission.validate_registered(TOOLS) is True


# ---------------------------------------------------------------------------
# P2-3：requirements.txt 声明 vs 实际 import
# ---------------------------------------------------------------------------

# 只作为「运行方式」存在、不会被 import 的包（Web 服务器 / 模板引擎）
RUNTIME_ONLY = {
    "uvicorn",
    "jinja2",
}

# 分发名 -> import 名
DIST_TO_IMPORT = {
    "pyyaml": "yaml",
    "mcp": "mcp",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "requests": "requests",
}


def _requirements_entries():

    """返回 requirements.txt 里**生效**（未注释）的顶层包名。"""

    path = os.path.join(ROOT, "requirements.txt")

    entries = []

    with open(path, encoding="utf-8") as fh:

        for line in fh:

            line = line.strip()

            if not line or line.startswith("#"):

                continue

            name = re.split(r"[<>=!~\[\s]", line)[0].strip().lower()

            if name:

                entries.append(name)

    return entries


def _imported_modules():

    """AST 扫描全仓库，收集所有 import 的顶层模块名。"""

    found = set()

    for dirpath, dirnames, filenames in os.walk(ROOT):

        dirnames[:] = [

            d for d in dirnames

            if d not in (".git", "__pycache__", ".backup", "storage")

        ]

        for filename in filenames:

            if not filename.endswith(".py"):

                continue

            full = os.path.join(dirpath, filename)

            try:

                tree = ast.parse(
                    open(full, encoding="utf-8").read()
                )

            except SyntaxError:

                continue

            for node in ast.walk(tree):

                if isinstance(node, ast.Import):

                    for alias in node.names:

                        found.add(alias.name.split(".")[0])

                elif isinstance(node, ast.ImportFrom):

                    if node.module and node.level == 0:

                        found.add(node.module.split(".")[0])

    return found


def test_every_declared_dependency_is_actually_imported():

    """P2-3 的原样复现：声明了却没人 import 的包会被抓出来。"""

    imported = _imported_modules()

    unused = []

    for dist in _requirements_entries():

        if dist in RUNTIME_ONLY:

            continue

        module = DIST_TO_IMPORT.get(dist, dist)

        if module not in imported:

            unused.append(dist)

    assert not unused, (

        f"requirements.txt 声明了但全仓库零 import: {unused}"

    )


def test_tqdm_is_gone():

    """P2-3 具体案例：tqdm 全仓库零 import，不应再出现在生效声明里。"""

    assert "tqdm" not in _requirements_entries()


def test_mcp_pin_is_exact():

    """mcp 必须精确钉版本：本环境 fastmcp 入口已移除，放宽会导入即失败。"""

    path = os.path.join(ROOT, "requirements.txt")

    with open(path, encoding="utf-8") as fh:

        text = fh.read()

    assert re.search(r"^mcp==\d", text, re.M), (

        "mcp 必须是 ==精确版本，不能是 >= 或未钉"

    )
