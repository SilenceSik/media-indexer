"""Web UI：本地媒体库浏览界面。

路线图：
    /                       列表（支持 ?has_magnet=1 / ?missing_meta=1 筛选）
    /search?q=              按番号搜索
    /detail/<number>        单条详情（本地文件 + 磁力 + 删除门控状态）
    /scan                   扫描页：指定目录、实时进度、结果明细
    /api/scan/start         启动扫描任务
    /api/scan/status        轮询任务进度

数据源是 v2 主库（config.yaml 的 database）。关联一律走 titles.id，
禁止用 metadata.number。
"""

import json
import os
import sqlite3
import sys
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

# 磁力列表默认展开多少条。超出的先折叠，点「展开全部」再看 ——
# 实测 ABP-171 有 76 条磁力，全铺开整页都是链接。
MAGNET_FOLD = 20

# 评论默认显示几条（主人 2026-09-24 定：默认 3 条，其余折叠）。
# 实测有的评论是千字长文，三条就已经占掉大半屏。
COMMENT_FOLD = 3

# 让 `from core import ...` / `from services import ...` 与调用者的 cwd 无关。
#
# 本模块里有若干**函数内延迟 import**（避开循环依赖），它们靠 sys.path 找
# 顶层包。`python web/app.py` 直接运行时，脚本目录是 web/，项目根不在
# sys.path 上，于是首页 500：ModuleNotFoundError: No module named 'core'。
# 从别处 `python -m web.app` 跑又恰好没事 —— 这类"看启动方式决定生死"的
# bug 最难查，所以在模块加载时就把根目录钉死。
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


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
#
# ⚠️ 必须带 Cache-Control，否则**每次切页都要回问服务器一遍**。
# 实测：默认只有 ETag，浏览器对首页 296 张卡片全部发条件请求
# -> 切标签明显卡顿。图片是内容寻址（文件名是内容哈希），内容不会变，
# 所以可以放心给长缓存。
class CachedStaticFiles(StaticFiles):
    """给静态资源加上 Cache-Control。

    为什么必须加：Starlette 的 StaticFiles 默认**不发 Cache-Control**，
    浏览器于是每次都要拿 ETag 回问一遍。首页有近 300 张卡片缩略图，
    切页时就是近 300 个条件请求 —— 表现就是「切标签特别卡」。

    图片文件名是内容哈希（如 `1921b0c12819bee6efe1f6213762f4b8.jpg`），
    内容变了文件名就变，所以长缓存是安全的。
    """

    def __init__(self, *args, max_age=604800, **kwargs):
        self.max_age = max_age
        super().__init__(*args, **kwargs)

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)

        if self.max_age > 0:
            resp.headers["Cache-Control"] = "public, max-age={}".format(
                self.max_age)
        else:
            # 允许存，但每次必须回源校验（配 ETag -> 没变就 304）
            resp.headers["Cache-Control"] = "no-cache"

        return resp


app.mount(
    "/static",
    # ⚠️ 这里**不能**用长缓存。
    #
    # 图片的文件名是内容哈希（内容变则文件名变），长缓存安全；
    # 但 style.css / common.js 的文件名是**固定的** —— 长缓存会让
    # 部署新样式后用户 7 天看不到变化（我自己先踩了这一步）。
    #
    # 用 no-cache 的语义是「可以存，但每次要先问一下」：仍然带 ETag，
    # 没变就回 304（无响应体），只多一个极轻的往返，换来「改完立刻生效」。
    CachedStaticFiles(
        directory=os.path.join(BASE_DIR, "web/static"),
        max_age=0,
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
    CachedStaticFiles(
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
        CachedStaticFiles(
            directory=SCREENSHOTS_DIR
        ),
        name="shots"
    )


# 宣传视频。`<video>` 要的是**普通视频文件**，而 javdb 给的是 HLS 的
# `.m3u8`（浏览器原生不认，点了只会下载）—— 所以要先把流转成 mp4 落盘。
# 见 `EnrichService.save_preview_video`。
#
# 与封面/截图不同，这里落盘的是**番号子目录下的定名文件**，所以直接按
# 目录挂 /previews/<番号>/preview.mp4 即可。
PREVIEWS_DIR = os.path.abspath(
    os.environ.get("LMM_PREVIEWS")
    or (CONFIG.get("image_storage") or {}).get("previews")
    or "storage/previews"
)

os.makedirs(
    PREVIEWS_DIR,
    exist_ok=True
)

app.mount(
    "/previews",
    # 宣传视频**是可重新录制的**（重录后同名文件内容会变），
    # 所以给短缓存（5 分钟）而不是图片那种长缓存 —— 否则重录完
    # 用户会看到旧视频，还以为没录成功。
    CachedStaticFiles(
        directory=PREVIEWS_DIR,
        max_age=300,
    ),
    name="previews"
)


# v2 主库；v1 库（"storage/library.db"）已冻结为迁移源，
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

# 导出包落盘目录
EXPORT_DIR = resolve_path(
    os.environ.get("LMM_EXPORTS")
    or CONFIG.get("export_dir")
    or "storage/exports"
)

os.makedirs(
    EXPORT_DIR,
    exist_ok=True
)


def ensure_schema(path=None):
    """把库迁到最新结构（补列 / 建表）。同一个库只跑一次。

    ⚠️ 这一步**必须**有：`migrate()` 只在 `Database()` 构造时跑，而本模块
    大量用裸 `query()` 直连 —— 少了它，新加的列（`favorite`、
    `metadata.preview_video`）在老库上不存在，页面直接
    `sqlite3.OperationalError: no such column`。

    ALTER TABLE ADD COLUMN 系列都是幂等的（内部先查 PRAGMA），但重复开
    连接也有成本，所以按路径记一次。测试里会改 `DB_PATH`，因此这里接参数
    而不是只认全局 —— 否则测试用的临时库永远等不到迁移。
    """

    target = path or DB_PATH

    if not target:

        return False

    if target in _MIGRATED:

        return True

    try:

        from core.database_v2 import Database

        Database(target).conn.close()

        _MIGRATED.add(target)

        return True

    except Exception as exc:                                    # noqa: BLE001

        # 迁移失败不该让整个 web 服务起不来 —— 页面会各自报缺列，
        # 但至少「哪里坏了」是可见的。
        print("[lmm] 库迁移失败（{}）：{}: {}".format(
            target, type(exc).__name__, exc))

        return False


_MIGRATED = set()

ensure_schema()


def load_rules():
    """读番号规则（低分清理要重放识别分数）。"""

    try:

        return json.load(
            open(resolve_path(CONFIG["dictionary"]), encoding="utf-8")
        )["rules"]

    except Exception:                                           # noqa: BLE001

        return []


def build_file_service():
    """文件操作服务（打开/删除/存种子/空目录/低分清理）。"""

    from core.database_v2 import Database

    from services.file_service import FileService

    return FileService(
        Database(DB_PATH),
        TORRENT_DIR,
        rules=load_rules(),
    )


def preview_url(number):
    """本地宣传视频的可访问地址。没落盘则空串。

    只在**文件真在**时才给地址 —— 否则模板会渲染一个 404 的 `<video>`，
    看起来像坏了，而不是「还没抓」。
    """

    path = os.path.join(PREVIEWS_DIR, number, "preview.mp4")

    if not os.path.exists(path) or os.path.getsize(path) == 0:

        return ""

    return "/previews/{}/preview.mp4".format(number)


def build_backup_service():
    """备份服务（导出/导入压缩包）。"""

    from core.database_v2 import Database                     # noqa: F401

    from services.backup_service import BackupService

    return BackupService(
        DB_PATH,
        COVERS_DIR,
        SCREENSHOTS_DIR,
        TORRENT_DIR,
        EXPORT_DIR,
    )


def _fallback_on():
    """本次是否启用 JavBus 兜底。"""

    from core.fallback_config import fallback_enabled

    try:

        return fallback_enabled(CONFIG)

    except Exception:                                           # noqa: BLE001

        return False


def build_enrich_service(fallback=None, db=None):
    """元数据抓取服务（JavDB -> 元数据/磁力/封面/截图/宣传视频）。

    ``fallback`` 控制是否挂 JavBus 兜底：
      * None  -> 读 config.yaml 的 `fallback`
      * True  -> 强制启用（WebUI 上逐次勾选时用）
      * False -> 强制关闭

    ``db`` 给了就用它，**不给才开默认库** ——
    ⚠️ 别写成 `db or Database(DB_PATH)`：测试会传临时库，
    而真值判断在对象上是隐式的，用 `is None` 才无歧义。

    兜底只在主源查不到时走，且服务**按需启停**（见 services/javbus_session）。
    """

    from core.database_v2 import Database

    from core.fallback_config import fallback_enabled

    from services.enrich_service import EnrichService

    if fallback is None:
        try:
            fallback = fallback_enabled(load_config())
        except Exception:                                       # noqa: BLE001
            fallback = False

    fb_client = None

    if fallback:

        from adapters.javbus_adapter import JavBusClient

        fb_client = JavBusClient()

    return EnrichService(
        db if db is not None else Database(DB_PATH),
        COVERS_DIR,
        SCREENSHOTS_DIR,
        fallback_client=fb_client,
        previews_dir=PREVIEWS_DIR,
    )


def query(sql, args=()):
    """只读查询。库不存在时返回空结果而不是 500（首次使用还没扫过）。

    连接参数与 core.database_v2 对齐：WAL + busy_timeout。
    不设的话，后台抓取在写、前台在读时会互相排队 —— 实测表现为
    "点补抓之后页面非常卡"（默认 delete 模式读写互斥）。
    """

    if not DB_PATH or not os.path.exists(DB_PATH):

        return []

    # 直连前确保结构最新（按路径记忆，只跑一次）。
    ensure_schema()

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10.0,
    )

    conn.row_factory = sqlite3.Row

    try:

        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("PRAGMA busy_timeout=10000")

    except sqlite3.DatabaseError:

        pass

    try:

        cur = conn.execute(
            sql,
            args
        )

        return cur.fetchall()

    finally:

        conn.close()


def execute_write(sql, args=()):
    """写库（单条语句）。与 query 用同一套连接参数。

    web 层此前只有只读 query，写操作都藏在 services 里。收藏是纯展示位的
    单列翻转，不值得为它单开一个 service，就地写。
    """

    if not DB_PATH or not os.path.exists(DB_PATH):

        raise FileNotFoundError("媒体库还不存在")

    ensure_schema()

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10.0,
    )

    try:

        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("PRAGMA busy_timeout=10000")

        cur = conn.execute(
            sql,
            args
        )

        conn.commit()

        return cur.rowcount

    finally:

        conn.close()


