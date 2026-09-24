# -*- coding: utf-8 -*-
"""常见 AV 视频格式（**删除门控**专用）。

⚠️ 这张表和 `core/scanner_v2.py` 的 `DEFAULT_VIDEO_EXTENSIONS` **不是一回事**，
两者有意分开：

  * 扫描白名单是**可配置**的（`config.yaml` 里能加），它决定「什么文件值得
    去看一眼番号」。放宽它是用户的选择，代价只是多解析几个文件。
  * 这张表是**硬编码**的，只用于**删除门控**：文件要被批量删掉，
    它得先是「通常的 AV 格式」。放宽扫描不该顺带放宽删除 —— 那是两件事。

背景（2026-09-23 实测）：`.webm` 曾进过白名单，结果把成人游戏 `\\ga\\`
目录里的游戏内视频段全收了进来 —— 26,230 条语料里 `.webm` 与可信厂牌的
交集为 **0**。用户结论：`.webm` 属于错误后缀，AV 本来就不该匹配。

所以这里**不含 `.webm`**。
"""

# 删除门控认可的常见 AV 视频格式。
TYPICAL_AV_EXTENSIONS = frozenset({
    ".mp4",
    ".mkv",
    ".avi",
    ".wmv",
    ".rmvb",
    ".rm",
    ".mov",
    ".flv",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".ts",
    ".m2ts",
    ".iso",
})


def ext_of(path):
    """取扩展名（小写，含点）。无扩展名返回空串。"""

    if not path:
        return ""

    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]

    if "." not in name:
        return ""

    return "." + name.rsplit(".", 1)[-1].lower()


def is_typical_av_ext(path):
    """这个文件是不是常见 AV 格式（删除门控用）。"""

    return ext_of(path) in TYPICAL_AV_EXTENSIONS
