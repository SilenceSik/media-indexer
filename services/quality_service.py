from core.quality import QualityChecker

from services.result import success


class QualityService:

    def __init__(
        self,
        db
    ):

        self.checker = QualityChecker(
            db
        )

    def report(
        self
    ):

        return success({

            "duplicate_files":

                self.checker.duplicate_files(),

            "missing_files":

                self.checker.missing_files(),

            "missing_metadata":

                self.checker.missing_metadata()

        })
