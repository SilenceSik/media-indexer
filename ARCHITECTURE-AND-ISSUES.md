# Local Media Manager — 架构与当前问题

> 生成时间：2026-09-23
> 代码版本：`1af9f9d`（已 pull 到最新），**工作区有未提交改动**（见 §7）
> 本文所有结论均来自实机运行，非读代码推断。复现命令附在每条问题下。

---

## 0. 一句话结论

**框架方向是对的，骨架搭得住；但现在跑不起来。**

分层设计（Database → TaskQueue → Worker → Service → MCP）合理，可以长大。问题是**关节没接上**：扫描结果进不了数据库、质检一调用就崩、MCP 入口导入即失败。

需要的是**接口缝合**，不是重写。但在缝合前必须先定一条：**v2 是唯一主线，v1 冻结为只读迁移源**。不定这条，每加一层都会再断一次。

---

## 1. 项目定位

本地媒体文件索引管理工具：扫描本地视频 → 文件名清洗 → 番号识别 → 建库 → 拉取元数据 → 整理归档。

最终形态是一套**常驻后台服务**，通过 MCP 暴露给 Agent（供思思调用），而不是一堆手动跑的脚本。

---

## 2. 目标架构（spec 意图）

spec（`attachments/新建 文本文档.txt` + `-2.txt`）规划了五个批次：

| 批次 | 内容 | 状态 |
|---|---|---|
| v2.0 | titles/media_files 一对多模型 + parser/matcher 拆分 + v1→v2 迁移 | 已提交 |
| v2.1 | Organizer 事务化 + Scanner 增量索引 + 操作回滚日志 | 已提交 |
| v2.1.1 | 基础设施修复层（v1 拒启 / TaskQueue / Worker / 质量检查 / Service 返回格式） | **未提交** |
| v2.2–v2.5 | 统一任务调度 / AI 整理层 / 知识图谱 | 部分落地，未串联 |

spec 设计的调用链：

```
Agent (MCP)
    │
Task Service
    │
Task Queue  ──→  Worker Manager  ──→  Metadata / Scanner / Repair Worker
    │
Database v2
    ├── titles ────┬── media_files
    │              ├── metadata
    │              └── graph (actors / tags)
    └── file_index
```

设计上强调两点：
- **所有耗时操作统一进队列**（JavDB 查询不能同步执行，否则扫 10000 个文件 = 查询 10000 次网页 = 程序卡死）
- **所有关系统一走 `id`**，不用 `number` 做外键（`metadata.number` 被 spec2 明确废止）

---

## 3. 当前实际架构

代码 72 个文件，其中 `core/` 37 个 py。**骨架在上，但上半截是空的。**

```
   入口层（真实入口全部指向 v1）              入口层（v2 唯一入口 = 测试）
   ┌────────────────────────┐                ┌──────────────────────┐
   │ main.py                │                │ agent/tools_v2.py    │  ← 导入即崩
   │ service.py             │                │   (缺 mcp 实例)       │
   │ web/app.py             │                └──────────────────────┘
   │ agent/server.py        │  ← 导入即崩              │
   │ agent/tools.py         │                ┌──────────────────────┐
   └────────────────────────┘                │ services/*.py  (6个) │
              │                              └──────────────────────┘
              ▼                                          │
   ┌────────────────────────┐                ┌──────────────────────┐
   │ storage/library.db     │                │ core/database_v2.py  │
   │ (v1: videos 表)         │                │ core/task_queue.py   │
   └────────────────────────┘                │ core/quality.py      │  ← 一调就崩
                                             │ core/metadata_worker │
                                             └──────────────────────┘
                                                         │
                                             ┌──────────────────────┐
                                             │ storage/library_v2.db │
                                             │ titles / media_files  │
                                             │ metadata / file_index │
                                             └──────────────────────┘
        ╰────────── 两套世界，互不相通 ──────────╯
```

**各层实测状态：**

