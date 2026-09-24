# -*- coding: utf-8 -*-
"""兜底开关的解析。

配置里写 `fallback: "off"` / `"auto"`，WebUI 上还能逐次覆盖。
集中在这里解析，避免各处各自 str() 判断漂移。
"""

# 视为「启用兜底」的取值（大小写不敏感）。
#
# ⚠️ 要容错 YAML 的布尔陷阱：YAML 1.1 把裸写的 `off`/`on`/`yes`/`no`
# 解析成布尔，所以配置里 `fallback: off` 到手是 `False`。
# 这里用 str() 归一后再比，False -> "false" -> 不启用，行为正确。
_ON_VALUES = {"auto", "on", "true", "yes", "1"}

# 视为「明确关闭」的取值
_OFF_VALUES = {"off", "none", "false", "no", "0", ""}


def parse_fallback(value, default=False):
    """配置值 -> 是否启用兜底。

    认不出来的取值按 `default` 处理（不猜）。
    """

    if value is None:
        return default

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in _ON_VALUES:
        return True

    if text in _OFF_VALUES:
        return False

    return default


def fallback_enabled(config):
    """从 config dict 读兜底开关。"""

    return parse_fallback((config or {}).get("fallback"))
