from services.result import success


class MetadataService:

    def __init__(
        self,
        queue
    ):

        self.queue = queue

    def request(
        self,
        number
    ):

        self.queue.add(
            "metadata",
            {
                "number":
                number
            }
        )

        return success({

            "queued":
                True,

            "number":
                number

        })