| 层 | 状态 | 证据 |
|---|---|---|
| `core/database_v2.py` | ✅ 可用 | 四张表正确建立 |
| `core/database_guard.py` | ✅ 可用 | 正确拦截 v1 库 |
| `core/task_queue.py` | ⚠️ 半可用 | add/get/done 能跑，但 `get()` 返回过期状态 |
| `core/metadata_worker.py` | ✅ 可用 | mock JavDB 下成功写入 metadata |
| `services/media_service.py` | ✅ 可用 | 返回格式统一 |
| `services/scan_service.py` | ❌ 空转 | 扫描结果不落库 |
| `services/quality_service.py` | ❌ 必崩 | SQL 引用不存在的列 |
| `agent/tools_v2.py` | ❌ 导入失败 | `NameError: name 'mcp' is not defined` |
| `agent/server.py` | ❌ 导入失败 | mcp 2.0.0 已移除 `mcp.server.fastmcp` |

---

## 4. 数据模型（以实际建表为准）

`core/database_v2.py` 实际创建：

```sql
titles       (id, number, title, maker, created_time)
media_files  (id, title_id, filepath, filename, file_hash, size, created_time)
metadata     (id, title_id, title, cover, cover_local, release_date,
              maker, actresses, tags, updated_time)          -- 注意：没有 number 列
file_index   (path, size, mtime, last_scan)
tasks        (id, type, payload, status, priority, retry, error, created_time,
              started_time)                                  -- 由 TaskQueue 建
```

**关键约束：`metadata` 表没有 `number` 列，关联一律走 `title_id → titles.id`。**

---

## 5. 当前问题清单

### P0 — 阻断性（不修则整套跑不通）

#### P0-1　扫描结果不进数据库（最致命）

**现象：** 扫描 3 个文件，解析出 `IPX-123`、`SSIS-001`，但库内 `titles=0`、`media_files=0`。

**根因：** `services/scan_service.py` 全流程只做「扫文件 → 解析番号 → 返回列表」，**没有任何一行写库**。唯一的落库入口是 `MediaService.add_file(number, filepath)`，即「按番号逐个加文件」，而不是「扫完自动入库」。

> ⚠️ 注意：**这是 spec 的设计缺口，不是实现偏离 spec。** 两份 spec 的 `ScanService` 都只写 scan + parse + return。所以修它属于**补设计**，需要先决定语义。

**额外：** 构造函数签名是 `ScanService(index_db, rules)`（两个参数），与 `agent/tools_v2.py` 的调用方式对不上。

**修法（需先定语义）：**
- 方案 A（推荐）：`scan()` 内部对每个识别出的番号调 `db.get_or_create_title()` + `db.add_file()`，扫描即入库
- 方案 B：`scan()` 只返回结果，另开 `import_scan_result()` 显式入库

**验证：** 扫描后 `SELECT COUNT(*) FROM titles` 应 > 0。

---

#### P0-2　质检 SQL 引用不存在的列

**现象：** `QualityService.report()` 抛 `sqlite3.OperationalError: no such column: metadata.number`。测试中因此挂掉 3 个。

**位置：** `core/quality.py:108-109`

```sql
LEFT JOIN metadata ON titles.number = metadata.number   -- ❌ metadata 无 number 列
```

**根因（重要）：两份 spec 自相矛盾，代码抄了旧的那份。**

| 来源 | 写法 | 正误 |
|---|---|---|
| `attachments/新建 文本文档.txt`（v2.1 批次） | `ON titles.number = metadata.number` | ❌ 错 |
| `attachments/新建 文本文档-2.txt`（v2.1.1 修复批次） | 明确写「这里不要再用 `metadata.number`。原因：v2 已经有 `titles.id`，所有关系统一走 id」，并给出正确 SQL | ✅ 对 |

而 `core/database_v2.py` 的 `metadata` 建表**用的是 spec2 的版本**（有 `title_id`，无 `number`）。

→ **结果：建表用了新 spec，查询用了旧 spec，两边对不上。**

**修法（spec2 已给出正确写法）：**

```sql
SELECT number FROM titles
WHERE id NOT IN (SELECT title_id FROM metadata)
```

**验证：** `python -m pytest tests/ -q` 中 3 个 quality 测试转绿。

---

