import os

from core.template import TemplateEngine


class Planner:

    def __init__(
        self,
        config
    ):

        self.engine = TemplateEngine()

        self.config = config

    def create_rename_plan(
        self,
        filepath,
        number
    ):

        folder = os.path.dirname(
            filepath
        )

        ext = os.path.splitext(
            filepath
        )[1]

        new_name = (
            number
            +
            ext
        )

        target = os.path.join(
            folder,
            new_name
        )

        return {

            "action":
            "rename",

            "source":
            filepath,

            "target":
            target,

            "reason":
            "normalize filename"

        }

    def create_move_plan(
        self,
        filepath,
        metadata
    ):

        ext = os.path.splitext(
            filepath
        )[1]

        data = {

            "number":
            metadata.get(
                "number"
            ),

            "title":
            metadata.get(
                "title",
                "Unknown"
            ),

            "maker":
            metadata.get(
                "maker",
                "Unknown"
            ),

            "ext":
            ext

        }

        folder = self.engine.render(
            self.config["folder_template"],
            data
        )

        filename = self.engine.render(
            self.config["filename_template"],
            data
        )

        target = os.path.join(

            self.config["library_path"],

            folder,

            filename

        )

        return {

            "action":
            "move",

            "source":
            filepath,

            "target":
            target,

            "reason":
            "template organize"

        }


def safe_path(path):

    if not os.path.exists(path):

        return path

    base, ext = os.path.splitext(
        path
    )

    index = 1

    while True:

        new = f"{base}_{index}{ext}"

        if not os.path.exists(new):

            return new

        index += 1


def preview_plan(
    plan
):

    return {

        "preview":

        f"""
        操作:
        {plan['action']}

        原文件:
        {plan['source']}

        新位置:
        {plan['target']}

        原因:
        {plan['reason']}
        """

    }
