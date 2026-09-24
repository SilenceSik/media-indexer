# -*- coding: utf-8 -*-
"""筛选日志的原因码与文案。

**被挡下的文件都要留痕**（主人 2026-09-24 要求）。以前这些只存在扫描任务的
内存里（`_SCAN_JOB["skipped_samples"]`），任务一结束就没了 ——
用户看不到「哪些文件被筛掉了、为什么」，也无从复查。

这里集中定义原因码，避免各处写自由文本导致无法统计。
"""

# 原因码 -> 人话
REASONS = {
    # ── 扫描阶段 ──
    "excluded_dir": "路径落在排除目录里（游戏/缓存等）",
    "extension": "扩展名不在白名单内",
    "stat_failed": "读不到文件信息（挂载点 / 权限 / 坏道）",
    "size_below_min": "小于设定的文件大小下限",
    "size_above_max": "超过设定的文件大小上限",
    "not_recognized": "文件名里识别不出番号",

    # ── 入库阶段 ──
    "confidence_out_of_range": "识别置信度不在设定的区间内",

    # ── 查询阶段 ──
    "lookup_notfound": "javdb 与 javbus 都查不到该番号（D8 闸门）",
    "number_mismatch": "站点返回的番号与识别出的不一致",

    # ── 误匹配（不是「查不到」，恰恰是「查得到」）──
    #
    # 2026-09-24 主人报的 FH-27：目录名 `…激情啪啪等FH 27V` 被抠成 `FH-27`，
    # 而本地其实是 25 集自拍短片。JavDB 上 FH-27 **真实存在**，于是它的
    # 磁力与评论被强加在这些错误文件上（卡片显示「高 / 磁力 15 / 评论」）。
    #
    # ⚠️ 与 `lookup_notfound` 分开记，绝不混用：这条是**站上有、我们认错了**，
    # 那条是**站上根本没有**。混起来会让「查不到」的统计说谎。
    "false_match": "识别出的番号并非本文件（目录名误匹配，站上另有其片）",

    # ── 档位阶段 ──
    "tier_too_low": "档位极低（0 条正确磁力），不值得入库",

    # ── 识别形态 / 格式（D11 第三条）──
    "untrusted_recognition": "识别来自弱形态（无分隔符 / 裸数字 / 兜底）",
    "atypical_extension": "不是常见 AV 格式（.webm 等）",

    # ── 时长（**只作评分，不是拒绝理由**）──
    "duration_off": "本地时长与元数据差得多（仅提示，不拦）",

    # ── 时长**完全不符**（另一部片，不是广告片头那种小偏差）──
    #
    # 主人 2026-09-24 定：像 FH-27 那种「9 分钟 vs 130 分钟」要扣到 0 分
    # 并踢出库。与 `duration_off` 严格区分 —— 那条是「可能只是片段/广告，
    # 只提示不拦」，这条是「确定不是这部片」。
    "duration_mismatch": "本地文件与元数据不是同一部片（时长完全不符）",
}


def label(reason):
    """原因码 -> 人话。不认识的原样返回，不吞。"""

    return REASONS.get(reason, reason or "")


def record(db, *, path=None, filename=None, size=None, number=None,
           confidence=None, source=None, reason="", detail=None,
           stage=None):
    """写一条筛选日志。

    `detail` 不给就用原因码的标准文案。写失败**不能**影响主流程
    （日志是旁路）。
    """

    try:

        db.conn.execute(
            """
            INSERT INTO filter_log
            (path, filename, size, number, confidence, source,
             reason, detail, stage, created_time)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                path,
                filename,
                size,
                number,
                confidence,
                source,
                reason,
                detail if detail is not None else label(reason),
                stage,
                __import__("time").time(),
            ),
        )

    except Exception:                                           # noqa: BLE001
        pass


def flush(db):
    """提交（批量写时在最后调一次就够）。"""

    try:
        db.conn.commit()
    except Exception:                                           # noqa: BLE001
        pass


def summary(db, limit=100):
    """按原因汇总，给界面显示「筛掉了多少、为什么」。"""

    try:

        rows = db.conn.execute(
            """
            SELECT reason, COUNT(*) AS n
            FROM filter_log
            GROUP BY reason
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "reason": r[0],
                "count": r[1],
                "label": label(r[0]),
            }
            for r in rows
        ]

    except Exception:                                           # noqa: BLE001
        return []


def entries(db, reason=None, limit=200):
    """列出明细，可按原因过滤。"""

    try:

        if reason:

            rows = db.conn.execute(
                """
                SELECT path, filename, size, number, confidence, source,
                       reason, detail, stage, created_time
                FROM filter_log WHERE reason = ?
                ORDER BY id DESC LIMIT ?
                """,
                (reason, limit),
            ).fetchall()

        else:

            rows = db.conn.execute(
                """
                SELECT path, filename, size, number, confidence, source,
                       reason, detail, stage, created_time
                FROM filter_log
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

    except Exception:                                           # noqa: BLE001
        return []

    keys = ("path", "filename", "size", "number", "confidence",
            "source", "reason", "detail", "stage", "created_time")

    return [dict(zip(keys, r)) for r in rows]
