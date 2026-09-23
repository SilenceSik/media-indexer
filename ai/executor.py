import os
import shutil


class Executor:

    def execute(
        self,
        plan
    ):

        action = plan["action"]

        if action == "rename":

            os.rename(
                plan["source"],
                plan["target"]
            )

        elif action == "move":

            os.makedirs(
                os.path.dirname(
                    plan["target"]
                ),
                exist_ok=True
            )

            shutil.move(
                plan["source"],
                plan["target"]
            )

        else:

            raise ValueError(
                "Unknown action"
            )

        return {

            "success":
            True,

            "action":
            action,

            "target":
            plan["target"]

        }
