"""Web UI：本地媒体库浏览界面。

路线图：
    /                       列表（支持 ?has_magnet=1 / ?missing_meta=1 筛选）
    /search?q=              按番号搜索
    /detail/<number>        单条详情（本地文件 + 磁力 + 删除门控状态）
    /scan                   扫描页：指定目录、实时进度、结果明细
    /api/scan/start         启动扫描任务
    /api/scan/status        轮询任务进度

数据源是 v2 主库（config.yaml 的 database）。关联一律走 titles.id，
禁止用 metadata.number（见设计决策记录 D4）。
"""

import json
import os
import sqlite3
import threading
import time

import yaml

from fastapi import FastAPI, Request

from fastapi.responses import HTMLResponse, JSONResponse

from fastapi.staticfiles import StaticFiles

from fastapi.templating import Jinja2Templates


BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)


def resolve_path(path):

    if not path or os.path.isabs(path):

        return path

    return os.path.join(
        BASE_DIR,
        path
    )


def load_config():

    with open(
        os.path.join(BASE_DIR, "config.yaml"),
        encoding="utf-8"
    ) as fh:

        return yaml.safe_load(fh) or {}


CONFIG = load_config()

# 环境变量覆盖：便于「用真实数据跑演示实例」而不动生产库。
#   LMM_DB      -> 主库路径（覆盖 config.database）
#   LMM_COVERS  -> 封面目录（覆盖 config.image_storage.covers）
ENV_DB = os.environ.get("LMM_DB")

ENV_COVERS = os.environ.get("LMM_COVERS")

app = FastAPI(
    title="Local Media Manager"
)


templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "web/templates")
)


# 静态资源。模板引的是 /static/style.css —— 缺这个挂载会 404 并导致页面裸奔。
app.mount(
    "/static",
    StaticFiles(
        directory=os.path.join(BASE_DIR, "web/static")
    ),
    name="static"
)


# 模板里封面 URL 是 /images/covers/<file>（见 web/templates/index.html），
# 所以 /images 必须挂到 covers 的**父目录**，否则封面 404。
COVERS_DIR = resolve_path(
    ENV_COVERS
    or (CONFIG.get("image_storage") or {}).get("covers")
    or "storage/images/covers"
)

IMAGE_ROOT = os.path.dirname(
    COVERS_DIR
)

# 目录不存在时 StaticFiles 会直接抛 RuntimeError，导致 web.app 无法导入
os.makedirs(
    COVERS_DIR,
    exist_ok=True
)


app.mount(
    "/images",
    StaticFiles(
        directory=IMAGE_ROOT
    ),
    name="images"
)


# v2 主库；v1 库（"storage/library.db"）已冻结为迁移源，见 设计决策记录 D1
DB_PATH = resolve_path(
    ENV_DB
    or CONFIG.get("database")
)


def query(sql, args=()):
    """只读查询。库不存在时返回空结果而不是 500（首次使用还没扫过）。"""

    if not DB_PATH or not os.path.exists(DB_PATH):

        return []

    conn = sqlite3.connect(
        DB_PATH
    )

    conn.row_factory = sqlite3.Row

    try:

        cur = conn.execute(
            sql,
            args
        )

        return cur.fetchall()

    finally:

        conn.close()


def human_size(n):
    """字节 -> 人类可读。"""

    n = n or 0

    for unit in ("B", "KB", "MB", "GB", "TB"):

        if n < 1024 or unit == "TB":

            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"

        n /= 1024.0


def cover_filename(cover_local):
    """封面本地路径 -> 文件名。

    必须在服务端算：模板里 `split('/')` 对 Windows 的反斜杠路径无效
    （`storage\\images\\covers\\x.jpg`.split('/')[-1] 会返回整条路径 → 404）。
    """

    if not cover_local:

        return None

    return os.path.basename(
        str(cover_local).replace("\\", "/")
    )


def names_of(raw):
    """把 actresses / tags 这类字段渲染成可读文本。

    库里存的是 JavDB 原始 JSON（`[{"id","name","avatar_url"}, ...]`），
    直接丢进模板会显示成一坨 JSON。只取 name 拼起来；解析不了就原样返回。
    """

    if not raw:

        return ""

    text = str(raw).strip()

    if not text.startswith("["):

        return text

    try:

        items = json.loads(text)

    except Exception:

        return text

    if not isinstance(items, list):

        return text

    names = []

    for item in items:

        if isinstance(item, dict):

            name = item.get("name")

            if name:

                names.append(str(name))

        elif item:

            names.append(str(item))

    return " / ".join(names)


