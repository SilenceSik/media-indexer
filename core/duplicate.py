import sqlite3

from core.filehash import calculate_hash


class DuplicateChecker:

    def __init__(
        self,
        db_path
    ):

        self.conn = sqlite3.connect(
            db_path
        )

        self.create()

    def create(self):

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hash
            (
                filepath TEXT PRIMARY KEY,

                hash TEXT
            )
            """
        )

        self.conn.commit()

    def check(
        self,
        filepath
    ):

        file_hash = calculate_hash(
            filepath
        )

        row = self.conn.execute(
            """
            SELECT filepath
            FROM file_hash
            WHERE hash=?
            """,
            (
                file_hash,
            )
        ).fetchone()

        if row:

            return row[0]

        self.conn.execute(
            """
            INSERT OR REPLACE
            INTO file_hash
            VALUES(?,?)
            """,
            (
                filepath,
                file_hash
            )
        )

        self.conn.commit()

        return None
