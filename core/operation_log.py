import sqlite3
import time
import os


class OperationLog:

    def __init__(
        self,
        db_path
    ):

        folder = os.path.dirname(
            db_path
        )

        if folder:

            os.makedirs(
                folder,
                exist_ok=True
            )

        self.conn = sqlite3.connect(
            db_path
        )

        self.create()

    def create(self):

        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operations
        (

            id INTEGER PRIMARY KEY,

            action TEXT,

            source TEXT,

            target TEXT,

            source_hash TEXT,

            target_hash TEXT,

            status TEXT,

            created_time REAL

        )
        """
        )

        self.conn.commit()

    def add(
        self,
        action,
        source,
        target,
        source_hash
    ):

        self.conn.execute(
        """
        INSERT INTO operations
        (
            action,
            source,
            target,
            source_hash,
            status,
            created_time
        )

        VALUES
        (?,?,?,?,?,?)

        """,
        (
            action,
            source,
            target,
            source_hash,
            "pending",
            time.time()
        )
        )

        self.conn.commit()

    def query(
        self,
        limit=100
    ):

        self.conn.row_factory = sqlite3.Row

        rows = self.conn.execute(
        """

        SELECT *

        FROM operations

        ORDER BY id DESC

        LIMIT ?

        """,
        (
            limit,
        )

        ).fetchall()

        return [

            dict(x)

            for x in rows

        ]

    def finish(
        self,
        source,
        target,
        target_hash
    ):

        self.conn.execute(
        """
        UPDATE operations

        SET

        target_hash=?,

        status='completed'

        WHERE

        source=?

        AND

        target=?

        """,
        (
            target_hash,
            source,
            target
        )
        )

        self.conn.commit()
