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
        persist=True,
        min_conf=0,
        max_conf=100,
        min_size=0,
        max_size=0,
        on_size_skip=None,
        on_discovered=None
    ):

        """
        persist=False 是**真·干跑**：

        - 只返回解析结果，不写库
        - 也不推进 file_index（update_index=persist），所以紧接着的
          persist=True 仍能看到这批文件并落库
        - 不会出现「先干跑一眼，再真跑就静默空转」的陷阱

        min_conf / max_conf 是**置信度门槛**：只有 confidence 落在区间内的
        候选才写库。默认 0-100 即「全都收」。

        为什么需要它：识别规则是启发式的，`kcf9.com-3 (1)_(new)_amq13.mp4`
        会被 `P_STD` 当成 `AMQ-13`（conf 70，厂牌未收录）。没有门槛时
        这类猜测照样入库，用户看到的就是"错误匹配"。

        min_size / max_size 是**文件大小门槛**（字节，0 = 不限制）。
        用来排掉预告片/花絮这类小文件，或只看正片。
        """

        files = self.scanner.scan(
            folder,
            update_index=persist,
            min_size=min_size,
            max_size=max_size,
            on_size_skip=on_size_skip,
        )

        # 目录已经走完、待处理清单已定 —— 这时候总数才是真的。
        #
        # 有了这个回调，调用方**不必再预先 os.walk 一遍**去数总数
        # （实测：对 G 盘那种 70 万文件的盘，白走一遍要多花好几分钟，
        #  而它的产出只有 3 个番号）。
        if on_discovered:

            on_discovered(len(files))

        results = []

        for file in files:

            numbers = self.parse_file(
                file
            )

            persisted = False

            skipped = None

            if persist and self.db:

                best = self.best_entry(
                    numbers
                )

                if best:

                    if min_conf <= best["confidence"] <= max_conf:

                        self.persist_file(
                            best["number"],
                            file,
                            source=best.get("source"),
                            confidence=best.get("confidence"),
                        )

                        persisted = True

                    else:

                        # 记下为什么没落库 —— 否则用户只看到"扫到了但没进库"
                        skipped = {
                            "number": best["number"],
                            "confidence": best["confidence"],
                            "reason": f"置信度 {best['confidence']} 不在"
                                      f" {min_conf}-{max_conf} 区间内",
                        }

            results.append(
                {

                    "file":
                        file,

                    "numbers":
                        numbers,

                    "persisted":
                        persisted,

                    "skipped":
                        skipped

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

    def best_entry(
        self,
        numbers
    ):

        """
        多个候选番号取 confidence 最高的那一条，返回**整个候选**（含
        confidence），供落库门槛判断。取不到返回 None。
        """

        if not numbers:

            return None

        return max(

            numbers,

            key=lambda item: item.get(
                "confidence",
                0
            )

        )

    def best_number(
        self,
        numbers
    ):

        """
        多个候选番号取 confidence 最高的那一条，返回番号字符串
        """

        best = self.best_entry(
            numbers
        )

        return best["number"] if best else None

    def persist_file(
        self,
        number,
        filepath,
        source=None,
        confidence=None
    ):

        """
        落库。title 的取用/新建全部交给 add_file（UPSERT）：

        - 同一 filepath 番号未变 → 复用现有 title（不新建）
        - 番号变了（字典修正后重扫）→ 重链到新 title，旧关联不再挂死

        ``source`` / ``confidence`` 来自 matcher，记下来供 D11 第三条判
        「识别是否来自高置信主路径」（批量删的门槛之一）。
        """

        return self.db.add_file(
            number,
            filepath,
            size=self.file_size(
                filepath
            ),
            match_source=source,
            match_confidence=confidence,
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
