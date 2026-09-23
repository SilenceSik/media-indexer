import re


class Normalizer:

    @staticmethod
    def clean(text: str) -> str:
        """
        标准化文件名
        """

        text = text.upper()

        # 全角符号
        replace_map = {
            "－": "-",
            "—": "-",
            "_": "-",
            " ": "-"
        }

        for old, new in replace_map.items():
            text = text.replace(old, new)

        # 删除网址
        text = re.sub(
            r"https?://\S+",
            "",
            text
        )

        # 删除常见广告格式
        text = re.sub(
            r"WWW\.\S+",
            "",
            text
        )

        return text
