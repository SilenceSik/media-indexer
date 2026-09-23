import sqlite3

from core.audit import LibraryAudit
from core.search import AdvancedSearch
from ai.planner import Planner


DB_PATH = "storage/library.db"


def get_connection():

    conn = sqlite3.connect(
        DB_PATH
    )

    conn.row_factory = sqlite3.Row

    return conn


def search_media(keyword):

    conn = get_connection()

    rows = conn.execute(
        """
        SELECT
        videos.number,
        videos.filename,
        videos.filepath,
        metadata.title

        FROM videos

        LEFT JOIN metadata

        ON videos.number =
        metadata.number

        WHERE
        videos.number LIKE ?
        OR metadata.title LIKE ?

        LIMIT 50

        """,
        (
            f"%{keyword}%",
            f"%{keyword}%"
        )
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


def find_missing_metadata():

    conn = get_connection()

    rows = conn.execute(
        """
        SELECT number, filepath

        FROM videos

        WHERE number NOT IN
        (
            SELECT number
            FROM metadata
        )

        """
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


def find_missing_cover():

    conn = get_connection()

    rows = conn.execute(
        """
        SELECT
        number,
        title

        FROM metadata

        WHERE
        cover_local IS NULL

        """
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


def library_statistics():

    conn = get_connection()

    videos = conn.execute(
        "SELECT COUNT(*) FROM videos"
    ).fetchone()[0]

    metadata = conn.execute(
        "SELECT COUNT(*) FROM metadata"
    ).fetchone()[0]

    conn.close()

    return {

        "videos": videos,

        "metadata": metadata

    }


def library_audit():

    audit = LibraryAudit(
        DB_PATH
    )

    return audit.full_audit()


def create_rename_plan(
    filepath,
    number
):

    planner = Planner()

    return planner.create_rename_plan(
        filepath,
        number
    )


def search_by_actor(
    actor
):

    search = AdvancedSearch(
        DB_PATH
    )

    return search.by_actor(
        actor
    )


def search_by_tag(
    tag
):

    search = AdvancedSearch(
        DB_PATH
    )

    return search.by_tag(
        tag
    )
