# -*- coding: utf-8 -*-
"""测试共用夹具与会话级隔离。

## 为什么需要这个文件

后端测试看不见两个只在真浏览器/真进程里显形的东西：整块 `<script>` 被
SyntaxError 炸掉、滑动条 step 吸附、弹窗交互。这些必须用真 Chromium 验。

但把真浏览器和真服务塞进 pytest 进程会踩两个坑，而且都**只在跑全量时**
显形（单跑那个文件永远过），很难查：

### 坑 1：环境变量跨测试泄漏

`test_job_state.py` 在**模块导入期**直接写 `os.environ["LMM_DB"] = <临时路径>`。
pytest 收集阶段就会导入所有测试模块，所以从收集完那一刻起 `LMM_DB` 已经
指向一个空的临时库。不是 monkeypatch，pytest 不会回滚。

后果：任何在进程内重新解析库路径的代码（比如浏览器用例起的服务）都会
去查那个空库，页面 404 回退到首页，症状是 `LB is not defined`。

两道防线：
  - `_PRISTINE_LMM`：conftest 比所有测试模块先导入，在这里快照原始值；
    `_restore_lmm_env` 每个用例结束后把 `LMM_*` 恢复成快照。
  - `_clean_env()`：给子进程传环境时直接剥掉所有 `LMM_*`，让它从
    `config.yaml` 解析真实库路径 —— 不依赖上面那道防线是否生效。

### 坑 2：事件循环被浏览器用例占住

`sync_playwright()` 存活期间会用 greenlet 把主线程的 asyncio 标记成
「正在运行」，退出时才清掉。如果 browser 夹具是 session 级，这个标记从
第一个浏览器用例开始一直挂到整个会话结束，之后任何 `asyncio.run()` 都抛
`RuntimeError: asyncio.run() cannot be called from a running event loop`。

后果：受害者是毫不相干的 `test_size_and_cleanup.py::test_import_rejects_missing_dir`。

修法：browser 夹具降到 **module 级**。playwright 上下文只在某个浏览器
测试模块跑的时候存活，模块跑完立刻关闭、标记清掉，与其他模块永不重叠。

## 缺依赖时的行为

没有 playwright 或本机没有 Chrome 时，只跳过浏览器用例，后端测试照跑。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PYTHON = sys.executable

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# 前端把「大小挡位」换算成真实字节数发给后端，测试要拿同一个基数核对
MB = 1024 * 1024

# conftest 比所有测试模块先导入，此刻的 LMM_* 就是未被污染的原值
_PRISTINE_LMM = {k: v for k, v in os.environ.items() if k.startswith("LMM_")}

# ── 会话级库路径隔离 ────────────────────────────────────────────
#
# 为什么必须在**收集期**就把路径钉死，而不是等夹具：
#
#   `web/app.py` 在**导入期**就跑 `ensure_schema()`。档位迁移
#   （`core.database_v2._retier_from_new_thresholds`）是会**写数据**的，
#   所以测试模块一旦 `from web import app`，那一刻若 LMM_DB 还没设，
#   解析到的就是**生产库** `storage/library_v2.db` —— 跑一次测试就把
#   用户真实库的 tier 按新门槛重算一遍。
#
#   实测（修复前）：复位 `user_version=0`、手改一条 tier，跑全量后
#   真实库变成 `user_version=2`、那条 tier 被改掉。夹具来不及拦，
#   因为导入发生在夹具之前。
#
# 隔离只作用于**测试进程内**：子进程走 `_clean_env()`（剥掉 LMM_*），
# 仍从 config.yaml 解析真实路径 —— 所以浏览器用例起的服务测的还是真库。
_SESSION_TMP = tempfile.mkdtemp(prefix="lmm-tests-")

_ISOLATED_LMM = dict(_PRISTINE_LMM)

# 直接覆盖而不用 setdefault：目标就是「测试绝不碰生产库」，
# 哪怕外部环境故意把 LMM_DB 指向真库，这里也要掰回来。
for _key, _leaf in (
    ("LMM_DB", "library.db"),
    ("LMM_INDEX_DB", "file_index.db"),
    ("LMM_COVERS", "covers"),
    ("LMM_SCREENSHOTS", "screenshots"),
    ("LMM_PREVIEWS", "previews"),
    ("LMM_TORRENTS", "torrents"),
    ("LMM_EXPORTS", "exports"),
):

    _ISOLATED_LMM[_key] = os.path.join(_SESSION_TMP, _leaf)

os.environ.update(_ISOLATED_LMM)


def _clean_env():
    """剥掉测试污染进来的 LMM_*，让子进程从 config.yaml 解析真实路径。"""

    return {k: v for k, v in os.environ.items() if not k.startswith("LMM_")}


@pytest.fixture(autouse=True)
def _restore_lmm_env():
    """每个用例结束后把 LMM_* 恢复到**隔离基线**。

    基线是 `_ISOLATED_LMM`（指向会话临时目录），不是原始快照 ——
    恢复成原始快照会让 LMM_DB 回到未设置状态，下一个用例导入
    `web.app` 时又会解析到生产库。
    """

    yield

    for key in [k for k in os.environ if k.startswith("LMM_")]:
        del os.environ[key]

    os.environ.update(_ISOLATED_LMM)


@pytest.fixture(scope="session")
def pristine_lmm_env():
    """会话最开始的 LMM_* 快照，给守卫用例比对用。"""

    return dict(_PRISTINE_LMM)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=45):
    """等到端口真的能应答（哪怕 404，也说明服务起来了）。"""

    import urllib.error
    import urllib.request

    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True
        except Exception:                                       # noqa: BLE001
            time.sleep(0.3)

    return False


def _real_db_path():
    """子进程用干净环境解析真实库路径。

    刻意不走进程内 `from web.app import DB_PATH`：那些模块会把
    `sys.modules["web.app"]` 换成指向临时库的副本，进程内取到的不可信。

    ⚠️ 也**刻意不导入 `web.app`**：模块级有 `ensure_schema()`，
    而档位迁移会写数据 —— 这里只是想知道路径，不该顺带把真库改了。
    改为把 `web.app` 的两个纯函数抠出来单独执行（同样的 `resolve_path`
    语义，但不触发模块体）。
    """

    # 与 web.app 同源的解析逻辑：BASE_DIR 即仓库根（子进程 cwd=ROOT），
    # 相对路径按它解析 —— 与 `web.app.resolve_path` 逐字一致。
    code = (
        "import os, sys, yaml;"
        "BASE = os.getcwd();"
        "conf = yaml.safe_load(open(os.path.join(BASE, 'config.yaml'), encoding='utf-8'));"
        "p = os.environ.get('LMM_DB') or conf['database'];"
        "print(p if os.path.isabs(p) else os.path.join(BASE, p))"
    )

    proc = subprocess.run(
        [PYTHON, "-c", code],
        cwd=ROOT,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )

    if proc.returncode != 0:
        pytest.fail(
            "子进程解析库路径失败：\n"
            + ((proc.stderr or "") + (proc.stdout or ""))[-2000:]
        )

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]

    if not lines:
        pytest.fail(f"子进程没打印库路径：{(proc.stderr or '')[-2000:]}")

    return lines[-1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """独立子进程起 uvicorn，返回 (base_url, **副本**库路径)。

    ⚠️ 子进程拿到的是**真库的副本**，不是真库本身。

    先前用 `_clean_env()` 让子进程从 config.yaml 解析**真库** —— 当时
    `ensure_schema()` 只补列、幂等无害，所以没事。但档位迁移
    （`_retier_from_new_thresholds`）会**写数据**，于是每跑一次浏览器用例
    就重算一次用户真库的 `tier`。实测：复位 `user_version=0` 后跑全量，
    真库被改回 `user_version=2`。

    改成副本后两边都保住：浏览器用例仍断言**真实语料**（副本内容一致），
    而写入只落在副本上。副本必须是文件拷贝，不能用 `file:...?mode=ro`
    这类只读挂载 —— uvicorn 起的服务要能写（迁移、扫描测试都要）。

    ⚠️ **没有真库时要退回空库**（公开克隆就是这种情况，`storage/` 是
    gitignored）：真库不存在就建一个空的，让服务能起来、页面能渲染。
    用例自己会按内容跳过（`test_detail_browser` 挑不到样本就 skip），
    但**夹具层不能崩** —— 崩了连 skip 都到不了，整个模块报 ERROR。
    """

    import shutil

    real_db = _real_db_path()

    env = _clean_env()

    db_copy = os.path.join(_SESSION_TMP, "livecopy-library.db")

    if os.path.exists(real_db):

        shutil.copyfile(real_db, db_copy)

    else:

        # 空库：服务起得来即可，用例按内容自行跳过。
        # 函数内导入（不在模块级）：避免 conftest 导入期拉起 core 依赖。
        from core.database_v2 import Database

        Database(db_copy).conn.close()

    env["LMM_DB"] = db_copy

    index_real = os.path.join(ROOT, "storage", "file_index_v2.db")

    if os.path.exists(index_real):

        index_copy = os.path.join(_SESSION_TMP, "livecopy-index.db")

        shutil.copyfile(index_real, index_copy)

        env["LMM_INDEX_DB"] = index_copy

    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    # 输出重定向到临时文件而不是管道：管道没人读会填满缓冲区把服务卡死
    log = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".log", delete=False, encoding="utf-8"
    )

    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    try:
        if not _wait_http(base + "/"):
            proc.kill()
            proc.wait(timeout=10)
            log.flush()
            log.seek(0)

            pytest.fail(
                "uvicorn 子进程 45 秒内没起来：\n" + log.read()[-2000:]
            )

        # 仍返回 (base_url, 真库路径)：用例按真库定位样本（**只读**），
        # 而服务写的是副本 —— 所以不该改返回值元数，否则一堆解包要跟着改。
        yield base, real_db

    finally:
        proc.terminate()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

        log.close()

        try:
            os.unlink(log.name)
        except OSError:
            pass


@pytest.fixture(scope="session")
def base_url(live_server):
    return live_server[0]


def _need_browser():
    """缺 playwright / Chrome 就只跳过浏览器用例。"""

    pytest.importorskip("playwright", reason="本机没装 playwright")

    if not os.path.exists(CHROME):
        pytest.skip(f"没找到本机 Chrome：{CHROME}")


@pytest.fixture(scope="module")
def browser():
    """module 级：上下文只在一个测试模块内存活。

    session 级会让 playwright 的「事件循环正在运行」标记挂满整场，
    之后所有 asyncio.run 全炸（见文件顶部坑 2）。已用变异验证：
    退回 session 级会精确复现 test_import_rejects_missing_dir 的
    `asyncio.run() cannot be called from a running event loop`。
    """

    _need_browser()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)

        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    """新页面，并把 JS 报错收集到 `page.js_errors` 上。"""

    pg = browser.new_page()
    pg.js_errors = []
    pg.on("pageerror", lambda e: pg.js_errors.append(str(e)))

    try:
        yield pg
    finally:
        pg.close()
