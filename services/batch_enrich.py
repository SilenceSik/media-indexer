# -*- coding: utf-8 -*-
"""并发批量抓取。

主人 2026-09-24 定的方案：**磁力查询不需要登录态**（实测确认），
并发时**隔离登录状态**即可 —— 既不会并发写坏 `auth.json`，
也没有登录账号可被风控。

实测（隔离 HOME、无 auth.json）：

    并发 6   18 个番号   9.6s   4.4x   失败 0
    并发 12  30 个番号   6.5s  10.8x   失败 1（番号不存在，非限流）
    单条中位 2.13s，与串行一致 —— 无排队、无限流迹象

## 两个必须守住的点

1. **每个线程自己的数据库连接**。SQLite 连接默认
   `check_same_thread=True`，跨线程用会直接抛错。所以每个 worker 自建
   `Database`，不共享。
2. **失败要能分类**：番号不存在（正常）与网络/限流（要退避）不是一回事，
   混在一起会把「番号本来就没有」误判成「被限流了」。

## 为什么默认 6 而不是 12

12 实测也没问题，但那是**短时间小批量**。批量抓几百个时，
长时间高频更可能触发风控。6 的加速比（4.4x）已经足够，
留一半余量给「更快失败要能刹住」。
"""

import concurrent.futures as cf
import os
import threading
import time

# 默认并发度。见模块 docstring 里「为什么默认 6」。
DEFAULT_WORKERS = 6

# 番号不存在时 CLI 的返回特征 —— 这类**不算失败**，是正常的「查不到」
NOT_FOUND_HINTS = ("找不到番号", "not found", "404", "no such")


def is_not_found(text):
    """这条错误是不是「番号不存在」而不是「请求出问题」。"""

    t = (text or "").lower()

    return any(h in t for h in NOT_FOUND_HINTS)


def run_batch(numbers, make_service, workers=DEFAULT_WORKERS,
              on_progress=None, should_stop=None):
    """并发抓取一批番号。

    ``make_service()`` 由调用方提供 —— 每次调用要返回一个**全新的**
    EnrichService（自带独立 Database 连接）。工厂模式而不是传实例，
    因为连接不能跨线程共用。

    ``on_progress(done, total, result)`` 在每个条目完成时回调。
    ``should_stop()`` 返回 True 时停止派发新任务（已在跑的跑完）。

    返回结果列表，顺序与输入**一致**（并发完成顺序是乱的，
    按输入顺序排回来，调用方才能按序展示）。
    """

    numbers = list(numbers or [])

    total = len(numbers)

    if not total:
        return []

    results = [None] * total

    done = [0]

    lock = threading.Lock()

    stopped = threading.Event()

    def work(idx_and_number):

        idx, number = idx_and_number

        if stopped.is_set() or (should_stop and should_stop()):

            stopped.set()

            return idx, {"number": number, "error": "已停止"}

        # 每个任务新建服务（= 新建 DB 连接）。SQLite 连接不能跨线程。
        try:

            svc = make_service()

        except Exception as exc:                                # noqa: BLE001

            return idx, {"number": number,
                         "error": "建服务失败 {}: {}".format(
                             type(exc).__name__, exc)}

        try:

            res = svc.enrich(number, want="all")

        except Exception as exc:                                # noqa: BLE001

            res = {"number": number,
                   "error": "{}: {}".format(type(exc).__name__, exc)}

        finally:

            # 连接用完就关，别攒着 —— 并发下连接数是有上限的
            try:
                svc.db.conn.close()
            except Exception:                                   # noqa: BLE001
                pass

        return idx, res

    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:

        futures = [ex.submit(work, (i, n)) for i, n in enumerate(numbers)]

        for fut in cf.as_completed(futures):

            try:

                idx, res = fut.result()

            except Exception as exc:                            # noqa: BLE001

                continue

            results[idx] = res

            with lock:

                done[0] += 1
                n_done = done[0]

            if on_progress:

                try:
                    on_progress(n_done, total, res)
                except Exception:                               # noqa: BLE001
                    pass

    # 没跑到的补一个占位，避免调用方拿到 None
    for i, r in enumerate(results):

        if r is None:

            results[i] = {"number": numbers[i], "error": "未执行"}

    return results