#### P0-3　MCP 入口是死的（两条路都断）

**路径一：`agent/tools_v2.py`**
四个工具函数（`search_media` / `add_media_file` / `check_library_quality` / `fetch_metadata`）的装饰器 `@mcp.tool()` 都在（第 40 行起），**缺的是 `mcp` 这个对象本身**——文件里从头到尾没有 `mcp = FastMCP(...)` 之类的实例化，所以 import 到装饰器那一行就炸：`NameError: name 'mcp' is not defined`。

**路径二：`agent/server.py`**
第 1 行 `from mcp.server.fastmcp import FastMCP`，但环境里是 **mcp 2.0.0**，`mcp.server.fastmcp` 已被移除（实测子模块列表里没有 fastmcp）→ 导入失败。且 server.py 挂的是 **v1 的老工具**（`agent/tools.py`），压根没引用 tools_v2。

**修法：**
1. tools_v2 补 `mcp = FastMCP("...")` 实例 + 恢复装饰器
2. 明确 server.py 用哪套工具（建议切到 tools_v2），并按当前 mcp 2.x API 改写导入
3. `requirements.txt` 把 `mcp>=1.0` 钉到实际版本

**验证：** `python -c "import agent.tools_v2"` 无报错；能列出 4 个 tool。

---

#### P0-4　TaskQueue.get() 返回过期状态

**现象：** `get()` 返回 `status='pending'`，但库内已更新为 `running`。

**位置：** `core/task_queue.py:109-148` —— 先 `SELECT` 取行 → 再 `UPDATE status='running'` → 最后 `return dict(row)`，返回的是 **UPDATE 之前**的快照。

**这是 spec 自身的设计疏漏**（`fetch()` 版本同样 `return dict(row)`），但测试 `test_task_queue_add_get_done` 断言 `task["status"] == "running"` → 冲突。

**修法（二选一，需拍板）：**
- A：更新后重新读取再返回（改代码，让 `status` 真实为 `running`）
- B：改测试断言为 `pending`（承认返回的是领取前状态）

推荐 A —— 队列的语义应是「领取后即为 running」。

---

### P1 — 结构性问题

#### P1-1　两套世界并行，v2 是完全的孤岛

| | v1 世界 | v2 世界 |
|---|---|---|
| 入口 | `main.py`、`service.py`、`web/app.py`、`agent/server.py`、`agent/tools.py`、`organize.py`、`metadata_update.py` | **只有 `agent/tools_v2.py`（且是坏的）** |
| 数据库 | `storage/library.db` → `videos` 表 | `storage/library_v2.db` → `titles` 表 |
| 谁在用 | **全部真实入口** | 只有 `tests/` 和临时探针 |

实测：除 `agent/tools_v2.py` 外，**没有任何生产代码 import `services/*`**（只有 tests 引用）。

更关键的是 `config.yaml` 里写的是 `database: "storage/library.db"` —— **配置指向 v1 库**。也就是说，现在手动跑 `python main.py`，走的是老世界，v2 这一整层白写。

**修法：** 明确 v2 为主线，把入口层（main / service / MCP）全部切到 `services/*` + `library_v2.db`；v1 代码保留为只读迁移源，标记 deprecated。

**✅ 已修（2026-09-23）**

- `main.py` 已切到 `services/scan_service.py` + `core/database_v2.py`，写 `library_v2.db`
- `web/app.py` 已切到 v2 库（模板变量名仍叫 `videos`，但数据源是 `titles`/`media_files`/`metadata`，
  关联走 `titles.id`，不用 `metadata.number`）
- `config.yaml` 的 `database` 已指向 `storage/library_v2.db`，`database_v1_legacy` 单列 v1 库
- 上表「谁在用 / 只有 tests 引用」描述的是**修复前**的状态，保留作为问题记录

---

#### P1-2　番号识别覆盖率不足

`core/parser_v2.py` + `data/dictionary.json`（**仅 13 条规则**：ABP / DASS / FC2 / HEYZO / IPX / JUL / JUQ / MIDE / ONED / SABA / SDDE / SSIS / STARS）。

实测结果：

