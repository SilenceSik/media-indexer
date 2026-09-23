import time


class RetryPolicy:

    def __init__(
        self,
        max_retry=3
    ):

        self.max_retry = max_retry

    def should_retry(
        self,
        count
    ):

        return count < self.max_retry

    def delay(
        self,
        count
    ):

        return 2 ** count
