"""MCP 入口：注册 tools_v2 的 5 个工具。

启动方式（两者都支持）：
    python -m agent.server        # 模块方式（推荐）
    python agent/server.py        # 脚本直跑（MCP 客户端常见配置）
"""
import os
import sys

# 脚本直跑时 sys.path[0] 是 agent/ 而不是仓库根，
# 下面的绝对导入 `from agent.tools_v2 import ...` 会解析失败。
# 此处把仓库根补进 sys.path（模块方式下 __package__ 非空，不进入该分支）。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server import MCPServer

# v2 工具（唯一主线，）
from agent.tools_v2 import (
    search_media,
    add_media_file,
    check_library_quality,
    fetch_metadata,
    scan_library
)

from agent.permission import validate_registered


mcp = MCPServer(
    "Local Media Manager"
)


# 注册 v2 工具。
#
# 说明（v1 -> v2 的缺口，未自行补造）：
# 旧 server.py 另外挂了 6 个 v1 工具（missing_metadata / missing_cover /
# statistics / audit_library / rename_plan / actor_search / tag_search），
# 它们全部走 agent/tools.py 的 v1 SQL（videos 表 + metadata.number 关联）。
# v2 侧目前没有等价能力：
#   - 缺元数据的条目   -> 可用 QualityService.report()["data"]["missing_metadata"] 覆盖，
#                          已由 check_library_quality 暴露
#   - 缺封面 / 统计    -> v2 无对应 service 方法
#   - 重命名计划       -> OrganizeService.move() 只有「执行移动」，无「只出计划」
#   - 演员 / 标签检索  -> v2 metadata.actresses / tags 存 JSON 文本，无检索接口
# 因此这 6 个工具在本次切换中**未注册**（不是静默删除：v1 文件保留，
# 需要时按上面的映射补 v2 实现）。

_TOOLS = [
    search_media,
    add_media_file,
    check_library_quality,
    fetch_metadata,
    scan_library,
]

for _t in _TOOLS:
    mcp.add_tool(_t)

# P2-2 修复：注册期自检。
# 每个注册的工具必须在 agent/permission.py 里有且仅有一个分类
# （只读 / 写操作），否则这里直接抛 ValueError，服务起不来。
# 宁可起不来，也不要「写操作被当成只读」无声放行。
validate_registered([t.__name__ for t in _TOOLS])


if __name__ == "__main__":

    mcp.run()
