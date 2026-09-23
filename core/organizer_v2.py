import os
import shutil
import hashlib

from core.operation_log import OperationLog


class Organizer:

    def __init__(
        self,
        library,
        log_db
    ):

        self.library = library

        self.log = OperationLog(
            log_db
        )

        os.makedirs(
            library,
            exist_ok=True
        )

    def hash_file(
        self,
        path
    ):

        h = hashlib.sha256()

        with open(
            path,
            "rb"
        ) as f:

            for chunk in iter(
                lambda: f.read(1024 * 1024),
                b""
            ):

                h.update(chunk)

        return h.hexdigest()

    def safe_target(
        self,
        path
    ):

        if not os.path.exists(path):

            return path

        base, ext = os.path.splitext(path)

        i = 1

        while True:

            target = f"{base}_{i}{ext}"

            if not os.path.exists(target):

                return target

            i += 1

    def move(
        self,
        source,
        target
    ):

        source_hash = self.hash_file(
            source
        )

        target = self.safe_target(
            target
        )

        self.log.add(
            "move",
            source,
            target,
            source_hash
        )

        os.makedirs(
            os.path.dirname(target),
            exist_ok=True
        )

        shutil.copy2(
            source,
            target
        )

        target_hash = self.hash_file(
            target
        )

        if source_hash != target_hash:

            os.remove(
                target
            )

            raise Exception(
                "hash mismatch"
            )

        os.remove(
            source
        )

        self.log.finish(
            source,
            target,
            target_hash
        )

        return target
