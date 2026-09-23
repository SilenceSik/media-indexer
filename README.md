# Local Media Manager

本地媒体库索引与整理工具：扫描视频文件 → 识别番号 → 落库 → 查询/质检/整理。

面向「文件名里带番号」的本地媒体库（数万文件量级）。核心价值是**把文件名解析成
结构化编号**，并围绕编号建立索引，再加一道**删除门控**——只有拿到已验证磁力链接的
条目才允许被清理。

## 它能做什么

- **扫描识别**：遍历配置目录，从文件名（回退到直接父目录名）解析编号
- **格式白名单**：只收标准视频格式，可按需追加
- **SQLite 索引**：三个库分工明确（见下）
- **增量扫描**：基于 file_index 跳过未变更文件；字典变了自动全量重算
- **MCP 工具层**：5 个工具，供 Agent 客户端调用
- **质检查询**：缺元数据、缺封面、重复、库统计
- **删除门控**：可删列表 = 有「已验证磁力」的编号，没磁力一律不可删

## Install

```bash
pip install -r requirements.txt
```

## 配置

编辑 `config.yaml`：

```yaml
scan_paths:
  - "X:\\"                 # 扫描根目录（多个）

video_extensions: []       # 追加格式（**追加**语义，见下）
excluded_dir_segments: []  # 追加排除目录（**追加**语义，见下）
database: "storage/library_v2.db"
index_db: "storage/file_index_v2.db"
dictionary: "data/dictionary.json"
```

### 格式白名单是「追加」不是「覆盖」

程序自带一套标准视频格式：

```
.mp4 .mkv .avi .mov .wmv .flv .mpg .mpeg .m4v .ts .m2ts
```

`config.yaml` 里的 `video_extensions` 是**往上加**，不是替换。想额外收 `.webm`：

```yaml
video_extensions:
  - ".webm"
```

选追加而不是覆盖，是因为覆盖语义下**少写一个格式会静默漏扫整类文件**；追加最坏只是多扫。

> `.webm` 不在默认列表里。实测 2.6 万条真实语料中，`.webm` 占 59.4%（15,593 个）但可信命中率为 0：
> 这些文件集中在 `X:\ga\` 成人游戏资源树（Ren'Py 引擎的 `game\images`、`game\movie`、
> `www\movies` 等），是游戏内视频段与引擎缓存，不含番号体系。放开白名单只会引入 1,289 条
> conf=70 的假番号（`KISS-01`、`MAST-001` 这类片段名）。要收就自己加。

### 目录级排除也是「追加」不是「覆盖」

程序自带一套排除目录：

```
ga game www animations __pycache__ node_modules
```

`config.yaml` 里的 `excluded_dir_segments` 同样是**往上加**：

```yaml
excluded_dir_segments:
  - "tencent files"
