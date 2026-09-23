import json
import os

from core.matcher_v2 import NumberMatcher
from core.normalizer import Normalizer


class Parser:

    def __init__(
        self,
        rules
    ):

        self.matcher = NumberMatcher(
            self._load_rules(rules)
        )

    def parse(
        self,
        filename
    ):

        # 只吃文件名，不吃路径。
        #
        # 上游（扫描器）递进来的是完整路径，而 Normalizer 不剥目录——于是
        # 目录名会一起进匹配，后果是**只出假阳性、不出真阳性**（均为实测）：
        #   临时目录 pytest-24            → 假番号 PYTEST-24
        #   媒体根/ABP-999/random_clip.mp4 → 目录里的真番号反而不命中
        # 所以这里在引擎边界统一归一，一次修掉所有调用方（v1 service.py /
        # v2 scan_service.py）。真实文件名形如 ABP-999.mp4，本就带番号；
        # 目录名只是容器，不参与识别。
        filename = os.path.basename(
            filename
        )

        clean = Normalizer.clean(
            filename
        )

        return self.matcher.match(
            clean
        )

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _load_rules(rules):
        """``rules`` 兼容三种形态；``parse()`` 返回形态不变（下游 C1/C4 依赖）。

        1. ``list``  —— 现成规则列表（框架原有形态，C1/C4 传的就是这个）
        2. ``str``   —— ``dictionary.json`` 路径（``config.yaml`` 的
           ``dictionary: "data/dictionary.json"`` 就是这个形态）
        3. ``dict``  —— 已解析的 ``{"rules": [...]}``

        兼容 2/3 是为了让「入口层切到 v2」时能直接把 ``config["dictionary"]``
        接进来（v1 的 ``NumberParser`` 收的就是路径）。
        """

        if rules is None:
            return []

        if isinstance(rules, dict):
            return rules.get("rules", [])

        if isinstance(rules, str):
            with open(rules, encoding="utf-8") as f:
                return json.load(f).get("rules", [])

        return list(rules)
