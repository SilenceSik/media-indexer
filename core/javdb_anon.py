# -*- coding: utf-8 -*-
"""隔离的匿名执行环境（并发抓取用）。

主人 2026-09-24 定的方案：**磁力查询不需要登录态**（实测确认），
所以并发跑时**隔离登录状态**即可 —— 既避开「并发写坏 auth.json」，
也避开「登录账号被风控」。

实测（2026-09-24，隔离 HOME、无 auth.json）：

    并发 6   18 个番号   9.6s   4.4x   失败 0
    并发 12  30 个番号   6.5s  10.8x   失败 1（番号不存在，非限流）

单条中位耗时 2.13s 与串行一致 —— 没有排队、没有限流迹象。

## 为什么不每线程一个 HOME

每个空 HOME 都会生成自己的 `device_uuid`。一堆不同 device_uuid 从同一个
出口 IP 发请求，看起来**更像**异常而不是更安全。
所以：**建一个共享的隔离 HOME**，所有 worker 复用它 ——
对外是「一台设备、未登录」，这是最正常的形态。
"""

import os
import shutil
import tempfile

# 隔离 HOME 的落点（放项目 storage 下，跟库一起管理）
DEFAULT_HOME = None


def ensure_isolated_home(base=None):
    """准备一个**没有登录态**的 HOME，返回路径。

    已存在就直接复用（保留 device_uuid / route.json，避免每次换身份）。
    """

    base = base or DEFAULT_HOME

    if not base:

        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "storage",
            "javdb_anon_home",
        )

    os.makedirs(base, exist_ok=True)

    return base


def env_for(home):
    """造一份指向该 HOME 的环境变量。

    Windows 上 `javdb` 认 `USERPROFILE`，类 Unix 认 `HOME` —— 两个都设。
    """

    env = dict(os.environ)

    env["HOME"] = home
    env["USERPROFILE"] = home

    return env


def has_login(home):
    """这个 HOME 里有没有登录态（有就说明隔离没做干净）。"""

    return os.path.exists(os.path.join(home, ".javdb-cli", "auth.json"))


def purge_login(home):
    """删掉隔离 HOME 里的登录态。

    正常情况下它压根不会生成（我们从不调用 auth login）；
    但若被人手动登录过，这里兜一下。
    """

    p = os.path.join(home, ".javdb-cli", "auth.json")

    if os.path.exists(p):
        os.remove(p)
        return True

    return False