| 输入 | 结果 | 判定 |
|---|---|---|
| `SSIS-001.mkv` | `SSIS-001` (100) | ✅ |
| `IPX-123.mp4` | `IPX-123` (100) | ✅ |
| `ABP-001.mp4` | `ABP-001` (100) | ✅ |
| `STARS-456.mp4` | `STARS-456` (90) | ⚠️ 分数偏低 |
| `MIDE-789.mp4` | `MIDE-789` (90) | ⚠️ 分数偏低 |
| `FC2-PPV-1234567.mp4` | **`PPV`** | ❌ 错值 |
| `FC2-1234567.mp4` | **`''`** | ❌ 空值 |
| `ABC-123.mp4` | `[]` | ❌ 未识别（规则表无 ABC） |
| `MIRD-234.mp4` | `[]` | ❌ 未识别 |
| `2728927.mp4` | `[]` | ❌ 未识别（无厂牌纯数字番号） |

**修法：** FC2 需单独处理（`FC2-PPV-{数字}` / `FC2-{数字}` 两种形态）；补常见片商；考虑通用 `[A-Z]{2,5}-\d{3,}` 兜底规则。这是**纯配置工作**，不涉及架构。

---

#### P1-3　v2.1.1 整层未提交

工作区 11 个修改 + 4 个新增文件（含 315 行测试），GitHub 上没有。远程唯一的领先提交 `1af9f9d` 只动了 v1 的 `core/database.py`，不冲突。

```
 M adapters/javdb_adapter.py     M services/media_service.py
 M agent/tools_v2.py             M services/metadata_service.py
 M core/database_v2.py           M services/organize_service.py
 M core/metadata_worker.py       M services/quality_service.py
 M core/operation_log.py         M services/scan_service.py
 M core/task_queue.py
?? _probe_v211.py      ?? core/database_guard.py
?? services/result.py  ?? tests/
```

⚠️ `adapters/javdb_adapter.py` 的改动已核：新增 `JavDBCLIClient`（走 `javdb` CLI 子进程 + `--json` 取数据），**不含凭据字面量**（已 grep token/password/api_key/secret/cookie 等关键词，确认为空）。但它引入了一个**外部运行依赖 `javdb` 命令**——环境里没有这个 CLI 时，元数据抓取会静默返回 `None`（代码里 `except` 直接吞掉异常）。部署前需确认该 CLI 已装。

---

### P2 — 次要问题

**P2-1　成对模块，无人声明谁是当前版本** ✅ 已修（2026-09-23）

```
database.py / database_v2.py        parser.py / parser_v2.py
matcher.py  / matcher_v2.py         scanner.py / scanner_v2.py
cache.py    / cache_v2.py           organizer.py / organizer_v2.py
hash.py     / filehash.py           javdb.py  / javdb_adapter.py
```

无 README、无 deprecation 标记。这是最容易踩坑的地方——改错文件白费功夫。

→ 已修：v1 那一列全部加模块头冻结标记（指向 v2 对应文件 + 保留原因 + "改 bug 请改 v2"）；
无 v2 对应的死模块（`scheduler` / `watcher` / `cache_v2`）加「未接线」标记，
写明"它现在不跑"，避免误以为定时扫描或目录监听是生效的。
`README.md` 补了 v1→v2 对照表与分层图。

---

**P2-2　权限表形同虚设** ✅ 已修（2026-09-23）

`agent/permission.py` 声明的工具名与实际注册的对不上：

| permission.py 声明 | 实际注册 |
|---|---|
| `search_media` ✅ | `search_media` |
| `scan_library` ❌ | — |
| `move_media` ❌ | — |
| `delete_media` ❌ | — |
| — | `add_media_file` |
| — | `check_library_quality` |
| — | `fetch_metadata` |

四个里只有一个对得上。写操作（`add_media_file`）会**绕过确认机制**。

**复核时发现比记录的更严重**：`agent/permission.py` 当时**完全无人 import**（AST 全仓扫描，
引用者 0）——不是"对不上"，是根本没接进链路，等于一张贴着门禁标签的废纸。