def score_breakdown(correct, comments, matches, source, ratios,
                    mismatch=False):
    """把置信分拆成「看得懂的一项一项」，给详情页展示。

    为什么要有这个：卡片上只有一个数字（比如 ABF-087 的 38），看不出
    为什么低 —— 主人 09-24 正是这么问的。这个函数把 `score()` 的内部
    构成摊开成条目：每项扣了多少、当前值多少。

    返回 `{score, base, consistency, duration_known, mismatch, items:[...],
            weights:{...}}`，`items` 里每项：
        {key, label, weight, value, points, note}
    `value` 是该项得分（0~1），`points` 是它实际贡献的分（base 内）。
    """

    from core import confidence as C
    from core.magnet_judge import TRUSTED_MATCH_SOURCES

    if matches is None:
        matches = True

    items = []

    # ── 服务器侧四项（构成 base，共 100 分）──
    mf = C.magnet_factor(correct)
    items.append({
        "key": "magnets",
        "label": "磁力条数",
        "weight": C.W_MAGNETS,
        "value": mf,
        "points": round(C.W_MAGNETS * mf * 100, 1),
        "note": "{} 条（到 {} 条封顶，对数式）".format(
            int(correct or 0), C.MAGNETS_SATURATION),
    })

    items.append({
        "key": "match",
        "label": "番号核对",
        "weight": C.W_MATCH,
        "value": 1.0 if matches else 0.0,
        "points": round(C.W_MATCH * (1.0 if matches else 0.0) * 100, 1),
        "note": "javdb 返回的番号与识别出的一致" if matches
                else "不一致 —— 整个页面可能都属于另一部片",
    })

    sf = C.source_factor(source)
    items.append({
        "key": "source",
        "label": "识别形态",
        "weight": C.W_SOURCE,
        "value": sf,
        "points": round(C.W_SOURCE * sf * 100, 1),
        "note": "{}（可信形态：{}）".format(
            source or "未记录",
            "、".join(sorted(TRUSTED_MATCH_SOURCES))),
    })

    cf = C.comment_factor(comments)
    items.append({
        "key": "comments",
        "label": "评论数",
        "weight": C.W_COMMENTS,
        "value": cf,
        "points": round(C.W_COMMENTS * cf * 100, 1),
        "note": "{} 条（到 {} 条封顶）".format(
            comments if comments is not None else "未知",
            C.COMMENTS_SATURATION),
    })

    base = C.base_score(correct, comments, matches, source)

    # ── 时长（乘性因子，不参与 base 求和）──
    cons = C.consistency_from_ratios(ratios)

    if mismatch:

        duration = {
            "known": True,
            "value": 0.0,
            "note": "本地文件与元数据**不是同一部片** —— 直接归零",
        }

    elif cons is None:

        duration = {
            "known": False,
            "value": C.CONSISTENCY_UNKNOWN,
            "note": "还没跑过时长核对 —— 本项**不扣分**（不是满分）",
        }

    else:

        floored = max(C.CONSISTENCY_FLOOR, min(1.0, cons))

        note = "吻合度 {:.0f}%".format(cons * 100)

        if floored != cons:

            note += "；已按兜底抬到 {:.0f}%（防探测偶发误读清零）".format(
                C.CONSISTENCY_FLOOR * 100)

        duration = {"known": True, "value": floored, "note": note}

    out = C.score(correct=correct, comments=comments, matches=matches,
                  source=source, ratios=ratios, mismatch=mismatch)

    # ── 门控加成（2026-09-26）──
    #
    # 单独成行展示，因为它是**唯一一项取决于档位**的加分：
    # 进「高」/「极高」才给，用来把热门资源的分抬起来、
    # 同时让冷门资源的分数天然落在它们之下（见 core/confidence 文件头）。
    gate = {
        "tier": out.get("gate_tier"),
        "bonus": out.get("gate_bonus", 0),
        "gated": out.get("base_gated", out["base"]),
        "note": (
            "档位「{}」—— 进批量删门槛，加 {} 分".format(
                out.get("gate_tier"), out.get("gate_bonus", 0))
            if out.get("gate_bonus") else
            "档位「{}」—— 不进批量删门槛，不加分（这就是热门与冷门的分数差）".format(
                out.get("gate_tier"))
        ),
        "rule": (
            "极高 = 磁力 > {} 且 评论 > {}；高 = 磁力 > {}".format(
                C.MAGNETS_FOR_TOP, C.COMMENTS_FOR_TOP, C.MAGNETS_FOR_HIGH)
        ),
    }

    return {
        "score": out["score"],
        "base": base,
        "consistency": out["consistency"],
        "duration_known": out["duration_known"],
        "mismatch": out["mismatch"],
        # ⚠️ 键名不能叫 `items` —— Jinja 里 `bd.items` 会解析成
        # dict 的 `.items()` 方法，模板 for 循环直接 TypeError。
        "parts": items,
        "duration": duration,
        "gate": gate,
        "weights": {
            "magnets": C.W_MAGNETS,
            "match": C.W_MATCH,
            "source": C.W_SOURCE,
            "comments": C.W_COMMENTS,
        },
        "constants": {
            "magnets_saturation": C.MAGNETS_SATURATION,
            "comments_saturation": C.COMMENTS_SATURATION,
            "floor": C.CONSISTENCY_FLOOR,
        },
    }


def human_size(n):
    """字节 -> 人类可读。"""

    n = n or 0

    for unit in ("B", "KB", "MB", "GB", "TB"):

        if n < 1024 or unit == "TB":

            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"

        n /= 1024.0


def cover_filename(cover_local):
    """封面路径 -> /images/covers/ 下的相对 URL 路径。

    库里可能存两种形态（都要支持）：
      * `ABP-041/image-002.jpg`  —— 服务抓取时写的新形态（带番号子目录）
      * `ABP-041-image-002.jpg`  —— 早期扁平布局（番号做前缀）

    绝不能只取 basename：落盘结构里各番号的封面**文件名完全一样**
    （都叫 image-002.jpg），只取 basename 会让全站撞成同一张图。
    """

    if not cover_local:

        return None

    text = str(cover_local).replace("\\", "/").lstrip("/")

    # 防目录穿越：去掉 .. 段
    parts = [p for p in text.split("/") if p and p != ".."]

    if not parts:

        return None

    return "/".join(parts)


# ── 原生文件夹选择框 ────────────────────────────────────────
#
# 用 tkinter 的 askdirectory 弹系统原生选择框。**必须在独立线程里跑**：
# 在已有事件循环内直接调 Tk 会与 uvicorn 的事件循环冲突。
#
# ⚠️ 不能用 functools.partial(web_app._pick_folder) 这种写法 —— 直接跑
# `python web/app.py` 时 uvicorn 会把模块加载成 `__main__`，闭包引用的是
# `web.app` 那份副本，写进去的全局变量不是请求线程看到的那份，端点的
# 轮询会永远超时。所以这里只用普通函数引用 + 模块级可变状态。

_PICK_LOCK = threading.Lock()

_PICK_STATE = {
    "busy": False,
    "path": None,
    "error": None,
}


def _pick_folder(initial=""):
    """弹系统文件夹选择框（阻塞直到用户选完或取消）。"""

    import tkinter as tk

    from tkinter import filedialog

    root = tk.Tk()

    root.withdraw()

    root.attributes("-topmost", True)

    try:

        path = filedialog.askdirectory(
            title="选择文件夹",
            initialdir=initial or None,
            mustexist=True,
        )

    finally:

        root.destroy()

    return path or ""


@app.post("/api/pick-folder")
def api_pick_folder():
    """打开系统文件夹选择框，返回用户选的路径（取消则返回空）。

    ⚠️ 必须是**同步** def，不能是 async def —— 这个端点会阻塞到用户选完
    （最长 5 分钟）。async def 跑在事件循环上，阻塞它会把整个服务冻住，
    连前端轮询都收不到响应。同步 def 则由 FastAPI 放进线程池，不挡别人。
    """

    with _PICK_LOCK:

        if _PICK_STATE["busy"]:

            return JSONResponse(
                {"ok": False, "error": "已经有一个选择框开着"},
                status_code=409,
            )

        _PICK_STATE["busy"] = True

        _PICK_STATE["path"] = None

        _PICK_STATE["error"] = None

    try:

        path = _pick_folder()

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    finally:

        with _PICK_LOCK:

            _PICK_STATE["busy"] = False

    return {"ok": True, "path": path or ""}


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

        text = str(item).replace("\\", "/").lstrip("/")

        # 与封面同理：保留番号子目录层级，只取 basename 会全站撞名
        parts = [p for p in text.split("/") if p and p != ".."]

        if not parts:

            continue

        name = "/".join(parts)

        if name not in out:

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
#
# D9/D11 的展示字段（档位 / 正确磁力数 / 评论数）一并查出来 ——
# 卡片上要显示，前端不各自再查一遍。
LIST_SQL = """
        SELECT
        titles.number,
        titles.tier,
        titles.correct_magnets,
        titles.comments_count,
        titles.evidence_strong,
        titles.number_matches,
        titles.favorite,
        media_files.filename,
        media_files.match_source,
        media_files.match_confidence,
        media_files.local_deleted,
        metadata.title,
        metadata.cover_local

        FROM titles

        LEFT JOIN media_files

        ON media_files.title_id = titles.id

        LEFT JOIN metadata

        ON metadata.title_id =
        titles.id
"""


def best_match_source(rows):
    """一组 media_files 行 -> 最有代表性的**识别形态**。

    `confidence.source_factor` 量的是「这个番号是**怎么被认出来的**」
    （带分隔符的标准形态 vs 裸数字 vs 兜底），字段是
    `media_files.match_source`。

    ⚠️ 别用 `titles.lookup_source` —— 那是**元数据来源**（'javdb' / 'javbus'），
    语义完全不同，而且不在 `TRUSTED_MATCH_SOURCES` 里，喂进去只会恒得 0.3，
    让这 21% 的权重变成永远的死重。两处页面一度各喂各的，于是
    「首页 80 / 详情 95」自相矛盾（2026-09-24 修）。

    一个番号可能有多个文件，取 `match_confidence` 最高的那条当代表。
    """

    best = None

    best_conf = -1

    for r in rows or []:

        try:

            src = r["match_source"]

            conf = r["match_confidence"]

        except (KeyError, IndexError, TypeError):

            continue

        if not src:

            continue

        conf = conf if conf is not None else 0

        if conf > best_conf:

            best_conf = conf

            best = src

    return best


DEFAULT_WEIGHTS = {
    "magnets": 0.36,
    "match": 0.29,
    "source": 0.21,
    "comments": 0.14,
}

# 权重上下限。界面上的面板用**百分数**填（36 / 29 / 21 / 14），
# 所以上限是 100 而不是 1 —— 卡在 1 的话 36/29/21/14 会被全部夹成 1，
# 归一化后变成等权 0.25，等于「面板点了应用但配比没变」。
# （这个 bug 是测试抓出来的。）
WEIGHT_MIN = 0.0
WEIGHT_MAX = 100.0


