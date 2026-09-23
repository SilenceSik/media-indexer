import os

from core.file_index import rules_fingerprint
from core.scanner_v2 import Scanner
from core.parser_v2 import Parser

from services.result import success


class ScanService:

    def __init__(
        self,
        index_db,
        rules,
        db=None,
        extensions=None,
        excluded_segments=None
    ):

        # 规则集指纹写进 file_index：字典一变，已索引的文件也算 changed
        self.rules_version = rules_fingerprint(
            rules
        )

        # extensions：自定义追加的扩展名（内置标准格式由 Scanner 叠加，
        # 见 core/scanner_v2.resolve_extensions 的「追加语义」说明）
        self.scanner = Scanner(
            index_db,
            rules_version=self.rules_version,
            extensions=extensions,
            excluded_segments=excluded_segments
        )

        self.parser = Parser(
            rules
        )

        self.db = db

    def scan(
        self,
        folder,
        persist=True
    ):

        """
        persist=False 是**真·干跑**：

        - 只返回解析结果，不写库
        - 也不推进 file_index（update_index=persist），所以紧接着的
          persist=True 仍能看到这批文件并落库
        - 不会出现「先干跑一眼，再真跑就静默空转」的陷阱
        """

        files = self.scanner.scan(
            folder,
            update_index=persist
        )

        results = []

        for file in files:

            numbers = self.parse_file(
                file
            )

            persisted = False

            if persist and self.db:

                best = self.best_number(
                    numbers
                )

                if best:

                    self.persist_file(
                        best,
                        file
                    )

                    persisted = True

            results.append(
                {

                    "file":
                        file,

                    "numbers":
                        numbers,

                    "persisted":
                        persisted

                }
            )

        return success(
            results
        )

    def parse_file(
        self,
        filepath
    ):

        """
        先解析文件名（basename）；文件名无命中时，回退到**直接父目录名**。

        传进 Parser 的绝不再是完整路径：目录名不再无条件参与匹配
        （否则 `X:/ABP-999/random_clip.mp4` 这类会被祖先目录的番号劫持，
        实测误判并入库）。只保留「番号目录/video.mp4」这一层回退。
        """

        numbers = self.parser.parse(
            os.path.basename(
                filepath
            )
        )

        if numbers:

            return numbers

        parent = os.path.basename(
            os.path.dirname(
                filepath
            )
        )

        if not parent:

            return numbers

        return self.parser.parse(
            parent
        )

    def best_number(
        self,
        numbers
    ):

        """
        多个候选番号取 confidence 最高的那一条，返回番号字符串
        """

        if not numbers:

            return None

        return max(

            numbers,

            key=lambda item: item.get(
                "confidence",
                0
            )

        )["number"]

    def persist_file(
        self,
        number,
        filepath
    ):

        """
        落库。title 的取用/新建全部交给 add_file（UPSERT）：

        - 同一 filepath 番号未变 → 复用现有 title（不新建）
        - 番号变了（字典修正后重扫）→ 重链到新 title，旧关联不再挂死
        """

        return self.db.add_file(
            number,
            filepath,
            size=self.file_size(
                filepath
            )
        )

    def file_size(
        self,
        filepath
    ):

        try:

            return os.path.getsize(
                filepath
            )

        except OSError:

            return None
