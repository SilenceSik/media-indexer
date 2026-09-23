import sqlite3
import time
import json


class TaskQueue:

    def __init__(
        self,
        db
    ):

        self.conn = sqlite3.connect(
            db
        )

        self.conn.row_factory = sqlite3.Row

        self.create()

    def create(self):

        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks

        (

        id INTEGER PRIMARY KEY,

        type TEXT,

        payload TEXT,

        status TEXT,

        priority INTEGER DEFAULT 0,

        error TEXT,

        retry INTEGER DEFAULT 0,

        created REAL

        )

        """
        )

        self.conn.commit()

    def add(
        self,
        task_type,
        payload,
        priority=0
    ):

        if isinstance(
            payload,
            dict
        ):

            payload = json.dumps(
                payload,
                ensure_ascii=False
            )

        self.conn.execute(
        """

        INSERT INTO tasks

        (

        type,

        payload,

        status,

        priority,

        created

        )

        VALUES(?,?,?,?,?)

        """,

        (

        task_type,

        payload,

        "pending",

        priority,

        time.time()

        )
        )

        self.conn.commit()

    def get(self):

        row = self.conn.execute(
        """

        SELECT *

        FROM tasks

        WHERE status='pending'

        ORDER BY priority DESC

        LIMIT 1

        """
        ).fetchone()

        if not row:

            return None

        task_id = row["id"]

        self.conn.execute(
        """

        UPDATE tasks

        SET status='running'

        WHERE id=?

        """,
        (
            task_id,
        )
        )

        self.conn.commit()

        row = self.conn.execute(
        """

        SELECT *

        FROM tasks

        WHERE id=?

        """,
        (
            task_id,
        )
        ).fetchone()

        return dict(row)

    def done(
        self,
        task_id
    ):

        self.conn.execute(
        """

        UPDATE tasks

        SET

        status='completed'

        WHERE id=?

        """,
        (
            task_id,
        )
        )

        self.conn.commit()

    def fail(
        self,
        task_id,
        error
    ):

        self.conn.execute(
        """

        UPDATE tasks

        SET

        status='failed',

        error=?

        WHERE id=?

        """,
        (
            error,
            task_id
        )
        )

        self.conn.commit()
