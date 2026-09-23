import os

from core.file_index import FileIndex


# ───────────────────────── 番号匹配的格式白名单 ────────────────────────
#
# 内置默认（程序自带一套标准视频格式，开箱即用）。
#
# 实测依据（26,230 条真实文件名快照）：
#   可信档（confidence >= 90）命中 223 个文件，全部落在
#   .mp4(200) / .mkv(11) / .wmv(9) / .avi(3) 四种格式上，其余格式贡献 0。
#
# 特别说明 .webm：语料里 15,593 个（占 59.4%），可信产出 0.00%，
# 且低分档假阳性几乎全是 .webm 片段（录屏 / OF / 3D 动画）。
# 所以它不在内置默认里 —— 确有个别真番号是 .webm，在 config.yaml 的
# video_extensions 里追加一行 ".webm" 即可（配置是「追加」语义）。
DEFAULT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".ts",
    ".m2ts",
)


def normalize_extensions(extensions):
    """
    归一化扩展名列表：去空、转小写、补前导点、保序去重。

    ".MP4" / "mp4" / " mp4 " → ".mp4"
    None / 空列表 → []
    """

    out = []

    for ext in extensions or ():

        if not ext:

            continue

        text = str(ext).strip().lower()

        if not text:

            continue

        if not text.startswith("."):

            text = "." + text

        if text not in out:

            out.append(text)

    return out


def resolve_extensions(extensions=None):
    """
    有效白名单 = 内置标准格式 ∪ 自定义追加项。

    **追加语义，不是覆盖**：配置里写的是「我还想多收哪些格式」，
    内置标准格式始终保留。理由：配置是给人手写的，一旦写成覆盖语义，
    少写一个格式就会静默漏扫整类文件；追加语义下最坏只是多扫。

    extensions 为空 → 只用内置默认。
    """

    merged = list(DEFAULT_VIDEO_EXTENSIONS)

    for ext in normalize_extensions(extensions):

        if ext not in merged:

            merged.append(ext)

    return tuple(merged)


class Scanner:

    def __init__(
        self,
        index_db,
        rules_version=None,
        extensions=None
    ):

        self.index = FileIndex(
            index_db,
            rules_version=rules_version
        )

        self.extensions = resolve_extensions(
            extensions
        )

    def accepts(
        self,
        path
    ):

        """该文件是否在白名单内（大小写不敏感）。"""

        return os.path.splitext(

            path

        )[1].lower() in self.extensions

    def scan(
        self,
        folder,
        update_index=True
    ):

        """
        扫描 folder 下「扩展名在白名单内、且发生变化」的文件。

        白名单过滤放在 os.stat **之前**：非视频文件（.txt/.jpg/无扩展名）
        与未收录的格式既不进 file_index、也不做 stat —— 26,230 条语料里
        这一步省掉 60% 的 stat 与解析（被省掉的格式可信产出为 0）。

        update_index=False → 真·干跑：只报告哪些文件「需要处理」，
        不推进 file_index（否则紧接着的真跑会因索引已推进而静默空转）。
        """

        changed = []

        for root, dirs, files in os.walk(folder):

            for name in files:

                path = os.path.join(
                    root,
                    name
                )

                if not self.accepts(
                    path
                ):

                    continue

                stat = os.stat(
                    path
                )

                if self.index.changed(
                    path,
                    stat.st_size,
                    stat.st_mtime
                ):

                    changed.append(
                        path
                    )

                    if update_index:

                        self.index.update(
                            path,
                            stat.st_size,
                            stat.st_mtime
                        )

        return changed
