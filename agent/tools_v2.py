"""MCP 工具层（v2 主线）。

入口依据：本环境 mcp == 2.0.0，`mcp.server.fastmcp` 已移除，
server 类改名为 `mcp.server.MCPServer`（实测确认）。

⚠️ 线程模型（实机踩过，别改回去）：
mcp 2.0.0 的 MCPServer 对**同步**工具函数走
`anyio.to_thread.run_sync`（worker 线程执行），而 sqlite3 默认禁止
跨线程复用 connection。因此下面所有 DB/Service 都用**函数内新建**，
不再用模块级单例——模块级 connection 建在 import 线程，一调用就报
`sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread`。
"""

import json
import os

import yaml

from mcp.server import MCPServer

from services.media_service import MediaService

from services.scan_service import ScanService

from services.quality_service import QualityService

from services.metadata_service import MetadataService

from core.task_queue import TaskQueue

from core.database_v2 import Database


# ---------------------------------------------------------------------------
# 配置与路径：一律基于「本文件所在仓库根」解析成绝对路径。
# 原先 DB = "storage/library_v2.db" 是相对路径，从别的 cwd 启动会连到另一个库。
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(

    os.path.dirname(

        os.path.abspath(__file__)

    )

)

CONFIG_PATH = os.path.join(

    ROOT,

    "config.yaml"

)


def load_config():

    with open(

        CONFIG_PATH,

        encoding="utf-8"

    ) as fh:

        return yaml.safe_load(

            fh
        ) or {}


def resolve_path(path):

    """相对路径按仓库根解析为绝对路径；空值原样返回。"""

    if not path:

        return path

    if os.path.isabs(

        path

    ):

        return path

    return os.path.join(

        ROOT,

        path

    )


CONFIG = load_config()


# v2 主库（config.yaml: database）
DB = resolve_path(

    CONFIG.get(

        "database"

    )
)


# 扫描增量索引库（config.yaml: index_db，缺省 storage/file_index_v2.db）
# file_index 表由 core.file_index.FileIndex 独立建库，不在 library_v2.db 里。
INDEX_DB = resolve_path(

    CONFIG.get(

        "index_db"

    )

    or

    "storage/file_index_v2.db"
)


# 番号规则（config.yaml: dictionary，v2 格式 = {"rules": [...]}）
DICTIONARY = resolve_path(

    CONFIG.get(

        "dictionary"
    )
)


def load_rules():

    with open(

        DICTIONARY,

        encoding="utf-8"

    ) as fh:

        return json.load(

            fh
        )["rules"]


def build_db():

    """每次调用新建：见文件头「线程模型」。"""

    return Database(

        DB
    )


def build_quality():

    return QualityService(

        DB
    )


def build_media():

    return MediaService(

        DB
    )


def build_metadata_service():

    return MetadataService(

        TaskQueue(

            DB
        )
    )


def build_scan_service():

    return ScanService(

        INDEX_DB,

        load_rules(),

        db=build_db(),

        # config.yaml: video_extensions 是「追加」语义（内置标准格式
        # 由 core.scanner_v2 叠加，见 resolve_extensions）
        extensions=CONFIG.get(

            "video_extensions"

        ) or []
    )


mcp = MCPServer(

    "Local Media Manager"
)


@mcp.tool()

def search_media(
    number: str
):

    """
    查询本地媒体
    """

    return build_media().search(
        number
    )


@mcp.tool()

def add_media_file(
    number: str,
    filepath: str
):

    """
    添加媒体文件
    """

    return build_media().add_file(
        number,
        filepath
    )


@mcp.tool()

def check_library_quality():

    """
    检查媒体库健康状态
    """

    return build_quality().report()


@mcp.tool()

def fetch_metadata(
    number: str
):

    """
    请求番号元数据同步
    """

    return build_metadata_service().request(
        number
    )


@mcp.tool()

def scan_library(
    folder: str = ""
):

    """
    扫描指定目录并把识别结果落库；folder 为空时扫 config.yaml 的 scan_paths
    """

    folders = [

        folder

    ] if folder else list(

        CONFIG.get(

            "scan_paths"
        ) or []
    )

    service = build_scan_service()

    scanned = []

    failed = []

    for path in folders:

        try:

            result = service.scan(

                path,

                persist=True
            )

            rows = result["data"]

            scanned.append(

                {

                    "folder":

                        path,

                    "files":

                        len(rows),

                    "persisted":

                        sum(

                            1

                            for row in rows

                            if row["persisted"]

                        )

                }
            )

        except Exception as exc:

            failed.append(

                {

                    "folder":

                        path,

                    "error":

                        f"{type(exc).__name__}: {exc}"

                }
            )

    return {

        "folders":

            scanned,

        "failed":

            failed

    }


TOOLS = [

    "search_media",

    "add_media_file",

    "check_library_quality",

    "fetch_metadata",

    "scan_library"

]
