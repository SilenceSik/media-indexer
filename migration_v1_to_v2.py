import sqlite3

from core.database_v2 import Database


old = "storage/library.db"

new = "storage/library_v2.db"


old_db = sqlite3.connect(
    old
)


new_db = Database(
    new
)


rows = old_db.execute(
"""
SELECT
number,
filepath

FROM videos

"""
).fetchall()


for number, filepath in rows:

    new_db.add_file(
        number,
        filepath
    )


print(
    "migration finished:",
    len(rows)
)
