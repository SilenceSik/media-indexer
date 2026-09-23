"""工具权限分类表（v2 主线）。

用途（两个，都是真的，不是摆设）：

1. **注册期自检** —— `agent/server.py` 注册工具后调 `validate_registered()`，
   任何一个工具没被分类就抛 `ValueError`。防止将来新增工具时忘记分类，
   导致写操作无声通过（这正是 P2-2 的成因：声明与实际注册两套名单，无人校对）。
2. **供 MCP 调用方判断副作用** —— Agent 侧可读 `is_write_tool()` 决定要不要
   先问用户。

⚠️ 边界说明（不要误读本表的能力）：
MCP 协议本身没有"交互式确认"通道，`MCPServer.add_tool()` 也不接受确认回调。
所以本表**不是运行时门禁**，它是一份「分类契约 + 注册期断言」。
真正的确认责任在调用方（Agent 侧），本表负责让调用方**能准确地知道**
哪些调用有副作用——这比一个永远返回 True 的假门禁更诚实。

历史：旧版本列的是 `move_media` / `delete_media`，这两个工具**从未存在**；
而真实注册的 `add_media_file` / `fetch_metadata` / `scan_library` 三个写操作
一个都没在表里（P2-2）。本次按 `agent/tools_v2.TOOLS` 实际注册名单重写，
并加测试锁死两者一致。
"""

# 只读：不改变库内数据
READ_ONLY_TOOLS = [

    "search_media",

    "check_library_quality",

]

# 写操作：会改变库内数据（落库 / 入队）
WRITE_TOOLS = [

    # 向 titles / media_files 落库
    "add_media_file",

    # 写入 tasks 队列，后续由 worker 落 metadata
    "fetch_metadata",

    # 扫描并 persist=True 落库
    "scan_library",

]


def is_write_tool(tool):

    """该工具是否具有副作用（写库 / 入队）。"""

    return tool in WRITE_TOOLS


def require_confirm(tool):

    """保留旧接口语义：调用方是否需要先向用户确认。

    注意返回 True 只代表「这个工具是写操作」，不代表门禁已生效——
    门禁在调用方。命名沿用历史，避免破坏潜在外部调用。
    """

    return is_write_tool(tool)


def validate_registered(tools):

    """注册期自检：每个注册的工具必须且只能落在一个分类里。

    在 agent/server.py 注册完成后调用。未分类 / 重复分类 / 分类了但没注册
    都会抛 ValueError，让 mcp 服务直接起不来——宁可起不来，也不要无声放行。

    返回 True 表示校验通过。
    """

    registered = list(tools)

    unknown = [

        t for t in registered

        if t not in READ_ONLY_TOOLS and t not in WRITE_TOOLS

    ]

    if unknown:

        raise ValueError(

            f"工具缺少权限分类，请补进 agent/permission.py: {unknown}"

        )

    both = [

        t for t in registered

        if t in READ_ONLY_TOOLS and t in WRITE_TOOLS

    ]

    if both:

        raise ValueError(

            f"工具同时被分类为只读与写操作: {both}"

        )

    declared = set(READ_ONLY_TOOLS) | set(WRITE_TOOLS)

    stale = sorted(

        declared - set(registered)

    )

    if stale:

        raise ValueError(

            f"权限表声明了未注册的工具（已失效名单）: {stale}"

        )

    if len(declared) != len(READ_ONLY_TOOLS) + len(WRITE_TOOLS):

        raise ValueError(

            "权限表内部重复：同一个工具同时出现在只读与写操作名单里"

        )

    return True