def normalize_weights(w_magnets=None, w_match=None, w_source=None,
                      w_comments=None):
    """四项权重 -> 归一化到 1 的 dict。非法/缺失的项用默认值。

    归一化是**必须**的：用户可能填 2/2/2/2 或 36/29/21/14，
    不归一化前者会让分数突破 100。这里统一按「相对配比」理解。
    """

    raw = dict(DEFAULT_WEIGHTS)

    for key, val in (("magnets", w_magnets), ("match", w_match),
                     ("source", w_source), ("comments", w_comments)):

        if val is None:

            continue

        try:

            v = float(val)

        except (TypeError, ValueError):

            continue

        raw[key] = max(WEIGHT_MIN, min(WEIGHT_MAX, v))

    total = sum(raw.values())

    if total <= 0:

        # 全是 0 -> 退回默认，而不是把分数做成 0/0
        return dict(DEFAULT_WEIGHTS)

    return {k: v / total for k, v in raw.items()}


def rows_to_videos(rows, deletable=None, eligibility=None, durations=None,
                   weights=None):
    """统一把查询结果加工成模板需要的形状。

    ⚠️ **一个番号一张卡**。`rows` 是 `titles LEFT JOIN media_files`，
    所以同一个番号可能带来多行（多文件）—— 这里按番号聚合。

    为什么必须聚合：
      * 卡是**这部片的档案**（番号/元数据/磁力/截图），文件只是它的副本。
        主人 2026-09-24 定了口径：「删源文件做磁盘管理，但卡要留着」。
      * 早前是**一个文件一张卡**（326 张卡 / 296 个番号），于是
        ABP-171 的两张卡分别显示 83 和 98 —— 同一部片两个分数，
        既容易误读，也让“保卡”无从谈起。
      * 删光文件的番号**也要出卡**（`local_deleted=1` 的行仍在），
        否则「删除文件保卡」就落空了。

    deletable: 可删番号集合（消费 deletable_titles）。为 None 时卡片上
    删除按钮一律禁用 —— 宁可少给按钮，不给一个会被服务端拒绝的按钮。

    eligibility: `deletion_eligibility()` 的结果，按番号索引。给了就用它
    决定按钮状态与分流原因（D9/D10/D11 三条件），比旧的 deletable 更准。

    durations: `duration_index()` 的结果，按番号索引。用于算置信分里的
    时长一致性因子；为 None 时置信分退化成「时长未验」而不是扣分。

    weights: 置信分的四项配比（`normalize_weights` 的产物）。不传用默认。
    """

    from core import confidence

    from core.duration_check import mismatch_verdict

    deletable = deletable or set()

    eligibility = eligibility or {}

    durations = durations or {}

    # ── 先按番号归组（同番号多文件 -> 一组）──
    grouped = {}

    order = []

    for r in rows:

        number = r["number"]

        if number not in grouped:

            grouped[number] = []

            order.append(number)

        grouped[number].append(r)

    out = []

    for number in order:

        group = grouped[number]

        head = group[0]

        e = eligibility.get(number) or {}

        # 优先用新门控的结论；没有则退回旧集合
        can_batch = bool(e.get("batch"))
        can_manual = bool(e.get("manual"))

        if not e:
            can_manual = number in deletable

        # ── 识别形态：取 match_confidence 最高的那条当代表 ──
        source = best_match_source(group)

        # ── 存活文件 ──
        alive = [g for g in group if not g["local_deleted"]]

        local_files = len(alive)
        local_deleted_all = bool(group) and local_files == 0

        # ── 置信分 ──
        # 取代旧的 evidence_strong 二值位：那个只数服务器侧磁力条数
        # （>=10 即「强证据」），不看本地文件 —— FH-27 就是靠 15 条磁力
        # 拿到「强证据」，而它的文件全是 9~28 分钟的短片。
        #
        # 时长一致性是**乘性因子**：磁力/评论/核对/来源四个维度都只证明
        # 「服务器上有这个号」，只有时长直接检验「本地这份就是这部片」。
        # ⚠️ 只看**存活**文件：已删的那些不该再影响分数。
        live_durations = [
            f for f in (durations.get(number) or [])
            if not f.get("local_deleted")
        ]

        ratios = confidence.file_ratios(live_durations)

        # 时长**完全不符**（另一部片）-> 扣到 0。
        #
        # 用与置信分同一套 `file_ratios`，不再单独查一遍库：口径必须
        # 一致，否则会出现「列表说 0 分、详情说 12 分」这种自相矛盾。
        # 番号写在文件名里 -> 是「内容没下全」，不是「认错片」。
        # 见 core/duration_check.number_in_filename（主人 09-24 提的分片情形）。
        from core.duration_check import number_in_filename as _nif

        # 番号在文件名里 **且** 多个文件（单片不足以证明是分片）
        partial_ok = len(alive) >= 2 and all(
            _nif(number, g["filename"] or "") for g in alive
        )

        verdict = mismatch_verdict(
            [
                (f.get("duration_local"), (f.get("duration_ref") or 0) * 60.0)
                for f in live_durations
            ],
            partial_ok=partial_ok,
        )

        conf = confidence.score(
            correct=head["correct_magnets"] if "correct_magnets" in head.keys() else None,
            comments=head["comments_count"] if "comments_count" in head.keys() else None,
            matches=(bool(head["number_matches"])
                     if "number_matches" in head.keys()
                     and head["number_matches"] is not None else None),
            source=source,
            ratios=ratios,
            mismatch=verdict["mismatch"],
            weights=weights,
        )

        out.append(
            {
                "number": number,
                # 文件名只在**还有存活文件**时才有意义；全删了显示空，
                # 由模板改显示「本地已删」。
                "filename": head["filename"] if alive else "",
                "title": head["title"],
                "cover_file": cover_filename(head["cover_local"]),
                "deletable": can_manual,

                # ── 本地文件状态（保卡的关键）──
                "local_files": local_files,
                "local_deleted_all": local_deleted_all,
                "local_deleted_count": len(group) - local_files,

                # ── D9/D11 展示字段 ──
                "tier": head["tier"] if "tier" in head.keys() else None,
                "correct_magnets": (
                    head["correct_magnets"] if "correct_magnets" in head.keys() else None
                ),
                "comments_count": (
                    head["comments_count"] if "comments_count" in head.keys() else None
                ),
                "number_matches": (
                    head["number_matches"] if "number_matches" in head.keys() else None
                ),
                "favorite": bool(
                    head["favorite"] if "favorite" in head.keys() else 0
                ),

                # ── 置信分（取代 evidence_strong 展示位）──
                "confidence": conf["score"],
                "confidence_base": conf["base"],
                # 门控加成（2026-09-26）：进档才有的加分，见 core/confidence
                "confidence_gated": conf.get("base_gated", conf["base"]),
                "confidence_gate_tier": conf.get("gate_tier"),
                "confidence_gate_bonus": conf.get("gate_bonus", 0),
                "confidence_duration": conf["consistency"],
                "duration_known": conf["duration_known"],
                # 时长完全不符：卡片上要显眼标出来 —— 这是「本地这批文件
                # 根本不是这个番号」，不是「分数偏低」那种程度的事。
                "duration_mismatch": conf["mismatch"],
                "duration_note": (
                    "每文件比例中位数 {:.2f}".format(verdict["median"])
                    if verdict.get("median") is not None else ""
                ),

                # 旧字段保留为派生值，兼容还没改过来的模板/脚本。
                "evidence_strong": bool(
                    head["evidence_strong"] if "evidence_strong" in head.keys() else 0
                ),

                "can_batch": can_batch,
                "can_manual": can_manual,
                "blocked_reason": e.get("blocked_reason") or "",
            }
        )

    return out


def deletable_numbers():
    """可删番号集合。库不存在/无表时返回空集，不让首页 500。"""

    try:

        return set(build_file_service().deletable_index())

    except Exception:                                           # noqa: BLE001

        return set()


def eligibility_index():
    """`deletion_eligibility()` 结果按番号索引。

    卡片用它决定删除按钮状态与分流原因（D9/D10/D11 三条件）。
    库不存在/表缺列时返回空 dict —— 首页降级成「未判定」，不 500。

    ⚠️ 这里必须**显式导入 Database**。此前漏了，而 `except Exception`
    把 `NameError` 一起吞掉 -> 本函数**永远返回空**，
    于是所有卡片的删除按钮状态一直静默退化（`can_batch` 恒 False、
    `blocked_reason` 恒空），首页「高置信一键送回收站」也永远不出现。
    教训：宽 except 会把「写错了」伪装成「没有数据」。
    """

    from core.database_v2 import Database

    try:

        db = Database(DB_PATH)

        return {e["number"]: e for e in db.deletion_eligibility()}

    except Exception:                                           # noqa: BLE001

        return {}


def duration_index():
    """每个番号的本地/元数据时长对，给置信分算一致性用。

    返回 `{number: [{"duration_local":…, "duration_ref":…}, …]}`。

    单独一条查询而不是塞进 `LIST_SQL`：一个番号可能有几十个文件，
     JOIN 进来会把列表行数放大数十倍 —— 首页只显示 500 行，
    乘上文件数会直接把内存吃满。

    库不存在/缺列时返回空 dict，置信分退化成「时长未验」，不 500。
    """

    try:

        rows = query(
            """
            SELECT titles.number AS number,
                   media_files.duration_local AS duration_local,
                   media_files.duration_ref AS duration_ref,
                   media_files.local_deleted AS local_deleted

            FROM media_files

            JOIN titles

            ON titles.id = media_files.title_id
            """
        )

    except Exception:                                           # noqa: BLE001

        return {}

    out = {}

    for r in rows:

        out.setdefault(r["number"], []).append({
            "duration_local": r["duration_local"],
            "duration_ref": r["duration_ref"],
            # 已删文件的时长不该再影响置信分（见 rows_to_videos）
            "local_deleted": r["local_deleted"],
        })

    return out


def tier_counts():
    """各档位计数，给首页概览用。

    ⚠️ 同 `eligibility_index`：漏导入会被宽 except 吞成空 dict。
    """

    from core.database_v2 import Database

    try:

        db = Database(DB_PATH)

        rows = db.conn.execute(
            "SELECT tier, COUNT(*) FROM titles GROUP BY tier"
        ).fetchall()

        return {r[0] or "未判定": r[1] for r in rows}

    except Exception:                                           # noqa: BLE001

        return {}


def library_stats():
    """首页四个数字。"""

    titles = query("SELECT COUNT(*) AS c FROM titles")
    # 文件数/占用只算**还在磁盘上的** —— 已送回收站的不该再计入，
    # 否则「删了文件但占用没变」会让人以为没删掉。
    files = query(
        "SELECT COUNT(*) AS c FROM media_files "
        "WHERE COALESCE(local_deleted, 0) = 0"
    )
    size = query(
        "SELECT COALESCE(SUM(size), 0) AS c FROM media_files "
        "WHERE COALESCE(local_deleted, 0) = 0"
    )
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


# ═══════════════════════ 卡片查询（首页与分页接口共用）

# 首屏一次渲染多少张卡片。
#
# 为什么要有这个数：主人 09-24 报「切标签特别卡」，查出来是**一次性渲染
# 几百张卡片**的固有成本（296 张纯布局 1.3s，CPU 4x 降速下）。
# content-visibility 只省了屏幕外元素的布局，HTML 与 DOM 仍是全量 ——
# 库里几千部时会彻底扛不住。所以改成按需追加。
#
# 24 张 = 桌面约 5 列 x 5 行，首屏铺满且略有富余。
CARDS_PAGE_SIZE = 24


