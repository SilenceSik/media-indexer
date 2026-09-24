# -*- coding: utf-8 -*-
"""运行时设置（WebUI 可改）—— 存在 `storage/settings.json`。

## 为什么不让 UI 直接改 `config.yaml`

`config.yaml` 是**给手改的**，里面大段注释本身就是文档（扫描范围策略、
兜底的取舍理由、并发度的实测数据……）。YAML 往返一次这些注释就全没了 ——
而 UI 只该改几个标量，不该承担「保住整份文档」的责任。

所以分两层：

    config.yaml        部署级配置，手改，带完整注释（**UI 不碰**）
    settings.json      运行时设置，UI 可改，只放少数字段

## 生效优先级（高 -> 低）

    构造参数 > 环境变量 > settings.json > config.yaml > 内置默认

`settings.json` 压在 `config.yaml` 之上：UI 改过的值要能盖住文件里的默认。

## 只允许写白名单里的键

不开放任意键 —— 免得 UI 变成「什么都能改」，把一个改错就难查的值
（比如数据库路径）暴露出去。加键要有明确理由。
"""

import json
import os

# 项目根（本文件在 core/ 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PATH = os.path.join(_ROOT, "storage", "settings.json")

# UI 可改的键。加键前先想清楚「改错了会怎样」。
EDITABLE_KEYS = (
    "javdb_backend",      # cli | native
    "javbus_backend",     # service | native
    "javbus_proxy",       # 如 http://127.0.0.1:7890（端口由使用者定）
    "javdb_proxy",
)

# 默认值（settings.json 里没有、config.yaml 里也没有时用）
_DEFAULTS = {
    "javdb_backend": "cli",
    "javbus_backend": "service",
    "javbus_proxy": "",
    "javdb_proxy": "",
}

_CACHE = {"loaded": False, "data": {}}


def path():
    return _PATH


def load():
    """读 settings.json。文件不存在/损坏都返回 {}（**不抛**）。"""

    if _CACHE["loaded"]:
        return _CACHE["data"]

    _CACHE["loaded"] = True

    try:

        with open(_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)

        _CACHE["data"] = raw if isinstance(raw, dict) else {}

    except Exception:

        _CACHE["data"] = {}

    return _CACHE["data"]


def reload_settings():
    """清缓存（测试与保存后调用）。"""

    _CACHE["loaded"] = False
    _CACHE["data"] = {}


def get(key, default=None):
    """取一个键。只认白名单里的键。"""

    if key not in EDITABLE_KEYS:
        return default

    value = load().get(key)

    if value is None:
        return _DEFAULTS.get(key, default)

    return value


def save(values):
    """保存若干键。返回 (ok, 说明)。只写白名单里的键，其余忽略。

    写入是**整体覆盖**：先读现有的，合并，再落盘 —— 不会把没提到的键抹掉。
    """

    if not isinstance(values, dict):
        return False, "values 必须是对象"

    unknown = [k for k in values if k not in EDITABLE_KEYS]

    if unknown:
        return False, "不支持的键：{}".format(", ".join(sorted(unknown)))

    current = dict(load())

    for key, value in values.items():

        if value is None:
            current.pop(key, None)
        else:
            current[key] = str(value).strip()

    try:

        os.makedirs(os.path.dirname(_PATH), exist_ok=True)

        # 先写临时文件再替换 —— 中途失败不会留下半个文件
        tmp = _PATH + ".tmp"

        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

        os.replace(tmp, _PATH)

    except OSError as exc:

        return False, "写入失败：{}".format(exc)

    reload_settings()

    return True, "已保存"


def snapshot():
    """当前所有可改键的**生效值**（含来源），给 UI 显示用。"""

    from core.datasource_config import _load as load_yaml

    yml = load_yaml()
    js = load()

    out = {}

    for key in EDITABLE_KEYS:

        js_value = js.get(key)

        if isinstance(js_value, str) and js_value.strip():
            out[key] = {"value": js_value, "source": "settings.json"}
            continue

        yml_value = yml.get(key)

        if isinstance(yml_value, str) and yml_value.strip():
            out[key] = {"value": yml_value, "source": "config.yaml"}
            continue

        out[key] = {"value": _DEFAULTS.get(key, ""), "source": "默认"}

    return out
