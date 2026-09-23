"""MCP 入口层测试（Wave3 缺口：入口此前零覆盖，且 C4 刚改过它）。

覆盖两件真实会坏的事：
  1. 两种启动方式都必须能起 —— 脚本直跑曾因 sys.path[0]=agent/ 而
     `ModuleNotFoundError: No module named 'agent.tools_v2'`。
  2. 工具必须真注册上去（不是「import 成功」就算数）。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(args, timeout=25):
    """起服务子进程；stdio 服务正常时静默等待 -> 超时视为启动成功。"""
    try:
        p = subprocess.run(
            args, cwd=str(REPO), capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", ""  # 还在等 stdin = 起来了


def test_script_launch_does_not_crash():
    """python agent/server.py —— MCP 客户端最常见的配置形态。

    回归：修复前必然 ModuleNotFoundError（sys.path[0] 是 agent/）。
    """
    rc, out, err = _run([PY, str(REPO / "agent" / "server.py")])
    assert "ModuleNotFoundError" not in err, f"脚本直跑崩了:\n{err}"
    assert "Traceback" not in err, f"脚本直跑有异常:\n{err}"


def test_module_launch_does_not_crash():
    """python -m agent.server —— 模块方式不能因修复而回归。"""
    rc, out, err = _run([PY, "-m", "agent.server"])
    assert "ModuleNotFoundError" not in err, f"模块方式崩了:\n{err}"
    assert "Traceback" not in err, f"模块方式有异常:\n{err}"


def test_tools_are_actually_registered():
    """工具必须真挂在 MCPServer 上 —— 不是「import 成功」就算数。"""
    probe = (
        "import asyncio,sys;"
        "sys.path.insert(0, r'%s');"
        "from agent.server import mcp;"
        "ts=asyncio.run(mcp.list_tools());"
        "print(sorted(t.name for t in ts))" % REPO
    )
    p = subprocess.run(
        [PY, "-c", probe], cwd=str(REPO), capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, f"取工具列表失败:\n{p.stderr}"
    names = set(eval(p.stdout.strip()))
    expected = {
        "search_media",
        "add_media_file",
        "check_library_quality",
        "fetch_metadata",
        "scan_library",
    }
    missing = expected - names
    assert not missing, f"这些工具没注册上: {missing}（实际: {names}）"
