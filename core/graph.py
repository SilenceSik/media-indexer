import sqlite3


class MediaGraph:

    def __init__(
        self,
        db
    ):

        self.conn = sqlite3.connect(
            db
        )

    def add_actor(
        self,
        number,
        actor
    ):

        cur = self.conn.execute(
            """
            INSERT OR IGNORE
            INTO actors(name)

            VALUES(?)
            """,
            (
                actor,
            )
        )

        actor_id = self.conn.execute(
            """
            SELECT id
            FROM actors
            WHERE name=?
            """,
            (
                actor,
            )
        ).fetchone()[0]

        self.conn.execute(
            """
            INSERT INTO video_actor

            VALUES(?,?)

            """,
            (
                number,
                actor_id
            )
        )

        self.conn.commit()

    def add_tag(
        self,
        number,
        tag
    ):

        self.conn.execute(
            """
            INSERT OR IGNORE
            INTO tags(name)

            VALUES(?)
            """,
            (
                tag,
            )
        )

        tag_id = self.conn.execute(
            """
            SELECT id
            FROM tags

            WHERE name=?

            """,
            (
                tag,
            )
        ).fetchone()[0]

        self.conn.execute(
            """
            INSERT INTO video_tag

            VALUES(?,?)

            """,
            (
                number,
                tag_id
            )
        )

        self.conn.commit()
