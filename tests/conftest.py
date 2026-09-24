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


def _clean_env():
    """剥掉测试污染进来的 LMM_*，让子进程从 config.yaml 解析真实路径。"""

    return {k: v for k, v in os.environ.items() if not k.startswith("LMM_")}


@pytest.fixture(autouse=True)
def _restore_lmm_env():
    """每个用例结束后把 LMM_* 恢复成会话开始时的样子。

    快照取自 conftest 的导入时刻，所以连「收集期就写环境变量」那种也兜得住。
    """

    yield

    for key in [k for k in os.environ if k.startswith("LMM_")]:
        del os.environ[key]

    os.environ.update(_PRISTINE_LMM)


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
    """

    code = (
        "import sys; sys.path.insert(0, '.');"
        " from web.app import DB_PATH; print(DB_PATH)"
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
def live_server():
    """独立子进程起 uvicorn，返回 (base_url, 真实库路径)。"""

    db_path = _real_db_path()

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
        env=_clean_env(),
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

        yield base, db_path

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
