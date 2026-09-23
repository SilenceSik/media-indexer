import requests
import os


class Downloader:

    def download(
        self,
        url,
        target
    ):

        if os.path.exists(
            target
        ):

            return target

        response = requests.get(
            url,
            timeout=30
        )

        response.raise_for_status()

        with open(
            target,
            "wb"
        ) as f:

            f.write(
                response.content
            )

        return target
