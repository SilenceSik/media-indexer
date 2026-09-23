"""⚠️ 未接线模块（v2 主线下无引用者）。

`core/scheduler.py` 定时调度器。v2 链路里没有任何地方构造 Scheduler，
`main.py` / `service.py` 也不启动它。保留是因为功能本身可能还需要，
但**它现在不跑**——别误以为扫描是定时自动触发的。

要用的话需要自己接：在入口脚本里构造并 `start()`，
并在 requirements.txt 里解开 `schedule` 的注释。
"""
import schedule
import time


class Scheduler:

    def __init__(self):

        self.jobs = []

    def add_daily(
        self,
        func,
        hour="03:00"
    ):

        schedule.every().day.at(
            hour
        ).do(
            func
        )

    def add_hourly(
        self,
        func
    ):

        schedule.every(
            1
        ).hours.do(
            func
        )

    def run(self):

        while True:

            schedule.run_pending()

            time.sleep(
                5
            )
