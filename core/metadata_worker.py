import time
import json


class MetadataWorker:

    def __init__(
        self,
        queue,
        javdb,
        db
    ):

        self.queue = queue

        self.javdb = javdb

        self.db = db

    def run_once(
        self
    ):

        task = self.queue.get()

        if not task:

            return

        try:

            payload = json.loads(
                task["payload"]
            )

            number = payload["number"]

            data = self.javdb.search(
                number
            )

            if data:

                self.db.save_metadata(
                    data
                )

            self.queue.done(
                task["id"]
            )

        except Exception as e:

            self.queue.fail(
                task["id"],
                str(e)
            )
