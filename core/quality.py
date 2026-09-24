import sqlite3


class QualityChecker:

    def __init__(
        self,
        db
    ):

        self.conn = sqlite3.connect(
            db
        )

    def duplicate_files(
        self
    ):

        rows = self.conn.execute(
        """
        SELECT

        file_hash,

        COUNT(*)

        FROM media_files

        WHERE file_hash IS NOT NULL

        -- 已送回收站的不算重复（那个副本已经不在了）
        AND COALESCE(local_deleted, 0) = 0

        GROUP BY file_hash

        HAVING COUNT(*)>1

        """
        ).fetchall()

        return [

            {

                "hash": x[0],

                "count": x[1]

            }

            for x in rows

        ]

    def missing_files(
        self
    ):

        import os

        rows = self.conn.execute(
        """
        SELECT

        id,

        filepath

        FROM media_files

        -- 已送回收站的是**故意删的**，不是「丢失」，别混进来
        WHERE COALESCE(local_deleted, 0) = 0

        """
        ).fetchall()

        result = []

        for row in rows:

            if not os.path.exists(
                row[1]
            ):

                result.append(
                    {

                        "id":
                            row[0],

                        "filepath":
                            row[1]

                    }
                )

        return result

    def missing_metadata(
        self
    ):

        """
        缺元数据的番号。

        口径：只统计「真有文件挂在上面」的 title（EXISTS media_files），
        且没有 metadata 行。

        旧口径 `id NOT IN (SELECT title_id FROM metadata)` 会把没有任何
        文件引用的孤儿 title 也算进来（C1b 实测：同一片子的新旧两个番号
        一起报出来，用户无法分辨哪个真实存在）。
        """

        rows = self.conn.execute(
        """

        SELECT

        titles.number

        FROM titles

        WHERE EXISTS (

        SELECT 1 FROM media_files

        WHERE media_files.title_id = titles.id

        AND COALESCE(media_files.local_deleted, 0) = 0

        )

        AND NOT EXISTS (

        SELECT 1 FROM metadata

        WHERE metadata.title_id = titles.id

        )

        """
        ).fetchall()

        return [

            x[0]

            for x in rows

        ]
