import sqlite3


class DatabaseGuard:

    V2_TABLES = {

        "titles",

        "media_files",

        "metadata",

        "file_index"

    }

    def check(
        self,
        path
    ):

        conn = sqlite3.connect(
            path
        )

        tables = {

            x[0]

            for x in conn.execute(
            """

            SELECT name

            FROM sqlite_master

            WHERE type='table'

            """
            )

        }

        if "videos" in tables:

            raise Exception(

            "v1 database detected, migration required"

            )

        return True