# 模板变量名保持 `videos`（模板未动）。
LIST_SQL = """
        SELECT
        titles.number,
        media_files.filename,
        metadata.title,
        metadata.cover_local

        FROM media_files

        JOIN titles

        ON titles.id =
        media_files.title_id

        LEFT JOIN metadata

        ON metadata.title_id =
        titles.id
"""


def rows_to_videos(rows):
    """统一把查询结果加工成模板需要的形状。"""

    out = []

    for r in rows:

        out.append(
            {
                "number": r["number"],
                "filename": r["filename"],
                "title": r["title"],
                "cover_file": cover_filename(r["cover_local"]),
            }
        )

    return out


def library_stats():
    """首页四个数字。"""

    titles = query("SELECT COUNT(*) AS c FROM titles")
    files = query("SELECT COUNT(*) AS c FROM media_files")
    size = query("SELECT COALESCE(SUM(size), 0) AS c FROM media_files")
    magnets = query(
        "SELECT COUNT(*) AS c FROM magnets WHERE verified = 1"
    )
    covered = query(
        "SELECT COUNT(*) AS c FROM metadata WHERE cover_local IS NOT NULL"
    )

    def one(rows):

        return rows[0]["c"] if rows else 0

    return {
        "titles": one(titles),
        "files": one(files),
        "size_h": human_size(one(size)),
        "magnets": one(magnets),
        "covered": one(covered),
    }


@app.get(
    "/",
    response_class=HTMLResponse
)
def index(
    request: Request,
    has_magnet: int = 0,
    missing_meta: int = 0
):

    where = ""
    args = ()

    if has_magnet:

        # 只显示有「已验证磁力」的番号（= 可删候选）
        where = """
        WHERE titles.id IN (
            SELECT title_id FROM magnets WHERE verified = 1
        )
        """

    elif missing_meta:

        where = """
        WHERE titles.id NOT IN (
            SELECT title_id FROM metadata
            WHERE title IS NOT NULL OR cover_local IS NOT NULL
        )
        """

    rows = query(
        LIST_SQL
        + where
        + """
        ORDER BY titles.number
        LIMIT 500
        """,
        args
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": rows_to_videos(rows),
            "stats": library_stats(),
            "q": "",
        }
    )


@app.get(
    "/search",
    response_class=HTMLResponse
)
def search(
    request: Request,
    q: str = ""
):

    rows = query(
        LIST_SQL
        + """
        WHERE titles.number LIKE ?

        ORDER BY titles.number
        LIMIT 500
        """,
        (
            f"%{q}%",
        )
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": rows_to_videos(rows),
            "stats": library_stats(),
            "q": q,
        }
    )


@app.get(
    "/detail/{number}",
    response_class=HTMLResponse
)
def detail(
    request: Request,
    number: str
):

    # 详情页用 numbers.title/maker/actresses/tags，注意与列表页共用同一套列名
    head = query(
        """
        SELECT
        titles.id AS title_id,
        titles.number,
        metadata.title,
        metadata.cover_local,
        metadata.release_date,
        metadata.maker,
        metadata.actresses,
        metadata.tags

        FROM titles

        LEFT JOIN metadata

        ON metadata.title_id =
        titles.id

        WHERE titles.number = ?
        """,
        (
            number,
        )
    )

    if not head:

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "videos": [],
                "stats": library_stats(),
                "q": number,
            },
            status_code=404,
        )

    row = head[0]
    title_id = row["title_id"]

    files = query(
        """
        SELECT filename, filepath, size

        FROM media_files

        WHERE title_id = ?

        ORDER BY filename
        """,
        (
            title_id,
        )
    )

    magnets = query(
        """
        SELECT magnet, source, size_text, verified

        FROM magnets

        WHERE title_id = ?

        ORDER BY verified DESC, size_text DESC
        """,
        (
            title_id,
        )
    )

    total_size = sum(
        (f["size"] or 0) for f in files
    )

    video = {
        "number": row["number"],
        "title": row["title"],
        "cover_file": cover_filename(row["cover_local"]),
        "release_date": row["release_date"],
        "maker": row["maker"] or "",
        "actresses": names_of(row["actresses"]),
        "tags": names_of(row["tags"]),
        "file_count": len(files),
        "size_h": human_size(total_size),
        "files": [
            {
                "filename": f["filename"],
                "ext": os.path.splitext(f["filename"] or "")[1].lstrip(".").lower()
                       or "?",
                "size_h": human_size(f["size"]),
            }
            for f in files
        ],
        "magnets": [
            {
                "magnet": m["magnet"],
                "source": m["source"] or "",
                "size_text": m["size_text"] or "",
                "verified": bool(m["verified"]),
            }
            for m in magnets
        ],
    }

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "video": video,
            "stats": library_stats(),
        }
    )


