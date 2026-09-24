import os

from core.file_index import FileIndex


# ───────────────────────── AV 匹配的格式白名单 ─────────────────────────
#
# 内置默认（程序自带一套标准视频格式，开箱即用）。
#
# 实测依据（26,230 条真实语料，X:\corpus\codes_extracted.json）：
#   可信档（confidence >= 90）命中 231 个文件，全部落在
#   .mp4 / .mkv / .wmv / .avi 四种格式上，其余格式贡献 0。
#
# 特别说明 .webm：语料里 15,593 个（占 59.4%），可信产出 0.00%。
#   这些文件集中在 X:\ga\ / X:\ga\ 成人游戏资源树（Ren'Py 引擎的
#   game\images、game\movie、www\movies 等目录），是游戏内视频段与
#   引擎缓存，不含番号体系；放开白名单只会引入 1,289 条 conf=70 的
#   假番号（含 KISS-01 / MAST-001 这类动画片段名）。
#   所以它不在内置默认里 —— 真有个别番号是 .webm 时，在 config.yaml 的
#   video_extensions 里追加一行 ".webm" 即可（配置是「追加」语义）。
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

# ───────────────────────── 目录级排除 ─────────────────────────
#
# 依据（26,230 条离线快照实测，2026-09-23）：
#   X:\ga\ 与 X:\ga\（成人游戏资源树，Ren'Py 引擎）贡献了 71.6% 的语料、
#   268 条解析命中，但可信档产出为 0 —— 里面的 "番号" 全是游戏内视频
#   段（...\game\images\...、...\www\movies\...）与引擎缓存名。
#   另有 Photoshop 工具提示视频（.../tool/xxx-tool-*.webm）等软件资源。
#
# 这些目录匹配出的番号是"诚实的错误匹配"——能解析出番号形态，但对象
# 根本不是影片。按目录段排除（匹配路径的任意一段，不区分大小写）。
DEFAULT_EXCLUDED_DIR_SEGMENTS = (
    "ga",              # 成人游戏根目录（X:\ga\ / X:\ga\）
    "game",            # Ren'Py / RPG Maker 引擎资源目录
    "www",             # RPG Maker MV/MZ 网页发布目录
    "animations",      # 引擎动画目录
    "__pycache__",     # 引擎/Python 缓存
    "node_modules",    # 软件依赖目录
)


def normalize_segments(segments):
    """归一化目录段列表：去空、转小写、保序去重。"""

    out = []

    for seg in segments or ():

        if not seg:

            continue

        text = str(seg).strip().lower().strip("\\/")

        if text and text not in out:

            out.append(text)

    return out


def resolve_segments(excluded=None):
    """有效排除表 = 内置默认 ∪ 自定义追加项（与扩展名同为追加语义）。"""

    merged = list(DEFAULT_EXCLUDED_DIR_SEGMENTS)

    for seg in normalize_segments(excluded):

        if seg not in merged:

            merged.append(seg)

    return tuple(merged)


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
        extensions=None,
        excluded_segments=None
    ):

        self.index = FileIndex(
            index_db,
            rules_version=rules_version
        )

        self.extensions = resolve_extensions(
            extensions
        )

        self.excluded_segments = resolve_segments(
            excluded_segments
        )

    def accepts(
        self,
        path
    ):

        """该文件在白名单内、且不在排除目录下（大小写不敏感）。"""

        if os.path.splitext(

            path

        )[1].lower() not in self.extensions:

            return False

        # 目录段排除：路径的任意一段（不区分大小写）命中排除表即拒绝。
        # 按段比对而非整串包含 —— 避免 "X:\Gauntlet\..." 这类
        # 「段内子串」误伤（实测语料里的反例）。
        parts = path.replace("/", "\\").split("\\")

        for part in parts[:-1]:

            if part.strip().lower() in self.excluded_segments:

                return False

        return True

    def scan(
        self,
        folder,
        update_index=True,
        min_size=0,
        max_size=0,
        on_size_skip=None
    ):

        """
        扫描 folder 下「扩展名在白名单内、且发生变化」的文件。

        白名单过滤放在 os.stat **之前**：非视频文件（.txt/.jpg/无扩展名）
        与未收录的格式既不进 file_index、也不做 stat —— 26,230 条语料里
        这一步省掉 60% 的 stat 与解析（被省掉的格式可信产出为 0）。

        大小过滤放在 stat **之后**：挂载点与 LNK 这类 stat 会抛
        OSError 的对象总是安全的，绝不为省一次调用把它们暴露在守卫之外。

        min_size / max_size 单位字节；**0 表示该端不限制**（划到头）。
        被大小挡下的文件不推进 file_index —— 日后调宽门槛重扫时还能进来。

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

                # 守卫在 stat 之前：os.walk 会把挂载点/junction 当文件列出，
                # stat 它们会抛 OSError（实测 X:\System Volume Information
                # 目录名带空格时直接 ValueError）。不为大小过滤把这条守卫挪后。
                try:

                    stat = os.stat(
                        path
                    )

                except (OSError, ValueError):

                    continue

                if min_size and stat.st_size < min_size:

                    # 上报给调用方，否则用户只看到"扫了一堆但通过的是零散几个"，
                    # 界面上 skipped_count 永远是 0，无从判断门槛是否生效。
                    if on_size_skip:

                        on_size_skip(path, stat.st_size, min_size, True)

                    continue

                if max_size and stat.st_size > max_size:

                    if on_size_skip:

                        on_size_skip(path, stat.st_size, max_size, False)

                    continue

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
