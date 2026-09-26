# -*- coding: utf-8 -*-
"""测试进程绝不能写用户的**生产库**。

## 为什么单独立一个文件

2026-09-26 实测到的真事故：给 `Database.migrate()` 加了档位重算
（`_retier_from_new_thresholds`，按新门槛重写 `titles.tier`）之后，
**跑一次全量测试就把用户真库改了** —— 复位 `user_version=0` 再跑，
真库被改回 `user_version=2` 且档位被按新口径重算。

根因是两条「导入期就写」的路径：

  1. `web/app.py` 模块级 `ensure_schema()` —— 以前只 `ALTER TABLE` 补列，
     幂等无害；现在是**写数据**。测试模块 `from web import app` 时
     `LMM_DB` 还没设，解析到的就是生产库。
  2. `tests/conftest.py` 的 `live_server` 用 `_clean_env()` 剥掉 `LMM_*`，
     让 uvicorn 子进程解析真库（浏览器用例要断言真语料）。

修法是「隔离 + 副本」，不是「别测真数据」。这个文件负责**看住它别复发**：
把真库的 `mtime` 与指纹记下来，跑完一轮对比。

⚠️ 本文件必须**只读**真库，且自己不能改它 —— 否则守卫本身成了污染源。
"""

import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REAL_DB = os.path.join(ROOT, "storage", "library_v2.db")


def _fingerprint(path):
    """(行数, 档位分布, user_version, mtime_ns)。没语料时返回 None。

    用**内容指纹**而不是只比 mtime：mtime 会被 `touch`、会被同秒写入
    掩盖；档位分布能抓住「被重算过」这件事本身。

    ⚠️ 判据是「**表在**」，不是「文件在」：公开克隆里 `storage/` 是
    gitignored，但测试会创建一个 0 字节的库文件 —— 只看 `exists()`
    会走进 `no such table`。
    """

    if not os.path.exists(path):

        return None

    conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)

    try:

        rows = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]

        dist = tuple(sorted(
            (t, n) for t, n in conn.execute(
                "SELECT tier, COUNT(*) FROM titles GROUP BY tier"
            ).fetchall()
        ))

        ver = conn.execute("PRAGMA user_version").fetchone()[0]

    except sqlite3.OperationalError:

        return None                       # 不是一份语料

    finally:

        conn.close()

    return (rows, dist, ver, os.stat(path).st_mtime_ns)


def _has_corpus(path):
    """真库在**且是语料**（有 titles 表）。"""

    return _fingerprint(path) is not None


@pytest.fixture(scope="session")
def real_db_fingerprint():
    """会话开始时真库的指纹。"""

    if not _has_corpus(REAL_DB):

        pytest.skip("本机没有可用语料库，守卫用例跳过")

    return _fingerprint(REAL_DB)


def test_importing_web_app_does_not_touch_real_db(real_db_fingerprint):
    """导入 `web.app`（`ensure_schema()` 在模块级）不能改动真库。

    这条直击事故根因 1：测试模块只要 `from web import app`，
    那一刻若 LMM_DB 未设，就解析到生产库。
    """

    # conftest 已在收集期把 LMM_* 钉到会话临时目录，这里断言它生效了
    assert os.environ.get("LMM_DB"), (
        "conftest 没在收集期设置 LMM_DB —— 导入 web.app 会打到生产库"
    )

    assert os.path.realpath(os.environ["LMM_DB"]) != os.path.realpath(REAL_DB), (
        "测试环境的 LMM_DB 指向了生产库"
    )

    import web.app                                                    # noqa: F401

    assert _fingerprint(REAL_DB) == real_db_fingerprint, (
        "导入 web.app 改动了生产库（导入期写操作又回来了）"
    )


def test_no_test_env_points_at_real_db():
    """所有 LMM_* 路径都不得指向仓库的 storage/。

    比单看 LMM_DB 更狠：覆盖 covers / screenshots / torrents / exports，
    防止哪天有人把其中某一个改回真目录。
    """

    offenders = []

    for key, val in os.environ.items():

        if not key.startswith("LMM_") or not val:

            continue

        if os.path.realpath(val) == os.path.realpath(REAL_DB):

            offenders.append((key, val))

        elif os.path.realpath(os.path.dirname(val)) == \
                os.path.realpath(os.path.join(ROOT, "storage")):

            offenders.append((key, val))

    assert not offenders, f"这些 LMM_* 指向了仓库 storage/：{offenders}"


def test_live_server_writes_to_copy_not_real_db():
    """`live_server` 的子进程必须指向副本（事故根因 2）。

    不真起服务（慢），只核对夹具的构造意图：它读真库、写副本。
    """

    import inspect

    from tests import conftest

    src = inspect.getsource(conftest.live_server)

    assert "copyfile" in src, "live_server 没有拷贝真库"
    assert 'env["LMM_DB"]' in src, "live_server 没把子进程指向副本"
    assert "real_db" in src


def test_real_db_hash_is_stable_across_this_file(real_db_fingerprint):
    """自证：本文件跑完没碰真库（守卫本身不能是污染源）。"""

    assert _fingerprint(REAL_DB) == real_db_fingerprint
