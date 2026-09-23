import sqlite3
import yaml

from core.organizer import Organizer


with open(
    "config.yaml",
    encoding="utf-8"
) as f:

    config = yaml.safe_load(f)

organizer = Organizer(
    config["library_path"],
    config["organize"]["mode"]
)

conn = sqlite3.connect(
    "storage/library.db"
)

rows = conn.execute(
    """
    SELECT
    number,
    filepath
    FROM videos
    """
).fetchall()

for row in rows:

    number = row[0]

    filepath = row[1]

    new_path = organizer.organize(
        number,
        filepath
    )

    print(
        number,
        "=>",
        new_path
    )