def _cards_where(has_magnet=0, missing_meta=0, favorite=0, q=""):
    """拼筛选条件。**首页与分页接口共用** —— 口径必须一致，不能各写一套。"""

    if favorite:
        # 只看收藏。纯人工标记，与档位/磁力无关。
        return " WHERE titles.favorite = 1 ", ()

    if has_magnet:
        # 只显示有「已验证磁力」的番号（= 可删候选）
        return (
            " WHERE titles.id IN ("
            "   SELECT title_id FROM magnets WHERE verified = 1"
            " ) ", ())

    if missing_meta:
        return (
            " WHERE titles.id NOT IN ("
            "   SELECT title_id FROM metadata"
            "   WHERE title IS NOT NULL OR cover_local IS NOT NULL"
            " ) ", ())

    if q:
        return " WHERE titles.number LIKE ? ", ("%{}%".format(q),)

    return "", ()


def count_cards(has_magnet=0, missing_meta=0, favorite=0, q=""):
    """符合条件的**总数**（分页要知道还有多少）。"""

    where, args = _cards_where(has_magnet, missing_meta, favorite, q)

    row = query("SELECT COUNT(*) AS n FROM titles" + where, args)

    return int(row[0]["n"]) if row else 0


def fetch_cards(offset=0, limit=None, has_magnet=0, missing_meta=0,
                favorite=0, q="", weights=None):
    """取一页卡片（已转成模板要的形状）。

    ## ⚠️ 分页必须按**番号**，不能按文件行

    `LIST_SQL` 是 `titles LEFT JOIN media_files`，一个番号多文件时会有
    多行；而 `rows_to_videos()` 按番号聚合成**一张卡**（主人定的口径：
    一个番号一张卡）。所以若直接对 rows 做 LIMIT/OFFSET：

      * 每页实际出的卡片数少于 limit（被聚合掉的）
      * 页码漂移 —— 第 2 页从 offset=24 开始，但前 24 行只出了 23 张卡，
        于是**重复**一个番号，后面还会连环错位

    实测本库有 18 个番号是多文件的（326 行 / 296 番号），一定会踩到。

    所以走两步：先对 `titles` 分页取出本页的**番号**，再取这些番号的
    完整行交给 `rows_to_videos()`。
    """

    limit = int(limit if limit is not None else CARDS_PAGE_SIZE)

    where, args = _cards_where(has_magnet, missing_meta, favorite, q)

    # 第一步：本页的番号（按 titles 分页，与 count_cards 同一套条件）
    page = query(
        "SELECT titles.id AS tid, titles.number AS number FROM titles"
        + where
        + """
        ORDER BY COALESCE(titles.favorite, 0) DESC, titles.number
        LIMIT ? OFFSET ?
        """,
        tuple(args) + (limit, int(offset))
    )

    if not page:
        return []

    tids = [r["tid"] for r in page]

    # 第二步：取这些番号的完整行（用 IN，不用 LIMIT —— 保证不漏文件）
    placeholders = ",".join("?" for _ in tids)

    rows = query(
        LIST_SQL
        + " WHERE titles.id IN ({})".format(placeholders)
        + """
        ORDER BY COALESCE(titles.favorite, 0) DESC, titles.number
        """,
        tuple(tids)
    )

    return rows_to_videos(
        rows, deletable_numbers(), eligibility_index(), duration_index(),
        weights=weights,
    )


@app.get(
    "/api/cards",
    response_class=JSONResponse
)
def api_cards(
    offset: int = 0,
    limit: int = None,
    has_magnet: int = 0,
    missing_meta: int = 0,
    favorite: int = 0,
    q: str = "",
    w_magnets: float = None,
    w_match: float = None,
    w_source: float = None,
    w_comments: float = None
):
    """按需取卡片（无限滚动用）。

    服务端渲染好 HTML 片段返回 —— 卡片模板里有大量条件分支
    （档位 / 置信分 / 时长 / 删除门控……），在 JS 里重写一份必然两边走样。
    直接用同一份 `_card.html`，口径天然一致。
    """

    weights = normalize_weights(w_magnets, w_match, w_source, w_comments)

    limit = max(1, min(int(limit or CARDS_PAGE_SIZE), 200))

    videos = fetch_cards(
        offset=offset, limit=limit,
        has_magnet=has_magnet, missing_meta=missing_meta,
        favorite=favorite, q=q, weights=weights,
    )

    total = count_cards(has_magnet, missing_meta, favorite, q)

    html = templates.get_template("_card_list.html").render(videos=videos)

    return {
        "html": html,
        "count": len(videos),
        "offset": offset,
        "total": total,
        "has_more": (int(offset) + len(videos)) < total,
    }


