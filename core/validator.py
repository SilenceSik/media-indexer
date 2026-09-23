import json
import re


class Validator:

    def __init__(
        self,
        blacklist
    ):

        with open(
            blacklist,
            encoding="utf-8"
        ) as f:

            self.words = json.load(f)["words"]

    def valid(
        self,
        value
    ):

        upper = value.upper()

        for word in self.words:

            if word in upper:

                return False

        # 必须包含数字

        if not re.search(
            r"\d",
            upper
        ):

            return False

        # 太短排除

        if len(upper) < 4:

            return False

        return True
