import json
import os
import time


class Learner:

    def __init__(
        self,
        path
    ):

        self.path = path

        if not os.path.exists(path):

            with open(
                path,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    {
                        "candidates": []
                    },
                    f
                )

    def add(
        self,
        candidate
    ):

        with open(
            self.path,
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        for item in data["candidates"]:

            if item["code"] == candidate:

                item["count"] += 1

                item["last_seen"] = time.time()

                break

        else:

            data["candidates"].append(
                {
                    "code": candidate,
                    "count": 1,
                    "last_seen": time.time()
                }
            )

        with open(
            self.path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=4
            )
