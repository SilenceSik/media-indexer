from core.organizer_v2 import Organizer

from services.result import success


class OrganizeService:

    def __init__(
        self,
        library,
        log_db
    ):

        self.organizer = Organizer(
            library,
            log_db
        )

    def move(
        self,
        source,
        target
    ):

        result = self.organizer.move(
            source,
            target
        )

        return success({

            "target":
                result

        })
