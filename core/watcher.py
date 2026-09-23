"""⚠️ 未接线模块（v2 主线下无引用者）。

`core/watcher.py` 文件系统监听（watchdog）。v2 链路里没有任何地方构造
Observer，`main.py` / `service.py` 也不启动它。保留是因为「目录变动自动感知」
是合理需求，但**它现在不跑**——别误以为丢个文件进库目录就会被自动索引。

要用的话需要自己接：在入口脚本里构造 Observer 并 `start()`，
并在 requirements.txt 里解开 `watchdog` 的注释。
"""
from watchdog.observers import Observer

from watchdog.events import FileSystemEventHandler


class MediaHandler(
    FileSystemEventHandler
):

    def __init__(
        self,
        callback
    ):

        self.callback = callback

    def on_created(
        self,
        event
    ):

        if event.is_directory:

            return

        self.callback(
            event.src_path
        )


class Watcher:

    def __init__(
        self,
        path,
        callback
    ):

        self.path = path

        self.callback = callback

    def start(self):

        observer = Observer()

        observer.schedule(
            MediaHandler(
                self.callback
            ),
            self.path,
            recursive=True
        )

        observer.start()

        return observer
