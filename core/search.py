import sqlite3


class AdvancedSearch:

    def __init__(
        self,
        db
    ):

        self.conn = sqlite3.connect(
            db
        )

    def by_actor(
        self,
        actor
    ):

        rows = self.conn.execute(
            """
            SELECT
            video_actor.number

            FROM video_actor

            JOIN actors

            ON actors.id =
            video_actor.actor_id

            WHERE actors.name=?

            """,
            (
                actor,
            )
        ).fetchall()

        return [
            x[0]
            for x in rows
        ]

    def by_tag(
        self,
        tag
    ):

        rows = self.conn.execute(
            """
            SELECT
            video_tag.number

            FROM video_tag

            JOIN tags

            ON tags.id =
            video_tag.tag_id

            WHERE tags.name=?

            """,
            (
                tag,
            )
        ).fetchall()

        return [
            x[0]
            for x in rows
        ]
