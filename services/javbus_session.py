# -*- coding: utf-8 -*-
"""JavBus 兜底源的**按需启停**管理。

设计取舍（主人 2026-09-24 定：走方案 2「按需起停」）：

  * javbus-api 是一个 Node 服务（约 90MB 常驻）。而它在这里只用于
    **javdb 查不到时的兜底** —— 一条罕见路径。
  * 为罕见路径常驻 90MB 不划算，所以：**平时不跑**，需要时起，用完杀。
  * 服务不在线时**跳过兜底并记一笔**，绝不自动拉起（单条抓取时拉起
    一个 Node 服务会让那条请求莫名其妙多等几秒）。

什么时候起：**批量抓取开始前**起一次，整批用完再杀。
单条兜底时若没在跑 —— 就跳过（`available()` 为假）。

生命周期由 `with javbus_session():` 管，异常路径也会收尾。
"""

import os
import shutil
import socket
import subprocess
import time

HOST = "127.0.0.1"
PORT = 8922

# javbus-api 的部署位置。
#
# 默认按平台惯例猜一个，但**允许用环境变量覆盖** —— 每个人的安装路径
# 都不一样，写死等于只有原作者能跑。
APP_DIR = os.environ.get("LMM_JAVBUS_API_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "javbus-api",
)

# node 可执行文件：先看 PATH，再退回常见安装位置
NODE = os.environ.get("LMM_NODE") or shutil.which("node") or "node"


def port_open(host=HOST, port=PORT, timeout=0.4):
    """端口上有人在听吗。"""

    try:

        with socket.create_connection((host, port), timeout=timeout):
            return True

    except OSError:

        return False


def find_server_pid(port=PORT):
    """找出**监听该端口**的进程 PID（没有则 None）。

    ⚠️ 不要用 `wmic` 按命令行匹配：这台机器上 **wmic 已被移除**
    （`FileNotFoundError: [WinError 2]`），而它被 `except OSError` 吞掉后
    会**静默**返回 None —— 结果 `_stop()` 永远杀不掉进程，方案 2 直接失效。

    改成问「谁占着这个端口」：语义更准（我们要的就是占用者），
    也不依赖命令行内容的匹配规则。
    """

    try:

        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, timeout=25,
        ).stdout.decode("utf-8", "replace")

    except (OSError, subprocess.SubprocessError):

        return None

    for line in out.splitlines():

        parts = line.split()

        if len(parts) < 5:
            continue

        # 形如：TCP  0.0.0.0:8922  0.0.0.0:0  LISTENING  30652
        if not parts[0].upper().startswith("TCP"):
            continue

        if not parts[1].endswith(":{}".format(port)):
            continue

        if parts[3].upper() != "LISTENING":
            continue

        if parts[4].isdigit():
            return int(parts[4])

    return None


class JavBusSession:
    """按需启停。用作上下文管理器。

        with JavBusSession(enabled=True) as session:
            if session.ready:
                ...   # 走兜底
    """

    def __init__(self, enabled=True, app_dir=APP_DIR, wait=25.0):
        self.enabled = enabled
        self.app_dir = app_dir
        self.wait = wait

        self.started_by_us = False
        self.ready = False
        self.note = ""

    # ------------------------------------------------------------ 内部

    def _start(self):
        """起服务，等端口可用。"""

        server = os.path.join(self.app_dir, "dist", "server.js")

        if not os.path.exists(server):
            self.note = "javbus-api 未部署：{}".format(server)
            return False

        if not os.path.exists(NODE):
            self.note = "找不到 node：{}".format(NODE)
            return False

        try:

            # 把我们的代理设置传进去。
            #
            # ⚠️ 服务自己是靠 `.env` 里的 HTTP_PROXY 走代理的，而
            # **端口不该由软件预设** —— 使用者在设置页填的地址要能生效。
            # `dotenv` 默认**不覆盖**已存在的环境变量，所以我们传进去的
            # 会盖过它 `.env` 里的值；没填时就不传，让它按自己的配置走。
            env = dict(os.environ)

            from adapters.javbus_adapter import resolve_proxy

            proxy = resolve_proxy()

            if proxy:
                env["HTTP_PROXY"] = proxy
                env["HTTPS_PROXY"] = proxy

            # 隐藏窗口后台起，与 vbs 启动器等效
            subprocess.Popen(
                [NODE, "-r", "dotenv/config", "dist/server.js"],
                cwd=self.app_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        except OSError as exc:

            self.note = "起服务失败：{}".format(exc)
            return False

        deadline = time.time() + self.wait

        while time.time() < deadline:

            if port_open():
                return True

            time.sleep(0.5)

        self.note = "服务起了但 {:.0f}s 内端口未就绪".format(self.wait)

        return False

    def _stop(self):
        """杀掉我们自己起的那个进程。"""

        pid = find_server_pid()

        if not pid:
            return

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=20,
        )

    def start(self):
        """显式启动（等价于进入上下文）。返回 self。"""

        return self.__enter__()

    def stop(self):
        """显式收尾（等价于退出上下文）。可重复调用。"""

        return self.__exit__(None, None, None)

    # ------------------------------------------------------------ 上下文

    def __enter__(self):

        if not self.enabled:
            self.note = "兜底未启用"
            return self

        # native 后端**直接抓网页，不需要这个 Node 服务** ——
        # 起了也只是白占 90MB，还可能与使用者的其它实例抢端口。
        try:

            from adapters.javbus_adapter import resolve_backend

            if resolve_backend() == "native":
                self.ready = True
                self.started_by_us = False
                self.note = "后端为 native，跳过 javbus-api 服务"
                return self

        except Exception:                                      # noqa: BLE001
            pass

        if port_open():
            # 已经有人在跑（也许是开机自启）—— 不是我们起的，就别杀
            self.ready = True
            self.started_by_us = False
            self.note = "服务已在线（非本次启动，结束后不关）"
            return self

        self.ready = self._start()
        self.started_by_us = self.ready

        if self.ready:
            self.note = "已按需启动"

        return self

    def __exit__(self, exc_type, exc, tb):

        if self.ready and self.started_by_us:

            self._stop()

        # 幂等：重复调用不会重复杀
        self.ready = False
        self.started_by_us = False
        self.note = ""

        return False