```

匹配的是路径的**任意一段**（不区分大小写），不是子串包含 —— `X:\ga\...` 排除，
`X:\Gaunt\x.mp4` 保留。

依据（2.6 万条语料实测）：`X:\ga\` 成人游戏资源树占语料 71.6%，贡献 268 条解析命中，
**可信档产出为 0** —— 全部是游戏内视频段与引擎缓存名。带 `game` 段的文件 16,480 个、
带 `www` 段的 2,254 个（两者互有重叠），可信产出同样是 0；而可信档 204 个编号中
无一路径含这些段。排除它们零误伤。

### 语料实测底数

一份 26,230 条真实文件清单（离线快照，不触碰磁盘）跑出的口径，供校准预期：

| 项 | 数 |
|---|---|
| 过格式白名单 | 7,220 |
| 解析出编号 | 440 |
| **可信档（confidence ≥ 90）** | **231 文件 / 204 唯一编号** |
| 可信档逐条核实为真 | 204 / 204 |

可信档全部落在 `.mp4`(208) / `.mkv`(11) / `.wmv`(9) / `.avi`(3)，其余格式贡献 0。

## 运行

### 命令行扫描

```bash
python main.py                    # 扫 config.yaml 的 scan_paths
python main.py "X:\迅雷下载"       # 只扫指定目录（覆盖 scan_paths）
python main.py "X:\片" "X:\下载"   # 扫多个目录
python main.py --list "X:\片"      # 只列出将扫描的目录，不执行
python main.py --dry-run "X:\片"   # 真·干跑：解析并打印结果，不写库
```

遍历目标目录，识别并落库。

**命令行传的目录同样受格式白名单与目录排除约束** —— 它们是安全防线，
不因为「临时扫一下」而打开后门。命令行只改「扫哪里」，不改「什么算影片」。

### Web UI

```bash
python web/app.py                 # 默认 http://127.0.0.1:8811
# 或
python -m uvicorn web.app:app --host 127.0.0.1 --port 8811
```

| 页面 | 作用 |
|---|---|
| `/` | 番号列表（封面网格），支持「有磁力」「缺元数据」筛选 |
| `/search?q=` | 按番号搜索 |
| `/detail/<番号>` | 单条详情：本地文件、磁力列表（含已验证标记）、删除门控状态 |
| `/scan` | 在界面里指定目录扫描，带实时进度与逐文件识别结果 |
| `/docs` | FastAPI 自动生成的接口文档 |

两个环境变量便于用独立数据跑演示实例而不动生产库：

```bash
LMM_DB=/path/to/other.db LMM_COVERS=/path/to/covers python web/app.py
```

> Web UI 是**只读浏览 + 扫描触发**。删除动作不在界面里，见下方「没有磁力就不许删」。

### 作为 MCP 服务

```bash
python -m agent.server      # 推荐（模块方式）
python agent/server.py      # 脚本直跑（MCP 客户端常见配置）
```

两种都支持。暴露的工具：

| 工具 | 副作用 | 说明 |
|---|---|---|
| `search_media` | 只读 | 按编号查询本地媒体 |
| `check_library_quality` | 只读 | 媒体库健康检查 |
| `add_media_file` | **写库** | 手动添加编号与文件关联 |
| `fetch_metadata` | **写队列** | 请求番号元数据同步（入队，由 worker 处理） |
| `scan_library` | **写库** | 扫描目录并落库（不传 folder 则用 `scan_paths`） |

## 架构

### v2 是唯一主线

v1 的扫描/解析/匹配模块**已冻结**，仅作为 v1 数据库的只读迁移参考，v2 链路不引用。
对应关系：

| v1（冻结） | v2（主线） |
|---|---|
| `core/parser.py` | `core/parser_v2.py` |
| `core/matcher.py` | `core/matcher_v2.py` |
| `core/scanner.py` | `core/scanner_v2.py` |
| `core/database.py` | `core/database_v2.py` |
| `core/cache.py` | `core/cache_v2.py` |
| `core/organizer.py` | `core/organizer_v2.py` |
| `adapters/javdb.py` | `adapters/javdb_adapter.py` |

改 bug、加规则请改 v2 那一列。

### 分层

```
main.py / agent/server.py     入口（CLI 扫描 / MCP 服务）
        │
   agent/tools_v2.py          MCP 工具层（5 个工具）
        │
   services/                  MediaService / ScanService / QualityService
        │                     MetadataService / OrganizeService
        │
   core/*_v2.py               Parser / Matcher / Scanner / Database
        │
   adapters/                  JavDB 等外部数据源适配
```

### 数据落在哪

| 库 | 表 | 用途 |
|---|---|---|
| `storage/library_v2.db` | `titles` / `media_files` / `metadata` / `magnets` / `file_index` | 主库：编号、文件、元数据、磁力 |
| `storage/file_index_v2.db` | `file_index` | 扫描增量索引（路径 + size + mtime + 规则指纹）|

`file_index` 会记录**规则集指纹**：`data/dictionary.json` 一改，已索引文件全部视为
变更，下次扫描自动重算——避免"改了字典但旧结果赖着不走"。

## 两条硬规矩

### 1. 编号目录不参与匹配（除一层回退）

文件名解析优先用 basename；**没命中时只回退到直接父目录名**，不再无条件拿整个路径参与匹配。

原因是实测踩过：`ABP-999/random_clip.mp4` 这类会被祖先目录的编号劫持，把无关文件误判入库。
现在只保留「编号目录/video.mp4」这一层。

### 2. 没有磁力就不许删

可删候选来自 `deletable_titles()`，它的条件是**存在 `verified=1` 的磁力记录**：

- 有已验证磁力 → 进候选
- 只有候选磁力（`verified=0`）→ **不进**
- 完全没磁力 → **不进，哪怕它是真片**

理由：搜索结果难免混进错条目，但**错的条目拿不到磁力**。所以"有没有可用的磁力"
比"文件名看着像不像"更能证明这条记录是真的、且已经能重新获取。

配套约束：本项目**不含任何真实删除实现**（无 `os.remove` / `unlink` / `rmtree` /
`send2trash`）。`deletable_titles()` 只产出候选清单，删除动作尚未实现——
将来实现时必须只消费这张表。

## 开发

```bash
pytest tests/ -q
```

测试覆盖：解析规则、格式白名单、增量索引、落库与重链、磁力门控、端到端链路。

## 已知缺口

- `core/cache_v2.py` 扫描缓存**未接线**，重复扫描会重算（大库性能受影响）
- `core/scheduler.py`（定时扫描）、`core/watcher.py`（目录监听）**未接线**，不随服务启动
- v2 暂无「重命名计划（只出计划不执行）」和「按演员/标签检索」能力

详见 `ARCHITECTURE-AND-ISSUES.md`。

## License

MIT
