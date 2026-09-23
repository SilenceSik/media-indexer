import json
import subprocess


class JavDBAdapter:

    def __init__(
        self,
        client
    ):

        self.client = client

    def search(
        self,
        number
    ):

        result = self.client.search(
            number
        )

        if not result:

            return None

        return {

            "number":
            number,

            "title":
            result.get(
                "title"
            ),

            "cover":
            result.get(
                "cover"
            ),

            "actresses":
            result.get(
                "actresses",
                []
            ),

            "tags":
            result.get(
                "tags",
                []
            ),

            "maker":
            result.get(
                "maker"
            )

        }


class JavDBCLIClient:

    def __init__(
        self,
        command="javdb"
    ):

        self.command = command

    def search(
        self,
        number
    ):

        try:

            result = subprocess.run(
                [
                    self.command,
                    "search",
                    number,
                    "--json"
                ],

                capture_output=True,

                text=True,

                timeout=30
            )

            if result.returncode != 0:

                return None

            return json.loads(
                result.stdout
            )

        except Exception:

            return None
