import hashlib


class FileHasher:

    def sha256(
        self,
        path
    ):

        h = hashlib.sha256()

        with open(
            path,
            "rb"
        ) as f:

            while True:

                chunk = f.read(
                    1024 * 1024
                )

                if not chunk:

                    break

                h.update(
                    chunk
                )

        return h.hexdigest()
