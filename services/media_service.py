from core.database_v2 import Database

from services.result import success


class MediaService:

    def __init__(
        self,
        db_path
    ):

        self.db = Database(
            db_path
        )

    def search(
        self,
        number
    ):

        return success(
            self.db.search_number(
                number
            )
        )

    def add_file(
        self,
        number,
        filepath
    ):

        self.db.add_file(
            number,
            filepath
        )

        return success({

            "number":
                number,

            "filepath":
                filepath

        })
