"""⚠️ v1 遗留模块（已冻结）：v2 主线对应 `core/database_v2.py`。

保留原因：作为 v1 数据库的只读迁移源。v2 链路不引用本文件。
本模块已冻结：v2 为唯一主线，v1 仅作只读迁移源。
"""
import json
import os
import sqlite3
import time


class Database:

    def __init__(self, path):

        folder = os.path.dirname(path)

        if folder:
            os.makedirs(folder, exist_ok=True)

        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")

        self.create()

    def create(self):

        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos
        (
            id INTEGER PRIMARY KEY,
            number TEXT NOT NULL,
            filename TEXT,
            filepath TEXT UNIQUE,
            file_hash TEXT,
            size INTEGER,
            created_time REAL,
            score INTEGER DEFAULT 0
        )
        """
        )

        self.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata
        (
            id INTEGER PRIMARY KEY,
            number TEXT UNIQUE,
            title TEXT,
            cover TEXT,
            cover_local TEXT,
            release_date TEXT,
            maker TEXT,
            actresses TEXT,
            tags TEXT,
            updated_time REAL
        )
        """
        )

        self.conn.commit()

    def insert(self, number, filepath, score=0, file_hash=None, size=None, created_time=None):

        filename = os.path.basename(filepath)

        self.conn.execute(
        """
        INSERT OR IGNORE INTO videos
        (number, filename, filepath, file_hash, size, created_time, score)
        VALUES(?,?,?,?,?,?,?)
        """,
        (number, filename, filepath, file_hash, size, created_time, score)
        )

        self.conn.commit()

    def save_metadata(self, data):

        self.conn.execute(
        """
        INSERT OR REPLACE INTO metadata
        (
            number, title, cover, cover_local,
            release_date, maker, actresses,
            tags, updated_time
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            data.get("number"),
            data.get("title"),
            data.get("cover"),
            data.get("cover_local"),
            data.get("release_date"),
            data.get("maker"),
            json.dumps(data.get("actresses", []), ensure_ascii=False),
            json.dumps(data.get("tags", []), ensure_ascii=False),
            data.get("updated_time", time.time())
        )
        )

        self.conn.commit()
