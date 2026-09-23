import os
import hashlib
import requests


class ImageCache:

    def __init__(
        self,
        folder
    ):

        self.folder = folder

        os.makedirs(
            folder,
            exist_ok=True
        )

    def filename(
        self,
        url
    ):

        ext = ".jpg"

        if "." in url.split("/")[-1]:

            ext = "." + url.split(".")[-1].split("?")[0]

        name = hashlib.md5(
            url.encode()
        ).hexdigest()

        return name + ext

    def download(
        self,
        url
    ):

        if not url:
            return None

        filename = self.filename(
            url
        )

        path = os.path.join(
            self.folder,
            filename
        )

        if os.path.exists(path):

            return path

        try:

            r = requests.get(
                url,
                timeout=20
            )

            r.raise_for_status()

            with open(
                path,
                "wb"
            ) as f:

                f.write(
                    r.content
                )

            return path

        except Exception:

            return None
