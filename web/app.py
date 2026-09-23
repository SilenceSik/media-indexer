import os
import sqlite3

import yaml

from fastapi import FastAPI, Request

from fastapi.responses import HTMLResponse

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


app = FastAPI(
    title="Local Media Manager"
)


templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "web/templates")
)


# 模板里封面 URL 是 /images/covers/<file>（见 web/templates/index.html），
# 所以 /images 必须挂到 covers 的**父目录**，否则封面 404。
COVERS_DIR = resolve_path(
    (
        load_config().get("image_storage") or {}
    ).get("covers")
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


# v2 主库；v1 库（"storage/library.db"）已冻结为迁移源
DB_PATH = resolve_path(
    load_config().get("database")
)


def query(sql, args=()):

    conn = sqlite3.connect(
        DB_PATH
    )

    conn.row_factory = sqlite3.Row

    cur = conn.execute(
        sql,
        args
    )

    result = cur.fetchall()

    conn.close()

    return result


# 模板变量名保持 `videos`（模板未动）。
# 数据源已从 v1 的 videos 表切到 v2 的 media_files + titles + metadata，
# 关联一律走 titles.id，禁止 metadata.number（见 DECISIONS D4）。
LIST_SQL = """
        SELECT
        titles.number,
        media_files.filename,
        metadata.title,
        metadata.cover,
        metadata.cover_local

        FROM media_files

        JOIN titles

        ON titles.id =
        media_files.title_id

        LEFT JOIN metadata

        ON metadata.title_id =
        titles.id
"""


@app.get(
    "/",
    response_class=HTMLResponse
)
def index(
    request: Request
):

    rows = query(
        LIST_SQL
        + """
        LIMIT 100
        """
    )

    # starlette 1.3.1 已移除 TemplateResponse(name, context) 旧签名，
    # 必须传 request：TemplateResponse(request, name, context)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": rows
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
        """,
        (
            f"%{q}%",
        )
    )

    # starlette 1.3.1 已移除 TemplateResponse(name, context) 旧签名，
    # 必须传 request：TemplateResponse(request, name, context)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "videos": rows
        }
    )