@app.get(
    "/",
    response_class=HTMLResponse
)
def index(
    request: Request,
    has_magnet: int = 0,
    missing_meta: int = 0,
    favorite: int = 0,
    w_magnets: float = None,
    w_match: float = None,
    w_source: float = None,
    w_comments: float = None
):

    weights = normalize_weights(w_magnets, w_match, w_source, w_comments)

    # 首屏只渲染一页；其余随滚动按需取（见 /api/cards）
    videos = fetch_cards(
        offset=0, limit=CARDS_PAGE_SIZE,
        has_magnet=has_magnet, missing_meta=missing_meta,
        favorite=favorite, weights=weights,
    )

    total = count_cards(has_magnet, missing_meta, favorite)

    # 高置信可批量删的数量 —— 首页「一键送回收站」按钮用它决定显示与否。
    try:

        _elig = eligibility_index()

        batch_count = sum(1 for e in _elig.values() if e.get("batch"))

    except Exception:                                           # noqa: BLE001

        batch_count = 0

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": videos,
            "total": total,
            "page_size": CARDS_PAGE_SIZE,
            "stats": library_stats(),
            "q": "",
            "fav_only": bool(favorite),
            "has_magnet": has_magnet,
            "missing_meta": missing_meta,
            "favorite": favorite,
            "weights": weights,
            "weights_default": DEFAULT_WEIGHTS,
            "batch_count": batch_count,
            # 分页接口要用同样的权重（不然后续批次的分数会跟首屏不一致）
            "w_magnets": w_magnets if w_magnets is not None else "",
            "w_match": w_match if w_match is not None else "",
            "w_source": w_source if w_source is not None else "",
            "w_comments": w_comments if w_comments is not None else "",
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
    """按番号搜索。

    ⚠️ 这个路由此前**一直是 500** —— 它调
    `rows_to_videos(..., weights=weights)` 但 `weights` 从未定义
    （NameError）。搜一下才发现，属于既存 bug。

    顺手改成与其他页面一致：首屏一页 + 滚动按需取。
    """

    weights = normalize_weights()

    videos = fetch_cards(
        offset=0, limit=CARDS_PAGE_SIZE, q=q, weights=weights,
    )

    total = count_cards(q=q)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": videos,
            "total": total,
            "page_size": CARDS_PAGE_SIZE,
            "stats": library_stats(),
            "q": q,
            "fav_only": False,
            "has_magnet": 0,
            "missing_meta": 0,
            "favorite": 0,
            "weights": weights,
            "weights_default": DEFAULT_WEIGHTS,
            "batch_count": 0,
            "w_magnets": "",
            "w_match": "",
            "w_source": "",
            "w_comments": "",
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
        titles.tier,
        titles.correct_magnets,
        titles.comments_count,
        titles.number_matches,
        titles.lookup_source,
        metadata.title,
        metadata.cover_local,
        metadata.release_date,
        metadata.maker,
        metadata.actresses,
        metadata.tags,
        metadata.screenshots,
        metadata.preview_video

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

        # ⚠️ index.html 现在还要 total / page_size / weights 等上下文
        # （加了分页容器与配比面板）。少给一个就是 UndefinedError -> 500，
        # 于是「番号不存在」看起来像「服务器坏了」。**给齐**。
        _w = normalize_weights()

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "videos": [],
                "total": 0,
                "page_size": CARDS_PAGE_SIZE,
                "stats": library_stats(),
                "q": number,
                "fav_only": False,
                "has_magnet": 0,
                "missing_meta": 0,
                "favorite": 0,
                "weights": _w,
                "weights_default": DEFAULT_WEIGHTS,
                "batch_count": 0,
                "w_magnets": "",
                "w_match": "",
                "w_source": "",
                "w_comments": "",
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
          AND COALESCE(local_deleted, 0) = 0

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
        """,
        (
            title_id,
        )
    )

    # ⚠️ 不能交给 SQL 排：size_text 是文本，"818 MB" 按字典序排在 "5.65 GB"
    # 前面（'8' > '5'）。必须换算成字节再排。已验证的仍然排前面。
    from core.size_parse import sort_key as _size_key

    magnets = sorted(
        magnets,
        key=lambda m: (0 if m["verified"] else 1, _size_key(m["size_text"])),
    )

    total_size = sum(
        (f["size"] or 0) for f in files
    )

    # 评论（正文）。以前只存数量、正文看过就丢 —— 现在落库并展示。
    from core.database_v2 import Database as _DB

    try:

        _db = _DB(DB_PATH)

        comments = _db.comments_of(title_id)

    except Exception:                                           # noqa: BLE001

        comments = []

    # ── 置信分 ──
    #
    # 详情页原先只显示档位，不显示分数 —— 而分数是首页卡片上最主要的
    # 判读依据（含时长一致性），点进来看不到会让人以为「详情页没这个信息」。
    # **口径必须与首页一致**：同样走 confidence.score + mismatch_verdict，
    # 否则会出现「列表 0 分、详情 12 分」这种自相矛盾。
    from core import confidence as _confidence

    from core.duration_check import mismatch_verdict

    # ⚠️ 一次取**全部**行，再分两种用途：
    #   * 识别形态（source）：是**整部片**的属性，删了文件也不该变 ——
    #     首页用的是全部行，详情页若只取存活行，两边分数就会分叉
    #     （实测全删后首页 95 / 详情 80）。
    #   * 时长配对：只认**存活**文件（已删的不该再参与判定）。
    dur_rows = query(
        """
        SELECT duration_local, duration_ref, match_source, match_confidence,
               local_deleted
        FROM media_files
        WHERE title_id = ?
        """,
        (title_id,),
    )

    live_rows = [d for d in dur_rows if not d["local_deleted"]]

    _pairs = [
        (d["duration_local"], (d["duration_ref"] or 0) * 60.0)
        for d in live_rows
        if d["duration_local"] and d["duration_ref"]
    ]

    # 番号写在文件名里 -> 内容没下全，不是认错片（见 number_in_filename）
    from core.duration_check import number_in_filename as _nif

    _partial = len(live_rows) >= 2 and all(
        _nif(number, r["filename"] or "") for r in
        query("SELECT filename FROM media_files WHERE title_id = ? "
              "AND COALESCE(local_deleted, 0) = 0", (title_id,))
    )

    _verdict = mismatch_verdict(_pairs, partial_ok=_partial)

    conf = _confidence.score(
        correct=row["correct_magnets"] if "correct_magnets" in row.keys() else None,
        comments=row["comments_count"] if "comments_count" in row.keys() else None,
        matches=(bool(row["number_matches"])
                 if "number_matches" in row.keys()
                 and row["number_matches"] is not None else None),
        # 与首页同一个取值口径：media_files.match_source（识别形态，全部行），
        # 不是 titles.lookup_source（元数据来源）。两处共用 best_match_source。
        source=best_match_source(dur_rows),
        ratios=_confidence.file_ratios(
            [{"duration_local": d["duration_local"],
              "duration_ref": d["duration_ref"]} for d in live_rows]
        ),
        mismatch=_verdict["mismatch"],
    )

    # 摊开成一项一项，给详情页显示「分数为什么是这个数」
    bd = score_breakdown(
        correct=row["correct_magnets"] if "correct_magnets" in row.keys() else None,
        comments=row["comments_count"] if "comments_count" in row.keys() else None,
        matches=(bool(row["number_matches"])
                 if "number_matches" in row.keys()
                 and row["number_matches"] is not None else None),
        source=best_match_source(dur_rows),
        ratios=_confidence.file_ratios(
            [{"duration_local": d["duration_local"],
              "duration_ref": d["duration_ref"]} for d in live_rows]
        ),
        mismatch=_verdict["mismatch"],
    )

    # 删除门控：与首页卡片同一套结论（D9/D10/D11），避免详情页
    # 出现一个首页不给、服务端也会拒绝的删除按钮。
    e = eligibility_index().get(row["number"]) or {}

    can_batch = bool(e.get("batch"))
    can_manual = bool(e.get("manual"))

    if not e:
        can_manual = row["number"] in deletable_numbers()

    video = {
        "number": row["number"],
        "title": row["title"],
        "cover_file": cover_filename(row["cover_local"]),
        "can_batch": can_batch,
        "can_manual": can_manual,
        "blocked_reason": e.get("blocked_reason") or "",
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
        "comments": comments,

        # 宣传视频：库里存的是 javdb 的 m3u8 签名链接（会过期）。
        # 模板要的是**能直接播的本地 mp4** —— `preview_play` 只在文件
        # 真落盘时才非空（见 preview_url），免得渲染出一个 404 的 <video>。
        "preview_video": row["preview_video"] or "",
        "preview_play": preview_url(row["number"]),

        # ── 置信分（与首页同一套口径）──
        "confidence": conf["score"],
        "breakdown": bd,
        "confidence_base": conf["base"],
        "confidence_duration": conf["consistency"],
        "duration_known": conf["duration_known"],
        "duration_mismatch": conf["mismatch"],
        "duration_note": (
            "每文件比例中位数 {:.2f}".format(_verdict["median"])
            if _verdict.get("median") is not None else ""
        ),
    }

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "video": video,
            "stats": library_stats(),
            # 磁力超过这个条数就先折叠。实测 ABP-171 有 76 条，
            # 全铺开整个页面就是一张链接墙。
            "MAGNET_FOLD": MAGNET_FOLD,
            # 评论默认显示几条（主人 09-24 定：默认 3 条，其余折叠）。
            "COMMENT_FOLD": COMMENT_FOLD,
        }
    )


# ── 扫描任务（内存态，本地单用户够用）─────────────────────
#
# 设计取舍：不引入任务队列/Redis —— 这是本机单人工具，一次只跑一个扫描任务，
# 用进程内 dict + 后台线程就够。进程重启丢任务状态是可接受的（扫描本身幂等，
# 重新发起即可）。
#
# 暂停/停止用**协作式标志**，不用 Thread.kill（Python 没有安全的强杀）：
# 后台循环在每个条目边界检查标志位，收到就干净收尾。

_SCAN_LOCK = threading.Lock()

# 协作式控制标志
_SCAN_STOP = threading.Event()

_SCAN_PAUSE = threading.Event()

# 每个键的"归零值"。初始状态与 _reset_job 都走这里，避免新增字段时漏改
# 一处 —— 之前初始字典和 _reset_job 各写一份，结果 `enrich_errors` 只进了
# 后者、从不重置（上一轮失败列表残留到下一轮），而初始字典又缺 `paused`
# 这类新键（服务刚启动时 /api/scan/status 直接 KeyError）。
#
# 返回字面量而不是模块级常量：每次调用都是全新的 list/dict，天然深拷贝。
# （用 dict(CONST) 浅拷贝会让默认表和运行时状态共用同一个 list，
#   worker append 就污染了默认值，下一轮 reset 拿到脏数据 —— 等于没修。）
def _job_defaults():
    return {
        "running": False,
        "path": "",
        # 多目录：本次要扫的目录列表 + 当前正在扫哪个
        "paths": [],
        "current_path": "",
        "dry_run": False,
        "enrich": False,
        "total": 0,
        "done": 0,
        "files": [],
        "saved": 0,
        "phase": "",
        "enrich_done": 0,
        "enrich_total": 0,
        "enriched": 0,
        "enrich_failed": 0,
        "enrich_ambiguous": 0,
        "enrich_errors": [],
        "min_conf": 0,
        "max_conf": 100,
        "min_size": 0,
        "max_size": 0,
        "skipped_count": 0,
        "skipped_samples": [],
        "unrecognized": [],
        "paused": False,
        "stopped": False,
        "export_result": None,
        "error": None,
        "started": None,
        "finished": None,
    }


# 运行时状态。
_SCAN_JOB = _job_defaults()


def _reset_job(path, dry_run, enrich=False, min_conf=0, max_conf=100,
               min_size=0, max_size=0):

    # 新一轮任务：清掉上一轮的停止/暂停状态
    _SCAN_STOP.clear()

    _SCAN_PAUSE.clear()

    # 从默认表整体归零 —— 不再手写字段清单（漏一个就会跨任务残留）
    _SCAN_JOB.update(_job_defaults())

    _SCAN_JOB.update(
        {
            "running": True,
            "path": path,
            "dry_run": dry_run,
            "enrich": enrich,
            "phase": "scan",
            "min_conf": min_conf,
            "max_conf": max_conf,
            "min_size": min_size,
            "max_size": max_size,
            "started": time.time(),
        }
    )


def _gate():
    """在每个条目边界调用：处理暂停与停止。

    返回 False 表示应该结束任务（收到停止）。
    """

    if _SCAN_STOP.is_set():

        return False

    while _SCAN_PAUSE.is_set():

        if _SCAN_STOP.is_set():

            return False

        time.sleep(0.3)

    return True


def _run_scan(paths, dry_run, enrich=False, min_conf=0, max_conf=100,
              min_size=0, max_size=0):
    """后台线程体：扫描（可多个目录）；enrich=True 时紧接着抓元数据。

    ``paths`` 是**目录列表** —— 支持一次扫多个。
    """

    if isinstance(paths, str):
        paths = [paths]

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

        # ⚠️ 这里原本会**预先 os.walk 一遍**只为数总数，然后扫描时再走一遍。
        # 对 G 盘那种 70 万文件的盘，白走一遍要多花好几分钟，而它的产出
        # 只有 3 个番号。现在改成扫描器走完目录后通过 on_discovered 回调
        # 把真实总数报回来 —— 少走一整遍盘。
        # 大小门槛挡下的文件在这里收口。扫描器只扫"通过的"，被挡下的
        # 不返回 —— 没有这个回调，界面上 skipped_count 永远是 0，
        # 用户无从判断门槛到底生效没有。
        def _on_size_skip(path_, size_, limit_, is_min):

            with _SCAN_LOCK:

                _SCAN_JOB["skipped_count"] += 1

                sk = _SCAN_JOB["skipped_samples"]

                if len(sk) < 50:

                    sk.append({
                        "file": os.path.basename(path_),
                        "size": human_size(size_),
                        "reason": (
                            f"小于下限 {human_size(limit_)}" if is_min
                            else f"超过上限 {human_size(limit_)}"
                        ),
                    })

            # 落到筛选日志（任务结束也能查）。批量写，提交在扫描收尾统一做。
            from core import filter_log

            filter_log.record(
                db,
                path=path_,
                filename=os.path.basename(path_),
                size=size_,
                reason="size_below_min" if is_min else "size_above_max",
                detail=(
                    "{} 门槛 {}（实际 {}）".format(
                        "小于下限" if is_min else "超过上限",
                        human_size(limit_),
                        human_size(size_),
                    )
                ),
                stage="scan",
            )

        def _on_discovered(n):

            with _SCAN_LOCK:

                _SCAN_JOB["total"] += n

        # 多个目录依次扫；总数由扫描器报回（见 on_discovered），
        # 不再预先 walk 一遍。
        all_rows = []

        for one_path in paths:

            if _SCAN_STOP.is_set():
                break

            with _SCAN_LOCK:

                _SCAN_JOB["current_path"] = one_path

            result = service.scan(
                one_path,
                persist=not dry_run,
                min_conf=min_conf,
                max_conf=max_conf,
                min_size=min_size,
                max_size=max_size,
                on_size_skip=_on_size_skip,
                on_discovered=_on_discovered,
            )

            all_rows.extend(result["data"])

        rows = all_rows

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
                        "skipped": row.get("skipped"),
                    }
                )

                if row["persisted"]:

                    _SCAN_JOB["saved"] += 1

                elif row.get("skipped"):

                    # 被可信度门槛挡下的，单独计数 + 展示，否则用户只看到
                    # "扫到了但没进库"，不知道是门槛挡的还是没识别出来
                    _SCAN_JOB["skipped_count"] += 1

                    from core import filter_log as _fl

                    _fl.record(
                        db,
                        path=row["file"],
                        filename=os.path.basename(row["file"]),
                        number=row["skipped"].get("number"),
                        confidence=row["skipped"].get("confidence"),
                        reason="confidence_out_of_range",
                        detail=row["skipped"].get("reason") or "",
                        stage="scan",
                    )

                    sk = _SCAN_JOB["skipped_samples"]

                    if len(sk) < 50:

                        # 带上 reason，与大小门槛挡下的条目形状统一 ——
                        # 前端据此统一渲染，不用猜是哪一类
                        sk.append({
                            "file": os.path.basename(row["file"]),
                            "number": row["skipped"]["number"],
                            "confidence": row["skipped"]["confidence"],
                            "reason": row["skipped"].get("reason", ""),
                        })

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

            from services.javbus_session import JavBusSession

            # 兜底源按需启停：整批抓取前起，整批后关（收尾挂在下方 finally）。
            # 用显式 start/stop 而不是 with —— 循环体很长，with 会让整段
            # 多一级缩进，diff 噪音大且容易改错。
            fallback_on = _fallback_on()

            session = JavBusSession(enabled=fallback_on).start()

            # 记着给 finally 收尾（服务是我们起的才需要关）
            _SCAN_JOB["fallback_session"] = session

            if session.note:

                with _SCAN_LOCK:

                    _SCAN_JOB["fallback_note"] = session.note

            fb = None

            if session.ready:

                from adapters.javbus_adapter import JavBusClient

                fb = JavBusClient()

            enricher = EnrichService(
                db,
                COVERS_DIR,
                SCREENSHOTS_DIR,
                fallback_client=fb,
            )

            for i, num in enumerate(numbers, 1):

                if not _gate():

                    with _SCAN_LOCK:

                        # 同批量抓取路径：停止时清掉 paused，避免两个互斥状态并存
                        _SCAN_JOB["stopped"] = True

                        _SCAN_JOB["paused"] = False

                    break

                try:

                    res = enricher.enrich(num, want="all")

                except Exception as exc:                        # noqa: BLE001

                    res = {"error": f"{type(exc).__name__}: {exc}"}

                with _SCAN_LOCK:

                    _SCAN_JOB["enrich_done"] = i

                    if res.get("error"):

                        _SCAN_JOB["enrich_failed"] += 1

                    else:

                        _SCAN_JOB["enriched"] += 1

                    if res.get("ambiguous_versions"):

                        _SCAN_JOB["enrich_ambiguous"] += 1

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

        # 筛选日志统一提交（批量写，避免逐条 commit）
        try:

            from core import filter_log as _fl2

            _fl2.flush(db)

        except Exception:                                       # noqa: BLE001

            pass

        # 兜底服务是我们起的就关掉（不是我们起的别动）
        _sess = _SCAN_JOB.pop("fallback_session", None)

        if _sess is not None:

            try:
                _sess.stop()
            except Exception:                                   # noqa: BLE001
                pass

        with _SCAN_LOCK:

            _SCAN_JOB["running"] = False

            _SCAN_JOB["finished"] = time.time()


@app.get(
    "/scan",
    response_class=HTMLResponse
)
def scan_page(request: Request):

    # 把 scan_scope 的决策也算出来给前端显示 ——
    # 用户点「开始扫描」之前就该知道会扫哪些盘、为什么。
    try:

        from core.scan_scope import known_paths, parse_scope

        scope, _bad = parse_scope(CONFIG)

        hint = []

        for drive in sorted(scope):

            mode = scope[drive]

            if mode == "skip":
                hint.append("{}skip（不扫）".format(drive))
            elif mode == "full":
                hint.append("{}full（整盘扫）".format(drive))
            else:
                n = len(known_paths(CONFIG, drive))
                hint.append("{}known（只扫 {} 个已知目录）".format(drive, n))

        known = known_paths(CONFIG)

    except Exception:                                           # noqa: BLE001

        hint, known = [], []

    return templates.TemplateResponse(
        request,
        "scan.html",
        {
            "scan_paths": CONFIG.get("scan_paths") or [],
            "known_paths": known,
            "scope_hint": hint,
            "stats": library_stats(),
        }
    )


@app.post("/api/scan/start")
async def scan_start(request: Request):

    payload = await request.json()

    # 支持**多目录**：`paths` 数组优先；旧的 `path` 单值仍认（向后兼容）。
    raw_paths = payload.get("paths")

    if isinstance(raw_paths, list):
        paths = [str(p).strip() for p in raw_paths if str(p or "").strip()]
    else:
        one = (payload.get("path") or "").strip()
        paths = [one] if one else []

    # 去重但保序
    seen = set()
    paths = [p for p in paths if not (p in seen or seen.add(p))]

    # 任务名用第一个目录 + 计数，界面上能看出扫的是哪几个
    label = paths[0] if len(paths) == 1 else "{} 等 {} 个目录".format(
        paths[0], len(paths)) if paths else ""

    dry_run = bool(payload.get("dry_run"))

    enrich = bool(payload.get("enrich"))

    try:

        min_conf = int(payload.get("min_conf", 0))

        max_conf = int(payload.get("max_conf", 100))

    except (TypeError, ValueError):

        return JSONResponse(
            {"ok": False, "error": "置信度门槛必须是数字"},
            status_code=400,
        )

    min_conf = max(0, min(100, min_conf))

    max_conf = max(0, min(100, max_conf))

    if min_conf > max_conf:

        min_conf, max_conf = max_conf, min_conf

    def _size(key):

        try:

            return max(0, int(payload.get(key, 0) or 0))

        except (TypeError, ValueError):

            return 0

    # 0 = 该端不限制（滑块划到头）
    min_size = _size("min_size")

    max_size = _size("max_size")

    if min_size and max_size and min_size > max_size:

        min_size, max_size = max_size, min_size

    if not paths:

        return JSONResponse(
            {"ok": False, "error": "请选择要扫描的目录"},
            status_code=400,
        )

    missing = [p for p in paths if not os.path.isdir(p)]

    if missing:

        return JSONResponse(
            {"ok": False, "error": "目录不存在：{}".format("、".join(missing[:3]))},
            status_code=400,
        )

    with _SCAN_LOCK:

        if _SCAN_JOB["running"]:

            return JSONResponse(
                {"ok": False, "error": "已有扫描任务在跑，等它结束"},
                status_code=409,
            )

        _reset_job(label, dry_run, enrich, min_conf, max_conf, min_size, max_size)

        _SCAN_JOB["paths"] = paths

    threading.Thread(
        target=_run_scan,
        args=(paths, dry_run, enrich, min_conf, max_conf, min_size, max_size),
        daemon=True,
    ).start()

    return {"ok": True, "paths": paths}


@app.post("/api/scan/stop")
def scan_stop():
    """请求停止当前任务（协作式：在下一个条目边界生效）。"""

    with _SCAN_LOCK:

        if not _SCAN_JOB["running"]:

            return {"ok": False, "message": "当前没有在跑的任务"}

        _SCAN_STOP.set()

        _SCAN_PAUSE.clear()

        # 停止是终态：清掉暂停标记，界面不该同时显示「已暂停」和「已停止」
        _SCAN_JOB["paused"] = False

    return {"ok": True, "message": "已请求停止，会在当前条目跑完后停下"}


@app.post("/api/scan/pause")
def scan_pause():
    """暂停当前任务。"""

    with _SCAN_LOCK:

        if not _SCAN_JOB["running"]:

            return {"ok": False, "message": "当前没有在跑的任务"}

        _SCAN_PAUSE.set()

        _SCAN_JOB["paused"] = True

    return {"ok": True, "message": "已暂停"}


@app.post("/api/scan/resume")
def scan_resume():
    """继续被暂停的任务。"""

    with _SCAN_LOCK:

        if not _SCAN_JOB["running"]:

            return {"ok": False, "message": "当前没有在跑的任务"}

        _SCAN_PAUSE.clear()

        _SCAN_JOB["paused"] = False

    return {"ok": True, "message": "已继续"}


@app.get("/api/scan/status")
def scan_status():

    with _SCAN_LOCK:

        job = dict(_SCAN_JOB)

        job["files"] = list(_SCAN_JOB["files"])

    # 只回传最近 200 条明细，避免响应体随扫描规模无限增长
    job["files"] = job["files"][-200:]

    return job


@app.get(
    "/logs",
    response_class=HTMLResponse
)
def logs_page(request: Request, reason: str = "", limit: int = 300):

    try:

        limit = int(limit or 300)

    except (TypeError, ValueError):

        limit = 300

    limit = max(1, min(limit, 2000))

    summary, entries, total, note = _load_filter_log(reason, limit)

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "stats": library_stats(),
            "summary": summary,
            "entries": entries,
            "total": total,
            "reason": reason,
            "limit": limit,
            "note": note,
        }
    )


@app.get(
    "/maintenance",
    response_class=HTMLResponse
)
def maintenance_page(request: Request):
    """维护页：备份、导出、低分清理、空目录、日志。

    这些功能原先散在首页（备份/导出/低分）与独立页（空目录/日志）。
    收拢到这里，首页只留日常要按的按钮。
    """

    return templates.TemplateResponse(
        request,
        "maintenance.html",
        {"stats": library_stats()}
    )







# ── 筛选日志页 ──────────────────────────────────────────────
#
# 后端从 09-24 起就把每次扫描挡下的文件写进 filter_log（小体积 / 识别不出 /
# 时长异常 / 查不到……），但界面上一直没入口 —— 两万多条只能靠脚本查库。
# 这里补上：按原因筛、看明细、导出 CSV。


def _format_log_entries(entries):
    """补两个纯展示字段：可读体积、本地时间。"""

    import datetime as _dt

    out = []

    for e in entries:

        e = dict(e)

        e["size_h"] = human_size(e["size"]) if e.get("size") else "—"

        ts = e.get("created_time")

        when = ""

        if ts:

            try:
                when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            except Exception:                                   # noqa: BLE001
                when = ""

        e["when"] = when

        out.append(e)

    return out





def _load_filter_log(reason="", limit=300):
    """取筛选日志：(汇总, 明细, 总数, 提示)。库没建时返回空，不 500。"""

    from core import filter_log

    from core.database_v2 import Database

    try:

        db = Database(DB_PATH)

        summary = filter_log.summary(db)

        entries = _format_log_entries(
            filter_log.entries(db, reason=reason or None, limit=limit)
        )

        total = 0

        try:

            total = db.conn.execute(
                "SELECT COUNT(*) FROM filter_log"
            ).fetchone()[0]

        except Exception:                                       # noqa: BLE001
            pass

        return summary, entries, total, ""

    except Exception as exc:                                    # noqa: BLE001

        return [], [], 0, "{}: {}".format(type(exc).__name__, exc)


@app.get("/cleanup")
def legacy_cleanup_redirect():
    """空目录已并入维护页（/maintenance）。保留跳转，别让旧书签失效。"""

    from fastapi.responses import RedirectResponse

    return RedirectResponse("/maintenance", status_code=302)


@app.get(
    "/settings",
    response_class=HTMLResponse
)
def settings_page(request: Request, saved: str = ""):

    from core import runtime_settings as RS

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "stats": library_stats(),
            "cur": RS.snapshot(),
            "saved": saved,
            "saved_note": "",
            "error": "",
        }
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    """保存数据源设置。写到 storage/settings.json（**不碰 config.yaml**）。"""

    from core import runtime_settings as RS

    form = await request.form()

    values = {k: form.get(k) for k in RS.EDITABLE_KEYS if k in form}

    ok, note = RS.save(values)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "stats": library_stats(),
            "cur": RS.snapshot(),
            "saved": "1" if ok else "",
            "saved_note": "（{}）".format(note) if ok else "",
            "error": "" if ok else note,
        }
    )


@app.post("/api/settings/test")
def api_settings_test():
    """实测两个数据源通不通。

    这一步很关键：代理端口是使用者自己填的，填错了得**当场知道**，
    而不是等批量抓取时一片超时。
    """

    from core import runtime_settings as RS

    results = []

    # ── JavDB ──
    try:

        from adapters.javdb_adapter import JavDBCLIClient

        client = JavDBCLIClient()

        d = client.detail("SSIS-001")

        if d and d.get("number"):
            results.append({
                "name": "JavDB（后端 {}）".format(client.backend),
                "ok": True,
                "detail": "通，取到 {}".format(d.get("number")),
            })
        else:
            results.append({
                "name": "JavDB（后端 {}）".format(client.backend),
                "ok": False,
                "detail": "没取到数据（查一下后端是否可用）",
            })

    except Exception as exc:                                   # noqa: BLE001

        results.append({
            "name": "JavDB",
            "ok": False,
            "detail": "{}: {}".format(type(exc).__name__, exc),
        })

    # ── JavBus ──
    try:

        from adapters.javbus_adapter import JavBusClient

        client = JavBusClient()

        if not client.available():

            hint = "后端 {} 不可用".format(client.backend)

            if client.backend == "native" and not RS.get("javbus_proxy"):
                hint += "（**没配代理** —— JavBus 直连会超时，去上面填代理地址）"

            results.append({"name": "JavBus", "ok": False, "detail": hint})

        else:

            d = client.detail("SSIS-001")

            if d and d.get("number"):
                results.append({
                    "name": "JavBus（后端 {}）".format(client.backend),
                    "ok": True,
                    "detail": "通，取到 {}".format(d.get("number")),
                })
            else:
                results.append({
                    "name": "JavBus（后端 {}）".format(client.backend),
                    "ok": False,
                    "detail": "连得上但没取到详情",
                })

    except Exception as exc:                                   # noqa: BLE001

        results.append({
            "name": "JavBus",
            "ok": False,
            "detail": "{}: {}".format(type(exc).__name__, exc),
        })

    return JSONResponse({"results": results})


@app.get("/api/filter-log/export")
def api_filter_log_export(reason: str = "", limit: int = 20000):
    """筛选日志导 CSV。带 BOM，Excel 打开不乱码。"""

    import csv

    import io

    from fastapi.responses import Response

    try:

        limit = int(limit or 20000)

    except (TypeError, ValueError):

        limit = 20000

    limit = max(1, min(limit, 200000))

    _summary, entries, _total, _note = _load_filter_log(reason, limit)

    buf = io.StringIO()

    w = csv.writer(buf)

    w.writerow(["原因码", "说明", "番号", "文件名", "路径", "体积",
                "来源", "置信度", "阶段", "时间"])

    for e in entries:

        w.writerow([
            e.get("reason", ""),
            e.get("detail", ""),
            e.get("number", ""),
            e.get("filename", ""),
            e.get("path", ""),
            e.get("size_h", ""),
            e.get("source", ""),
            e.get("confidence", ""),
            e.get("stage", ""),
            e.get("when", ""),
        ])

    data = "\ufeff" + buf.getvalue()

    name = "filter-log{}.csv".format("-" + reason if reason else "")

    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="{}"'.format(name)
        },
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


@app.post("/api/favorite/{number}")
def api_favorite(number: str):
    """翻转收藏位，返回翻转后的状态。

    ⚠️ 收藏是**纯人工标记**，不改任何自动判定 —— 不抬置信分、不解锁删除。
    删除门控只看 `deletable_titles()`（verified=1 磁力），与本接口无关。
    """

    rows = query(
        "SELECT id, favorite FROM titles WHERE number = ?",
        (number,)
    )

    if not rows:

        return JSONResponse(
            {"ok": False, "error": "库里没有该番号"},
            status_code=404,
        )

    new_val = 0 if rows[0]["favorite"] else 1

    execute_write(
        "UPDATE titles SET favorite = ? WHERE id = ?",
        (new_val, rows[0]["id"])
    )

    return {
        "ok": True,
        "number": number,
        "favorite": bool(new_val),
        "message": "已收藏" if new_val else "已取消收藏",
    }


@app.post("/api/comments/{number}")
def api_comments(number: str):
    """按需抓一页 javdb 评论正文并落库。

    **不自动联网** —— 只有点「抓取评论」才走这里（与抓元数据同一个原则）。
    """

    from core.database_v2 import Database as _DB

    rows = query("SELECT id FROM titles WHERE number = ?", (number,))

    if not rows:

        return JSONResponse(
            {"ok": False, "error": "库里没有该番号"}, status_code=404
        )

    from adapters.javdb_adapter import JavDBCLIClient

    client = JavDBCLIClient()

    reviews = client.reviews(number, limit=20)

    if not reviews:

        # 抓不到和「没有评论」都可能走到这。分开说，别把失败说成没评论。
        return {
            "ok": False,
            "error": "没抓到评论（网络故障、番号在 javdb 无评论，或"
                     "cli 不支持）",
            "count": 0,
        }

    db = _DB(DB_PATH)

    added = db.save_comments(rows[0]["id"], reviews)

    return {
        "ok": True,
        "count": len(reviews),
        "added": added,
        "message": "抓到 {} 条评论（新增 {} 条）".format(len(reviews), added),
    }


@app.post("/api/preview-video/{number}")
def api_preview_video(number: str):
    """抓宣传视频并**存成能直接播的 mp4**。

    ## 为什么不是只存链接

    javdb 给的是 HLS 的 `.m3u8`（带签名、会过期，分片还 AES-128 加密）。
    浏览器原生不支持 HLS —— 主人拿到的正是「点一下就转成下载」。
    所以这里落盘成 mp4：模板用普通 `<video>` 播，**不引 hls.js**，
    而且存一次之后离线也能看，不受签名过期影响。

    下载走官方 `javdb assets download -o`（它本来就干这个）。
    """

    from core.database_v2 import Database as _DB

    from adapters.javdb_adapter import JavDBCLIClient

    rows = query("SELECT id FROM titles WHERE number = ?", (number,))

    if not rows:

        return JSONResponse(
            {"ok": False, "error": "库里没有该番号"}, status_code=404
        )

    title_id = rows[0]["id"]

    db = _DB(DB_PATH)

    svc = build_enrich_service(db)

    existing = db.conn.execute(
        "SELECT preview_video FROM metadata WHERE title_id = ?", (title_id,)
    ).fetchone()

    url = existing["preview_video"] if existing else None

    if not url:

        url = JavDBCLIClient().preview_video_url(number)

        if not url:

            return {"ok": False, "error": "这部没有宣传视频，或没抓到"}

        db.conn.execute(
            "UPDATE metadata SET preview_video = ? WHERE title_id = ?",
            (url, title_id),
        )

        db.conn.commit()

    got = svc.save_preview_video(number, url)

    if not got["ok"]:

        # 链接过期/下载失败要说清楚，别让前端以为「没有视频」
        return {"ok": False, "error": got["error"], "url": url}

    return {
        "ok": True,
        "play": preview_url(number),
        "message": "宣传视频已存好，可直接播",
    }


@app.get("/api/delete-batch/preview")
def api_delete_batch_preview():
    """批量送回收站的**预览** —— 弹窗要显示「动哪些、多少、腾多少」。

    只读，不改任何东西。主人 2026-09-24 要的「一键」必须**先看得见**，
    否则一键就是盲删。
    """

    try:

        svc = build_file_service()

        cands = svc.batch_candidates()

    except Exception as exc:                                    # noqa: BLE001

        return {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc),
                "count": 0, "files": 0, "bytes": 0, "bytes_h": "0 B",
                "titles": []}

    total_files = sum(c["files"] for c in cands)
    total_bytes = sum(c["bytes"] for c in cands)

    return {
        "ok": True,
        "count": len(cands),
        "files": total_files,
        "bytes": total_bytes,
        "bytes_h": human_size(total_bytes),
        # 弹窗里列出番号（按占用从大到小），带上各自大小
        "titles": [
            {
                "number": c["number"],
                "tier": c["tier"],
                "files": c["files"],
                "bytes_h": human_size(c["bytes"]),
            }
            for c in cands
        ],
    }


@app.post("/api/delete-batch")
def api_delete_batch():
    """把高置信（`batch=True`）的番号文件**全部送回收站**。

    ⚠️ 三条边界，与单卡删除完全一致：
      * 只送文件，**卡保留**（`local_deleted` 标记，见 FileService）
      * 一律走回收站，不用 os.remove / rmtree
      * 判据复用 `deletion_eligibility()` 的 `batch`，不另立一套

    确认由前端弹窗负责（列出 N 部 / X 个文件 / Y GB 再让用户点确定）——
    服务端不做二次确认弹窗，但要**如实回报删了什么**。
    """

    try:

        svc = build_file_service()

        summary = svc.delete_batch()

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)},
            status_code=500,
        )

    msg = "已送回收站 {} 个文件 / {} 部（卡片保留，可恢复）".format(
        summary["files"], len(summary["titles"])
    )

    if summary["failed"]:

        msg += "；{} 项失败".format(len(summary["failed"]))

    return {
        "ok": not summary["failed"],
        "message": msg,
        "titles": len(summary["titles"]),
        "files": summary["files"],
        "bytes_h": human_size(summary["bytes"]),
        "failed": summary["failed"][:20],
    }


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
        WHERE metadata.title IS NULL
           OR metadata.cover_local IS NULL
           -- ③ 档位为空也要补抓。
           --
           -- 「待抓」原先只看元数据缺不缺，于是**档位为空的条目选不进
           -- 候选**，tier 永远补不上。实测 KW-7142：元数据和封面都在、
           -- 0 磁力、tier 是 NULL —— 按钮点下去也轮不到它。
           -- 这种多半是历史版本留下的（旧代码在无磁力时不判档）。
           OR titles.tier IS NULL
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

    session = None
    def _run():
        nonlocal session

        try:

            from core.database_v2 import Database

            from services.enrich_service import EnrichService

            from services.javbus_session import JavBusSession

            # 兜底源的**按需启停**：整批抓取开始时起一次服务，整批结束再关。
            # 不做「每条查不到就起一次」—— 那会让单条请求莫名其妙多等几秒。
            # 服务本来就在线（比如开机自启留下的）则不碰它，也不在结束时杀它。
            session = JavBusSession(enabled=_fallback_on()).start()

            if session.note:

                with _SCAN_LOCK:

                    _SCAN_JOB["fallback_note"] = session.note

            # 服务不在线（且启不起来）时不给兜底客户端 —— 免得每次
            # 查不到都白等一次连接超时。
            fb = None

            if session.ready:

                from adapters.javbus_adapter import JavBusClient

                fb = JavBusClient()

            # ── 并发抓取（主人 2026-09-24 定的方案）──
            #
            # 实测：磁力查询**不需要登录态**，所以隔离 HOME 后跑并发 ——
            # 既不会并发写坏 auth.json，也没有登录账号可被风控。
            # 实测加速：并发 6 = 4.4x，并发 12 = 10.8x，单条中位耗时不变。
            from core import javdb_anon

            from adapters.javdb_adapter import JavDBCLIClient

            from services.batch_enrich import DEFAULT_WORKERS, run_batch

            anon_home = javdb_anon.ensure_isolated_home()

            javdb_anon.purge_login(anon_home)       # 兜底：确保没登录态

            workers = int(CONFIG.get("enrich_workers") or DEFAULT_WORKERS)

            def make_service():

                # 每个任务一个**新连接** —— SQLite 连接不能跨线程用
                return EnrichService(
                    Database(DB_PATH),
                    COVERS_DIR,
                    SCREENSHOTS_DIR,
                    client=JavDBCLIClient(home=anon_home),
                    fallback_client=fb,
                )

            def on_progress(n_done, total, res):

                with _SCAN_LOCK:

                    _SCAN_JOB["enrich_done"] = n_done

                    if res.get("error"):

                        _SCAN_JOB["enrich_failed"] += 1

                        errs = _SCAN_JOB["enrich_errors"]

                        if len(errs) < 40:

                            errs.append({
                                "number": res.get("number"),
                                "error": res["error"],
                            })

                    else:

                        _SCAN_JOB["enriched"] += 1

                    if res.get("ambiguous_versions"):

                        _SCAN_JOB["enrich_ambiguous"] += 1

            def should_stop():

                return not _gate()

            run_batch(
                numbers,
                make_service,
                workers=workers,
                on_progress=on_progress,
                should_stop=should_stop,
            )

            with _SCAN_LOCK:

                if _SCAN_STOP.is_set():

                    _SCAN_JOB["stopped"] = True

                    _SCAN_JOB["paused"] = False

        except Exception as exc:                                # noqa: BLE001

            with _SCAN_LOCK:

                _SCAN_JOB["error"] = f"{type(exc).__name__}: {exc}"

        finally:


            # 兜底服务是我们起的就关掉（不是我们起的不动）
            if session is not None:

                try:
                    session.stop()
                except Exception:                           # noqa: BLE001
                    pass
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


# ── 低分卡片清理 ────────────────────────────────────────────


@app.get("/api/low-score")
def api_low_score(max_score: int = 79):
    """列出识别分 <= max_score 的库内条目（只读）。

    分数不落库，按文件名重放识别得出 —— 所以这是"当前规则下的分数"。
    """

    svc = build_file_service()

    entries = svc.low_score_entries(max_score)

    # 分数分布：让用户看着分布决定阈值，而不是盲删。
    # 70 分档里混着"厂牌未收录的真番号"（ACC-006 / AMBI-128 这类），
    # 只看条数容易一刀切误伤。
    dist = {}

    for e in entries:

        key = str(e["confidence"])

        dist[key] = dist.get(key, 0) + 1

    return {
        "ok": True,
        "max_score": max_score,
        "count": len(entries),
        "distribution": dist,
        "entries": entries[:300],
    }


@app.post("/api/low-score/remove")
async def api_low_score_remove(request: Request):
    """把指定条目**从媒体库摘除**。

    ⚠️ 只删库记录，**磁盘上的文件不动** —— 这是"清理卡片"不是"删片"。
    真要删文件走卡片上的删除按钮（那条路有磁力门控 + 回收站）。
    """

    payload = await request.json()

    paths = payload.get("filepaths") or []

    if not paths:

        return JSONResponse(
            {"ok": False, "error": "没有要清理的条目"},
            status_code=400,
        )

    svc = build_file_service()

    removed, orphans = svc.remove_entries(paths)

    return {
        "ok": True,
        "removed": removed,
        "orphan_titles": orphans,
        "message": f"已从库中摘除 {removed} 条记录"
                   + (f"，清理空标题 {orphans} 个" if orphans else "")
                   + "（磁盘文件未动）",
    }


# ── 手动从文件夹加入媒体库 ──────────────────────────────────


@app.post("/api/scan/import")
async def api_scan_import(request: Request):
    """手动指定一个文件夹，识别其中的番号并加入库（不进后台任务队列）。

    与 /api/scan/start 的区别：这是**同步**的、单目录、面向"我就想加这几个
    文件"的场景，结果直接返回，前端即时显示。批量扫描仍走 /api/scan/start。
    """

    payload = await request.json()

    path = (payload.get("path") or "").strip()

    if not path:

        return JSONResponse(
            {"ok": False, "error": "请填写文件夹路径"},
            status_code=400,
        )

    if not os.path.isdir(path):

        return JSONResponse(
            {"ok": False, "error": f"目录不存在：{path}"},
            status_code=400,
        )

    try:

        min_conf = int(payload.get("min_conf", 0) or 0)

        max_conf = int(payload.get("max_conf", 100) if payload.get("max_conf") is not None else 100)

    except (TypeError, ValueError):

        min_conf, max_conf = 0, 100

    min_conf = max(0, min(min_conf, 100))

    max_conf = max(0, min(max_conf, 100))

    if min_conf > max_conf:

        min_conf, max_conf = max_conf, min_conf

    from core.database_v2 import Database

    from services.scan_service import ScanService

    try:

        service = ScanService(
            resolve_path(CONFIG.get("index_db") or "storage/file_index_v2.db"),
            load_rules(),
            db=Database(DB_PATH),
            extensions=CONFIG.get("video_extensions") or [],
            excluded_segments=CONFIG.get("excluded_dir_segments") or [],
        )

        result = service.scan(
            path,
            persist=True,
            min_conf=min_conf,
            max_conf=max_conf,
        )

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    rows = result["data"]

    added = []
    skipped = []

    for row in rows:

        if row["persisted"]:

            added.append(
                {
                    "file": os.path.basename(row["file"]),
                    "number": row["numbers"][0]["number"] if row["numbers"] else "",
                    "confidence": row["numbers"][0]["confidence"] if row["numbers"] else 0,
                }
            )

        elif row.get("skipped"):

            skipped.append(
                {
                    "file": os.path.basename(row["file"]),
                    "number": row["skipped"]["number"],
                    "confidence": row["skipped"]["confidence"],
                }
            )

    return {
        "ok": True,
        "path": path,
        "added": added,
        "skipped": skipped,
        "message": f"加入 {len(added)} 条"
                   + (f"，置信度门槛挡下 {len(skipped)} 条" if skipped else ""),
    }


# ── 导出 / 导入媒体库 ───────────────────────────────────────


@app.post("/api/backup/export")
async def api_backup_export(request: Request):
    """导出媒体库为压缩包（后台线程跑，避免大包把请求卡死）。"""

    try:

        payload = await request.json()

    except Exception:                                           # noqa: BLE001

        payload = {}

    include_images = bool((payload or {}).get("include_images", True))

    with _SCAN_LOCK:

        if _SCAN_JOB["running"]:

            return JSONResponse(
                {"ok": False, "error": "有任务在跑，等它结束再导出（避免打到写一半的库）"},
                status_code=409,
            )

        _SCAN_JOB.update({
            "running": True,
            "path": "(导出媒体库)",
            "phase": "export",
            "error": None,
            "started": time.time(),
            "finished": None,
            "export_result": None,
        })

    def _run():

        try:

            svc = build_backup_service()

            path, stats = svc.export(include_images=include_images)

            with _SCAN_LOCK:

                _SCAN_JOB["export_result"] = {
                    "path": path,
                    "name": os.path.basename(path),
                    "size": os.path.getsize(path) if os.path.exists(path) else 0,
                    "stats": stats,
                }

        except Exception as exc:                                # noqa: BLE001

            with _SCAN_LOCK:

                _SCAN_JOB["error"] = f"{type(exc).__name__}: {exc}"

        finally:

            with _SCAN_LOCK:

                _SCAN_JOB["running"] = False

                _SCAN_JOB["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()

    return {"ok": True, "include_images": include_images}


@app.get("/api/backup/download/{name}")
def api_backup_download(name: str):
    """下载导出好的压缩包。"""

    safe = os.path.basename(name)

    path = os.path.join(EXPORT_DIR, safe)

    if not os.path.exists(path):

        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)

    from fastapi.responses import FileResponse

    return FileResponse(path, filename=safe, media_type="application/zip")


@app.post("/api/backup/import")
async def api_backup_import(request: Request):
    """导入压缩包（合并进现有库；导入前自动备份现有库）。"""

    from fastapi import UploadFile

    # 支持两种投递方式：上传文件，或给服务器上已有包的路径
    content_type = request.headers.get("content-type", "")

    zip_path = None

    include_images = True

    temp = None

    try:

        if "multipart/form-data" in content_type:

            form = await request.form()

            upload = form.get("file")

            include_images = str(form.get("include_images", "1")) not in ("0", "false", "False")

            if not isinstance(upload, UploadFile):

                return JSONResponse(
                    {"ok": False, "error": "没有收到文件"},
                    status_code=400,
                )

            os.makedirs(EXPORT_DIR, exist_ok=True)

            temp = os.path.join(
                EXPORT_DIR, f".upload-{int(time.time())}.zip"
            )

            with open(temp, "wb") as out:

                while True:

                    chunk = await upload.read(1024 * 1024)

                    if not chunk:

                        break

                    out.write(chunk)

            zip_path = temp

        else:

            payload = await request.json()

            name = (payload or {}).get("name") or ""

            include_images = bool((payload or {}).get("include_images", True))

            zip_path = os.path.join(EXPORT_DIR, os.path.basename(name))

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": f"读取上传失败：{exc}"},
            status_code=400,
        )

    with _SCAN_LOCK:

        if _SCAN_JOB["running"]:

            return JSONResponse(
                {"ok": False, "error": "有任务在跑，等它结束再导入"},
                status_code=409,
            )

    try:

        svc = build_backup_service()

        stats, backup = svc.import_zip(zip_path, include_images=include_images)

    except FileNotFoundError:

        return JSONResponse({"ok": False, "error": "压缩包不存在"}, status_code=404)

    except ValueError as exc:

        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    except Exception as exc:                                    # noqa: BLE001

        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    finally:

        if temp and os.path.exists(temp):

            os.remove(temp)

    return {
        "ok": True,
        "stats": stats,
        "message": (
            f"新增 {stats['titles_added']} 个番号 / {stats['files_added']} 个文件 / "
            f"{stats['magnets_added']} 条磁力；"
            f"素材 {stats['covers']} 封面 + {stats['screenshots']} 截图"
        ),
    }


@app.get("/api/filter-log")
def api_filter_log(reason: str = "", limit: int = 300):
    """筛选日志：被挡下的文件 + 原因 + 人话说明。"""

    from core import filter_log

    from core.database_v2 import Database

    try:

        db = Database(DB_PATH)

        return {
            "ok": True,
            "summary": filter_log.summary(db),
            "entries": filter_log.entries(db, reason=reason or None, limit=limit),
        }

    except Exception as exc:                                    # noqa: BLE001

        # 库还没建（首次使用）时不该 500 —— 返回空即可
        return {"ok": True, "summary": [], "entries": [],
                "note": f"{type(exc).__name__}: {exc}"}


@app.get("/api/backup/list")
def api_backup_list():
    """列出导出目录里已有的压缩包。"""

    out = []

    if os.path.isdir(EXPORT_DIR):

        for name in sorted(os.listdir(EXPORT_DIR), reverse=True):

            if not name.lower().endswith(".zip"):

                continue

            full = os.path.join(EXPORT_DIR, name)

            try:

                st = os.stat(full)

            except OSError:

                continue

            out.append(
                {
                    "name": name,
                    "size": st.st_size,
                    "size_h": human_size(st.st_size),
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
                    ),
                }
            )

    return {"ok": True, "dir": EXPORT_DIR, "files": out}


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8811,
    )