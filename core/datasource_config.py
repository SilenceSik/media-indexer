# -*- coding: utf-8 -*-
"""数据源后端与代理的配置读取（`config.yaml`）。

## 为什么要单独一个模块

`JavDBCLIClient` / `JavBusClient` 在 7 处被构造（enrich_service 1 处、
web/app 6 处）。如果让每个调用点自己去读配置再传进来，等于把「读哪几个键、
默认值是什么、环境变量优先级」复制 7 份 —— 迟早不一致。

所以让客户端**自己读**，调用点一行不用改。

## 优先级（高 -> 低）

1. 构造参数（`backend=` / `proxy=`）—— 测试与临时覆盖用
2. 环境变量（`LMM_JAVDB_BACKEND` / `LMM_JAVBUS_BACKEND` /
   `LMM_JAVBUS_PROXY` / `LMM_JAVDB_PROXY`）—— 不改文件就能试验
3. `storage/settings.json` —— **WebUI 可改**（见 core.runtime_settings）
4. `config.yaml` —— 部署级、手改、带完整注释
5. 内置默认

## 为什么代理必须由使用者自己配

JavBus 在国内**直连超时**（实测），必须走代理。而每个人机器上的代理端口
都不一样（7890 / 7891 / 10809 / 7897 …），**软件不该预设任何端口** ——
所以这里只接受一个完整地址，端口由使用者在 UI 或配置里填。

留空 = 用系统环境变量（requests 的 `trust_env` 行为）；那也没有时，
`JavBusClient.available()` 会返回 False，上层据此给出「未配置代理」的
提示，而不是干等超时。

## 读不到配置怎么办

**静默回落内置默认**，不抛异常。理由：这两个客户端在「WebUI 起来时」
就会被构造，而配置缺失/损坏不该让整个服务起不来 —— 数据源后端不是
启动的必要条件。
"""

import os

# 项目根（本文件在 core/ 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CACHE = {"loaded": False, "config": {}}


def _load():
    """读 config.yaml，**只读一次**并缓存。失败返回 {}。"""

    if _CACHE["loaded"]:
        return _CACHE["config"]

    _CACHE["loaded"] = True

    try:

        import yaml

        path = os.path.join(_ROOT, "config.yaml")

        with open(path, encoding="utf-8") as fh:
            _CACHE["config"] = yaml.safe_load(fh) or {}

    except Exception:

        _CACHE["config"] = {}

    return _CACHE["config"]


def reload_config():
    """清缓存（测试用）。"""

    _CACHE["loaded"] = False
    _CACHE["config"] = {}


def get_backend(kind, env_var, allowed, default):
    """取某个数据源的后端名。

    `kind` 是配置键前缀（`javdb` / `javbus`），实际读 `<kind>_backend`。
    """

    # 环境变量优先
    from_env = (os.environ.get(env_var) or "").strip().lower()

    if from_env in allowed:
        return from_env

    key = "{}_backend".format(kind)

    # WebUI 改过的值（settings.json）优先于 config.yaml
    from core.runtime_settings import get as get_setting

    ui_value = str(get_setting(key, "") or "").strip().lower()

    if ui_value in allowed:
        return ui_value

    value = str(_load().get(key) or "").strip().lower()

    return value if value in allowed else default


def get_proxy(kind, env_var):
    """取某个数据源的代理地址。空串 = 用系统环境变量（requests 默认行为）。

    **软件不预设任何端口** —— 这里只读使用者填的完整地址
    （如 `http://127.0.0.1:7890`），端口完全由使用者决定。
    """

    from_env = (os.environ.get(env_var) or "").strip()

    if from_env:
        return from_env

    key = "{}_proxy".format(kind)

    from core.runtime_settings import get as get_setting

    ui_value = str(get_setting(key, "") or "").strip()

    if ui_value:
        return ui_value

    return str(_load().get(key) or "").strip() or None


def proxy_configured(kind, env_var):
    """代理是否已配置。没配时上层该给提示，而不是干等超时。"""

    return bool(get_proxy(kind, env_var))
