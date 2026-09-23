import sqlite3

from adapters.javdb import JavdbClient
from core.database import Database
from core.metadata import MetadataManager


db = Database(
    "storage/library.db"
)

client = JavdbClient()

manager = MetadataManager(
    client,
    db
)

conn = sqlite3.connect(
    "storage/library.db"
)

rows = conn.execute(
    """
    SELECT number
    FROM videos
    """
).fetchall()

for row in rows:

    manager.update(
        row[0]
    )