# ── 扫描任务（内存态，本地单用户够用）─────────────────────
#
# 设计取舍：不引入任务队列/Redis —— 这是本机单人工具，一次只跑一个扫描任务，
# 用进程内 dict + 后台线程就够。进程重启丢任务状态是可接受的（扫描本身幂等，
# 重新发起即可）。

_SCAN_LOCK = threading.Lock()

_SCAN_JOB = {
    "running": False,
    "path": "",
    "dry_run": False,
    "total": 0,
    "done": 0,
    "files": [],
    "saved": 0,
    "error": None,
    "started": None,
    "finished": None,
}


def _reset_job(path, dry_run):

    _SCAN_JOB.update(
        {
            "running": True,
            "path": path,
            "dry_run": dry_run,
            "total": 0,
            "done": 0,
            "files": [],
            "saved": 0,
            "error": None,
            "started": time.time(),
            "finished": None,
        }
    )


def _run_scan(path, dry_run):
    """后台线程体：真实调用 ScanService。"""

    try:

        from core.database_v2 import Database

        from services.scan_service import ScanService

        rules = json.load(
            open(
                resolve_path(CONFIG["dictionary"]),
                encoding="utf-8",
            )
        )["rules"]

        index_db = resolve_path(
            CONFIG.get("index_db") or "storage/file_index_v2.db"
        )

        db = Database(DB_PATH)

        service = ScanService(
            index_db,
            rules,
            db=db,
            extensions=CONFIG.get("video_extensions") or [],
            excluded_segments=CONFIG.get("excluded_dir_segments") or [],
        )

        # 先数一遍总量，前端才能算进度（walk 很快，真正的开销在解析）
        total = 0

        for root, _dirs, names in os.walk(path):

            for name in names:

                if service.scanner.accepts(
                    os.path.join(root, name)
                ):

                    total += 1

        with _SCAN_LOCK:

            _SCAN_JOB["total"] = total

        result = service.scan(
            path,
            persist=not dry_run,
        )

        rows = result["data"]

        for i, row in enumerate(rows, 1):

            with _SCAN_LOCK:

                _SCAN_JOB["done"] = i

                _SCAN_JOB["files"].append(
                    {
                        "file": os.path.basename(row["file"]),
                        "numbers": [
                            {
                                "number": n["number"],
                                "confidence": n["confidence"],
                            }
                            for n in row["numbers"][:3]
                        ],
                        "persisted": row["persisted"],
                    }
                )

                if row["persisted"]:

                    _SCAN_JOB["saved"] += 1

    except Exception as exc:                                   # noqa: BLE001

        with _SCAN_LOCK:

            _SCAN_JOB["error"] = f"{type(exc).__name__}: {exc}"

    finally:

        with _SCAN_LOCK:

            _SCAN_JOB["running"] = False

            _SCAN_JOB["finished"] = time.time()


@app.get(
    "/scan",
    response_class=HTMLResponse
)
def scan_page(request: Request):

    return templates.TemplateResponse(
        request,
        "scan.html",
        {
            "scan_paths": CONFIG.get("scan_paths") or [],
            "stats": library_stats(),
        }
    )


@app.post("/api/scan/start")
async def scan_start(request: Request):

    payload = await request.json()

    path = (payload.get("path") or "").strip()

    dry_run = bool(payload.get("dry_run"))

    if not path:

        return JSONResponse(
            {"ok": False, "error": "请填写要扫描的目录"},
            status_code=400,
        )

    if not os.path.isdir(path):

        return JSONResponse(
            {"ok": False, "error": f"目录不存在：{path}"},
            status_code=400,
        )

    with _SCAN_LOCK:

        if _SCAN_JOB["running"]:

            return JSONResponse(
                {"ok": False, "error": "已有扫描任务在跑，等它结束"},
                status_code=409,
            )

        _reset_job(path, dry_run)

    threading.Thread(
        target=_run_scan,
        args=(path, dry_run),
        daemon=True,
    ).start()

    return {"ok": True}


@app.get("/api/scan/status")
def scan_status():

    with _SCAN_LOCK:

        job = dict(_SCAN_JOB)

        job["files"] = list(_SCAN_JOB["files"])

    # 只回传最近 200 条明细，避免响应体随扫描规模无限增长
    job["files"] = job["files"][-200:]

    return job


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8811,
    )