import os
import sqlite3


class LibraryAudit:

    def __init__(
        self,
        db_path
    ):

        self.conn = sqlite3.connect(
            db_path
        )

        self.conn.row_factory = sqlite3.Row

    def missing_files(self):

        rows = self.conn.execute(
            """
            SELECT
            number,
            filepath

            FROM videos
            """
        ).fetchall()

        result = []

        for row in rows:

            if not os.path.exists(
                row["filepath"]
            ):

                result.append(
                    {
                        "type":
                        "missing_file",

                        "number":
                        row["number"],

                        "filepath":
                        row["filepath"]
                    }
                )

        return result

    def duplicate_numbers(self):

        rows = self.conn.execute(
            """
            SELECT
            number,
            COUNT(*) as count

            FROM videos

            GROUP BY number

            HAVING count > 1

            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def empty_metadata(self):

        rows = self.conn.execute(
            """
            SELECT
            number

            FROM videos

            WHERE number NOT IN
            (
                SELECT number
                FROM metadata
            )

            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def full_audit(self):

        return {

            "missing_files":
            self.missing_files(),

            "duplicate_numbers":
            self.duplicate_numbers(),

            "empty_metadata":
            self.empty_metadata()

        }