→ 已修：
1. `agent/permission.py` 按 `agent/tools_v2.TOOLS` 实际名单重写：
   `READ_ONLY = [search_media, check_library_quality]`、
   `WRITE = [add_media_file, fetch_metadata, scan_library]`
   （`fetch_metadata` 写 tasks 队列、`scan_library` 落库，都属写操作）
2. 新增 `validate_registered()`，在 `agent/server.py` 注册完工具后**立即调用**：
   未分类 / 重复分类 / 声明了未注册的旧名字 → 直接抛 `ValueError`，MCP 服务起不来。
   宁可起不来，也不要「写操作被当成只读」无声放行。
3. 加测试锁死：权限表 ↔ 注册表集合必须完全一致；把 `move_media` 塞回权限表必须炸。

⚠️ 边界（不夸大本表能力）：MCP 协议没有交互式确认通道，`MCPServer.add_tool()`
也不接受确认回调。本表是**分类契约 + 注册期断言**，不是运行时门禁——
真正的确认责任在调用方（Agent 侧），本表负责让调用方**能准确知道**哪些调用有副作用。
这比一个永远返回 `True` 的假门禁诚实。

---

**P2-3　依赖缺失 / 版本钉错** ✅ 已修（2026-09-23）

`requirements.txt` 声明了，但实测环境：

| 包 | 状态 |
|---|---|
| `watchdog` | ❌ 未安装（`core/watcher.py` 依赖） |
| `schedule` | ❌ 未安装（`core/scheduler.py` 依赖） |
| `mcp` | ⚠️ 实际 2.0.0，声明 `>=1.0`（API 已不兼容） |

**AST 复核补充**：`watchdog` / `schedule` 各自**只被一个模块 import**，而那两个模块
（`core/watcher.py`、`core/scheduler.py`）本身**无人引用**——所以"未安装"不影响主线，
真正的毛病是声明与实际不符。另外 `tqdm` 声明了但**全仓库零 import**。

→ 已修：`requirements.txt` 按实际 import 点分组重写（核心 / Web / v1 遗留三段），
`mcp` 钉死 `==2.0.0` 并注明理由（2.0.0 已移除 `mcp.server.fastmcp`），
`tqdm` 移除，`schedule` / `watchdog` 降为注释（标 v1 遗留、需自行接线）。
加测试锁死：任何"声明了却零 import"的包都会让测试变红；`mcp` 必须是 `==` 精确钉版。


**P2-4　测试只覆盖 v2.1.1，无端到端测试** ✅ 已补（2026-09-23）
`tests/test_v211_base.py`（315 行）覆盖 spec 的四组验收标准，但**没有一条测试串起「扫描 → 落库 → 质检 → MCP 调用」全链路**——正因如此，P0-1（扫描不落库）这种致命问题才没被测出来。

→ 已补 `tests/test_e2e_pipeline.py`（3 条）：直接调用 `agent/tools_v2.py` 里
`@mcp.tool()` 注册的真实工具函数（mcp 2.0.0 装饰后返回原函数），把 `DB`/`INDEX_DB`
重定向到 tmp 后串通四环节，断言**跨环节数据一致**（扫描落的番号必须能被检索到、
质检必须能看到刚落的数据、扫后删文件必须被质检报出）。已做 mutation 验证：
把质检指向另一份库（复现 P0-2 形态）→ 两条测试立刻红，恢复后 188 passed。

**P2-6　番号匹配未限定格式（`video_extensions` 是死配置）** ✅ 已修（2026-09-23）

**现象：** `config.yaml` 里写着 `video_extensions`（.mp4/.mkv/.avi/.mov/.wmv/.ts），但
`core/scanner_v2.py` 的 `os.walk` **从不过滤扩展名** —— 这个配置项从来没有被任何代码引用过。
结果：语料里 15,593 个 `.webm` 全部进解析流程，既拖慢扫描，又制造大量低分假阳性。

