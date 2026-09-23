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


# 内容截图单独挂一个前缀：模板用 /images/screenshots/<file>，
# 但截图和封面可能在**不同的根目录**（LMM_COVERS 只覆盖封面），
# 所以按 image_storage.screenshots 独立解析，缺省与封面同根。
SCREENSHOTS_DIR = resolve_path(
    os.environ.get("LMM_SCREENSHOTS")
    or (CONFIG.get("image_storage") or {}).get("screenshots")
    or "storage/images/screenshots"
)

os.makedirs(
    SCREENSHOTS_DIR,
    exist_ok=True
)


def screenshot_url(name):
    """截图文件名 -> 可访问 URL。

    两种情况：
      * 截图目录在 IMAGE_ROOT/screenshots 下 -> /images/screenshots/<name>
        （与模板里的路径一致）
      * 在别处 -> 单独挂 /shots/<name>
    """

    expected = os.path.join(
        IMAGE_ROOT,
        "screenshots"
    )

    if os.path.normcase(os.path.abspath(SCREENSHOTS_DIR)) == \
            os.path.normcase(os.path.abspath(expected)):

        return "/images/screenshots/{}".format(name)

    return "/shots/{}".format(name)


if os.path.normcase(os.path.abspath(SCREENSHOTS_DIR)) != \
        os.path.normcase(os.path.abspath(os.path.join(IMAGE_ROOT, "screenshots"))):

    app.mount(
        "/shots",
        StaticFiles(
            directory=SCREENSHOTS_DIR
        ),
        name="shots"
    )


# v2 主库；v1 库（"storage/library.db"）已冻结为迁移源，见 设计决策记录 D1
DB_PATH = resolve_path(
    ENV_DB
    or CONFIG.get("database")
)

# 种子保存目录（磁力文本档，附在卡片上的「存种子」按钮写这里）
TORRENT_DIR = resolve_path(
    os.environ.get("LMM_TORRENTS")
    or CONFIG.get("torrent_dir")
    or "storage/torrents"
)

os.makedirs(
    TORRENT_DIR,
    exist_ok=True
)


def build_file_service():
    """文件操作服务（打开/删除/存种子/空目录）。"""

    from core.database_v2 import Database

    from services.file_service import FileService

    return FileService(
        Database(DB_PATH),
        TORRENT_DIR
    )


