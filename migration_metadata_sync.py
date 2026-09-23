import sqlite3
import sys


def migrate(db_path):

    conn = sqlite3.connect(db_path)

    try:

        conn.execute(
            """
            ALTER TABLE metadata

            ADD COLUMN sync_status TEXT
            DEFAULT 'pending'
            """
        )

    except sqlite3.OperationalError as error:

        print("skip sync_status:", error)

    try:

        conn.execute(
            """
            ALTER TABLE metadata

            ADD COLUMN sync_time REAL
            """
        )

    except sqlite3.OperationalError as error:

        print("skip sync_time:", error)

    conn.commit()

    conn.close()


if __name__ == "__main__":

    migrate(
        sys.argv[1]
    )