> **定性更正（2026-09-23 二轮实测）：** 这里原先写 `.webm` 是「录屏 / OF / 3D 动画」，
> 经全量语料复核**不准确**。真实构成是 `X:\ga\` **成人游戏资源树**
> （Ren'Py 引擎的 `game\images`、`game\movie`、`www\movies` 等），是游戏内视频段与
> 引擎缓存名，其余为零星的 Photoshop 工具提示视频与 tumblr 片段。共同点只有一个：
> **它们在可信档的产出是 0**。

**根因：** 扫描器只做了「变化检测」，从没有过「这是什么类型的文件」这一层。

**修法（主人 2026-09-23 定的规矩 ①：限定格式，默认几个标准格式 + 支持自定义添加）：**

1. `core/scanner_v2.py` 新增 `DEFAULT_VIDEO_EXTENSIONS`（11 个标准格式）+
   `resolve_extensions()`（**追加**语义：配置是「我还想多收哪些」，内置标准格式始终保留）+
   `Scanner.accepts()`；过滤放在 `os.stat` **之前**（非目标格式连索引都不进）
2. `services/scan_service.py` 透传 `extensions`；`agent/tools_v2.py` 从 config 读取
3. `config.yaml` 的 `video_extensions` 改为追加语义并写明实测依据

**为什么是追加语义而不是覆盖：** 配置是给人手写的，覆盖语义下少写一个格式会
**静默漏扫整类文件**；追加语义下最坏只是多扫。方向是安全的。

**实测依据（26,230 条真实语料，`真实文件名快照`）：**

| 扩展名 | 文件数 | 可信档命中（conf≥90） | 产出率 |
|---|---|---|---|
| `.webm` | 15,593 | **0** | **0.00%** |
| `.mp4` | 8,952 | 200 | 2.23% |
| `.avi` | 837 | 3 | 0.36% |
| `.ts` | 280 | 0 | 0.00% |
| `.mov` | 178 | 0 | 0.00% |
| `.mkv` | 172 | 11 | 6.40% |
| `.rmvb` | 133 | 0 | 0.00% |
| `.wmv` | 61 | 9 | 14.75% |

可信档 223 个命中**全部**落在 `.mp4 / .mkv / .wmv / .avi` 四种格式上。

**验证：** 出厂默认白名单保留 10,495/26,230（40.0%），过滤掉 60% 的文件，
**被过滤掉的文件里可信档命中 = 0 个**（零静默漏扫）。
脚本：`离线验证脚本`。

**已做的 mutation 验证**（`mutation 验证脚本`，5/5 全红、还原复绿）：
① 扫描不过滤扩展名 ② 追加语义改成覆盖语义 ③ 把 `.webm` 塞回默认名单
④ `ScanService` 丢掉 `extensions` 透传 ⑤ 删除门控放松到 `verified>=0`。

---

**P2-7　删除门控的策略（已确认无需改动，补测试锁死）** ✅ 已锁（2026-09-23）

主人 2026-09-23 定的规矩 ②：

> 多抓的可以通过后面抓取磁力的时候把不对的项目去除，错的项目不含磁力，
> **没有磁力的项目即使是真 AV 也不应该删除。**

核查结论：**现有 `deletable_titles()` 已经就是这个语义** ——
它只返回挂着 `verified=1` 磁力的番号，`verified=0` 的候选一律不算，
没有磁力的番号无论多真都不会出现。

另外核过：全项目**没有任何真实删除实现**（`os.remove` / `os.unlink` / `shutil.rmtree` /
`send2trash` 零匹配），所以这道门控目前无法被绕过 —— 可删列表是「候选清单」，
真正的删除动作还没写。真写删除时，必须只消费 `deletable_titles()` 的输出。

→ 已补策略矩阵测试 `test_deletion_gate_requires_verified_magnet`
（真 AV 无磁力 / 假阳性 / 只有 verified=0 候选 三种情形全部不可删）+
`test_deletion_gate_does_not_delete_when_no_local_file`。

---

**P2-5　README 与现状脱节** ✅ 已修（2026-09-23）

README 停留在 v1（只提 `storage/library.db` 和 `python main.py`），未提 v2 架构、MCP 接入、五批演进。

→ 已重写 `README.md`：v2 主线说明、v1→v2 对照表、分层图、三个库分工、
格式白名单的追加语义与实测依据、MCP 工具表（标注哪些有副作用）、
两条硬规矩（编号目录不参与匹配 / 没有磁力不许删）、已知缺口（未接线模块）。

---

## 6. 测试现状（实跑）

**2026-09-23 修复后**（含格式白名单改造 + P2-1/2/3/5 收口）：

```
$ python -m pytest tests/ -q
220 passed in 21.64s
```

文件分布（`--collect-only` 实测）：

| 文件 | 条数 |
|---|---|
| `test_parser_v2.py` | 134 |
| `test_extension_filter.py` | 19 |
| `test_c1b_integrity.py` | 17 |
| `test_v211_base.py` | 16 |
| `test_magnets.py` | 12 |
| `test_permission_and_deps.py` | 11 |
| `test_scan_persist.py` | 5 |
| `test_mcp_entry.py` | 3 |
| `test_e2e_pipeline.py` | 3 |
| **合计** | **220** |

**评估时（修复前，仅存档）**：

```
$ python -m pytest tests/ -q
4 failed, 12 passed in 1.20s
```

失败项：
- `test_task_queue_add_get_done` → P0-4
- `test_quality_checks_same_database` → P0-2
- `test_quality_service_report_shape` → P0-2
- `test_all_services_return_unified_shape` → P0-2

> 注：此前记录的「16/16 全过」已不成立。上表为评估当时的存档，P0/P2 修复后已全绿。

---

## 7. 推进顺序（已全部执行完毕）

**第一步（架构决策）** ✅
1. 确认 **v2 为唯一主线**，v1 冻结为只读迁移源 → 设计决策记录 D1
2. P0-1 落库语义 → 采用 UPSERT 重链方案（见 `core/database_v2.add_file` docstring）
3. P0-4 取 A（写入后重读快照）

**第二步（缝合 P0，让链路活起来）** ✅
4. P0-2 质检 SQL
5. P0-1 扫描落库
6. P0-4 队列快照
7. P0-3 MCP 入口（tools_v2 + server.py + requirements）

**第三步（收口）** ✅
8. 补端到端测试锁住链路（`tests/test_e2e_pipeline.py`）
9. 提交 v2.1.1（已核 `javdb_adapter` 不含凭据）
10. 入口层从 v1 切到 v2（`config.yaml` 的 `database` 已指向 `library_v2.db`）

**第四步（补设计）** ✅
11. 番号识别规则扩充（P1-2）→ 82 条规则，11/11 复现用例通过
12. 成对模块标注与清理（P2-1）→ v1 加冻结标记 + README 对照表

**第五步（格式与门控）** ✅
13. 格式白名单（P2-6）
14. 删除门控策略矩阵（P2-7）
15. 权限表接线 + 依赖声明校正 + README 重写（P2-1/2/3/5）

---

## 8. 交接须知

**当前状态**
```bash
cd X:/dev/local-media-manager
python -m pytest tests/ -q      # → 220 passed
git log --oneline -1
```

**环境**：Windows + Python 3.11，仓库自带 `.venv` 或任一 3.11 解释器；
`watchdog` / `schedule` 属 v1 遗留模块专用，主线不需要装。

**容易踩的坑**
1. 改 `parser / scanner / matcher / database / cache / organizer` 前，先确认改的是 `_v2` 那份
   （v1 那份文件头已加冻结标记）
2. 别照抄旧 spec 的 SQL —— `metadata.number` 已被废止，一律用 `title_id`
3. `scheduler.py` / `watcher.py` / `cache_v2.py` **当前不生效**（未接线），别以为在跑
4. 新增 MCP 工具必须同时补进 `agent/permission.py`，否则 `agent/server.py` 启动即抛错
5. `requirements.txt` 里加依赖必须有真实 import，否则测试会红


---

## 9. 附：v2 服务层返回格式（已实现，供参考）

所有 service 统一返回：
```python
{"success": bool, "data": any, "error": str|None, "trace_id": str}
```
由 `services/result.py` 提供 `success()` / `failure()` 构造器。这一层设计是干净的，实测通过。
