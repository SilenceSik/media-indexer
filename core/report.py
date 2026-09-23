import json
import os
import time


class ReportGenerator:

    def __init__(
        self,
        folder="storage/reports"
    ):

        self.folder = folder

        os.makedirs(
            folder,
            exist_ok=True
        )

    def save(
        self,
        data
    ):

        filename = (
            f"audit_{int(time.time())}.json"
        )

        path = os.path.join(
            self.folder,
            filename
        )

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=4
            )

        return path