def build_enrich_service():
    """元数据抓取服务（JavDB -> 元数据/磁力/封面/截图）。"""

    from core.database_v2 import Database

    from services.enrich_service import EnrichService

    return EnrichService(
        Database(DB_PATH),
        COVERS_DIR,
        SCREENSHOTS_DIR,
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


def screenshot_files(raw):
    """screenshots 字段 -> 文件名列表。

    库里可能存两种形态：
      * JSON 数组（完整路径或文件名）—— 新写入的
      * 单个路径字符串 —— 兼容手写
    统一取 basename，供模板拼 /images/screenshots/<file>。
    """

    if not raw:

        return []

    text = str(raw).strip()

    items = []

    if text.startswith("["):

        try:

            parsed = json.loads(text)

            items = parsed if isinstance(parsed, list) else [parsed]

        except Exception:

            items = []

    elif text:

        items = [text]

    out = []

    for item in items:

        if not item:

            continue

        name = os.path.basename(
            str(item).replace("\\", "/")
        )

        if name and name not in out:

            out.append(name)

    return out


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


def rows_to_videos(rows, deletable=None):
    """统一把查询结果加工成模板需要的形状。

    deletable: 可删番号集合（消费 deletable_titles）。为 None 时卡片上
    删除按钮一律禁用 —— 宁可少给按钮，不给一个会被服务端拒绝的按钮。
    """

    deletable = deletable or set()

    out = []

    for r in rows:

        out.append(
            {
                "number": r["number"],
                "filename": r["filename"],
                "title": r["title"],
                "cover_file": cover_filename(r["cover_local"]),
                "deletable": r["number"] in deletable,
            }
        )

    return out


def deletable_numbers():
    """可删番号集合。库不存在/无表时返回空集，不让首页 500。"""

    try:

        return set(build_file_service().deletable_index())

    except Exception:                                           # noqa: BLE001

        return set()


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

    shots = query(
        """
        SELECT COUNT(*) AS c FROM metadata
        WHERE screenshots IS NOT NULL AND screenshots != '[]'
        """
    )

    def one(rows):

        return rows[0]["c"] if rows else 0

    return {
        "titles": one(titles),
        "files": one(files),
        "size_h": human_size(one(size)),
        "magnets": one(magnets),
        "covered": one(covered),
        "shots": one(shots),
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
            "videos": rows_to_videos(rows, deletable_numbers()),
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
            "videos": rows_to_videos(rows, deletable_numbers()),
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
        metadata.tags,
        metadata.screenshots

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
        "screenshots": [
            {
                "name": n,
                "url": screenshot_url(n),
            }
            for n in screenshot_files(row["screenshots"])
        ],
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
    "enrich": False,
    "total": 0,
    "done": 0,
    "files": [],
    "saved": 0,
    "phase": "",
    "enrich_done": 0,
    "enrich_total": 0,
    "unrecognized": [],
    "error": None,
    "started": None,
    "finished": None,
}


def _reset_job(path, dry_run, enrich=False):

    _SCAN_JOB.update(
        {
            "running": True,
            "path": path,
            "dry_run": dry_run,
            "enrich": enrich,
            "total": 0,
            "done": 0,
            "files": [],
            "saved": 0,
            "phase": "scan",
            "enrich_done": 0,
            "enrich_total": 0,
            "unrecognized": [],
            "error": None,
            "started": time.time(),
            "finished": None,
        }
    )


def _run_scan(path, dry_run, enrich=False):
    """后台线程体：扫描；enrich=True 时紧接着抓元数据/磁力/封面截图。"""

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

        # ── 抓取阶段（仅在显式要求时执行；绝不自动触发）──
        if enrich and not dry_run:

            numbers = []

            for row in rows:

                if not row["numbers"]:

                    with _SCAN_LOCK:

                        _SCAN_JOB["unrecognized"].append(
                            os.path.basename(row["file"])
                        )

                for n in row["numbers"][:1]:

                    if n["number"] not in numbers:

                        numbers.append(n["number"])

            with _SCAN_LOCK:

                _SCAN_JOB["phase"] = "enrich"

                _SCAN_JOB["enrich_total"] = len(numbers)

            from services.enrich_service import EnrichService

            enricher = EnrichService(
                db,
                COVERS_DIR,
                SCREENSHOTS_DIR,
            )

            for i, num in enumerate(numbers, 1):

                try:

                    res = enricher.enrich(num, want="all")

                except Exception as exc:                        # noqa: BLE001

                    res = {"error": f"{type(exc).__name__}: {exc}"}

                with _SCAN_LOCK:

                    _SCAN_JOB["enrich_done"] = i

                    for item in _SCAN_JOB["files"]:

                        for n in item["numbers"]:

                            if n["number"] == num:

                                n["enriched"] = not res.get("error")

                                n["magnet_count"] = res.get("magnets", 0)

                                if res.get("error"):

                                    n["enrich_error"] = res["error"]

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

    enrich = bool(payload.get("enrich"))

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

        _reset_job(path, dry_run, enrich)

    threading.Thread(
        target=_run_scan,
        args=(path, dry_run, enrich),
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


@app.get(
    "/cleanup",
    response_class=HTMLResponse
)
def cleanup_page(request: Request):

    return templates.TemplateResponse(
        request,
        "cleanup.html",
        {
            "stats": library_stats(),
        }
    )


# ── 卡片操作（打开位置 / 删除 / 存种子）──────────────────────


@app.post("/api/open/{number}")
def api_open(number: str):
    """在资源管理器里定位该番号的本地文件。"""

    svc = build_file_service()

    paths = svc.files_of(number)

    if not paths:

        return JSONResponse(
            {"ok": False, "error": "库里没有该番号的文件记录"},
            status_code=404,
        )

    ok, msg = svc.open_in_explorer(paths[0])

    return {"ok": ok, "message": msg, "path": paths[0]}


@app.post("/api/delete/{number}")
def api_delete(number: str):
    """把该番号的本地文件送回收站。

    **只消费 deletable_titles()**（有 verified=1 磁力）—— 没磁力一律拒绝，
    这是 README 写死的门控契约，不做例外。
    """

    svc = build_file_service()

    ok, msg, detail = svc.delete_title(number)

    return JSONResponse(
        {"ok": ok, "message": msg, "detail": detail},
        status_code=200 if ok else 403,
    )


@app.post("/api/torrent/{number}")
def api_torrent(number: str):
    """把该番号的磁力存成文本档到项目目录。"""

    svc = build_file_service()

    ok, msg, path = svc.save_torrent(number)

    return JSONResponse(
        {"ok": ok, "message": msg, "path": path},
        status_code=200 if ok else 404,
    )


@app.post("/api/torrents/export-all")
def api_torrents_export_all():
    """把库里所有有磁力的番号各导出一份到项目目录。"""

    svc = build_file_service()

    ok, fail, directory = svc.save_all_torrents()

    return {
        "ok": True,
        "message": f"已导出 {ok} 个番号的磁力到 {directory}"
                   + (f"（{fail} 个失败）" if fail else ""),
        "saved": ok,
        "failed": fail,
        "dir": directory,
    }


@app.post("/api/enrich/{number}")
def api_enrich(number: str):
    """抓取单个番号的元数据 / 磁力 / 封面 / 截图。"""

    svc = build_enrich_service()

    try:

        res = svc.enrich(number, want="all")

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    return {
        "ok": not res.get("error"),
        "result": res,
    }


@app.post("/api/enrich-all")
def api_enrich_all(request: Request):
    """批量抓取库里**缺元数据**的番号（后台跑）。"""

    with _SCAN_LOCK:

        if _SCAN_JOB["running"]:

            return JSONResponse(
                {"ok": False, "error": "已有任务在跑，等它结束"},
                status_code=409,
            )

    rows = query(
        """
        SELECT titles.number AS number
        FROM titles
        LEFT JOIN metadata ON metadata.title_id = titles.id
        WHERE metadata.title IS NULL OR metadata.cover_local IS NULL
        ORDER BY titles.number
        """
    )

    numbers = [r["number"] for r in rows]

    if not numbers:

        return {"ok": True, "message": "没有缺元数据的番号", "count": 0}

    with _SCAN_LOCK:

        _reset_job("(批量抓取)", False, True)

        _SCAN_JOB["enrich_total"] = len(numbers)

        _SCAN_JOB["phase"] = "enrich"

    def _run():

        try:

            from core.database_v2 import Database

            from services.enrich_service import EnrichService

            enricher = EnrichService(
                Database(DB_PATH),
                COVERS_DIR,
                SCREENSHOTS_DIR,
            )

            for i, num in enumerate(numbers, 1):

                try:

                    enricher.enrich(num, want="all")

                except Exception:                               # noqa: BLE001
                    pass

                with _SCAN_LOCK:

                    _SCAN_JOB["enrich_done"] = i

        except Exception as exc:                                # noqa: BLE001

            with _SCAN_LOCK:

                _SCAN_JOB["error"] = f"{type(exc).__name__}: {exc}"

        finally:

            with _SCAN_LOCK:

                _SCAN_JOB["running"] = False

                _SCAN_JOB["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()

    return {"ok": True, "count": len(numbers)}


# ── 空目录扫描 / 批量删除 ───────────────────────────────────


@app.get("/api/empty-dirs")
def api_empty_dirs(roots: str = "", allow_drive_root: int = 0):
    """扫出空目录候选（只读，不删）。

    ⚠️ **必须显式给 roots**，且默认拒绝整盘根目录：
    默认拿 config.scan_paths 会直接全盘遍历（实测 config 里是 `X:\\` / `X:\\`，
    其中 E 盘有重映射告警，全盘 walk 会长时间卡住并加重病盘负担）。
    真要扫整盘得显式传 allow_drive_root=1。
    """

    if not roots:

        return JSONResponse(
            {
                "ok": False,
                "error": "请指定要扫描的目录（roots 参数）。"
                         "为避免误扫整盘，这里不会默认使用 config.yaml 的 scan_paths。",
                "configured": CONFIG.get("scan_paths") or [],
            },
            status_code=400,
        )

    target_roots = [r.strip() for r in roots.split("|") if r.strip()]

    unsafe = [
        r for r in target_roots
        if os.path.normcase(os.path.abspath(r)).count(os.sep) <= 1
    ]

    if unsafe and not allow_drive_root:

        return JSONResponse(
            {
                "ok": False,
                "error": f"拒绝直接扫描盘符根目录：{unsafe}。"
                         f"盘根通常是整个硬盘，遍历代价极大且可能触碰坏道。"
                         f"请给具体子目录，或显式传 allow_drive_root=1。",
            },
            status_code=400,
        )

    svc = build_file_service()

    found = svc.scan_empty_dirs(target_roots)

    return {
        "ok": True,
        "roots": target_roots,
        "count": len(found),
        "dirs": found[:500],
    }


@app.post("/api/empty-dirs/delete")
async def api_empty_dirs_delete(request: Request):
    """把指定的空目录送回收站。"""

    payload = await request.json()

    dirs = payload.get("dirs") or []

    if not dirs:

        return JSONResponse(
            {"ok": False, "error": "没有要删除的目录"},
            status_code=400,
        )

    svc = build_file_service()

    ok_count, failed = svc.delete_empty_dirs(dirs)

    return {
        "ok": not failed,
        "deleted": ok_count,
        "failed": failed,
    }


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8811,
    )