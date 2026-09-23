"""AV 匹配格式白名单 —— 验收测试

对应主人 2026-09-23 定的两条设计规矩：
  ① AV 匹配要限定格式：程序默认几个标准格式，也支持自定义添加
  ② 多抓的交给后续磁力阶段去除：错的项目不含磁力；
     没有磁力的项目即使是真 AV 也不应删除

本文件覆盖 ① 的实现（白名单语义 + 过滤时机 + 与删除门控的一致性）。
② 的策略矩阵在 tests/test_magnets.py。
"""

import os
import sqlite3

import pytest

from core.scanner_v2 import (
    DEFAULT_VIDEO_EXTENSIONS,
    Scanner,
    normalize_extensions,
    resolve_extensions,
)
from services.scan_service import ScanService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES_PATH = os.path.join(ROOT, "data", "dictionary.json")


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as fh:
        import json
        return json.load(fh)["rules"]


def make_files(tmp_path, names):
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)

    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 16)

    return str(folder)


def index_paths(index_db):
    """file_index 里实际登记了哪些路径（用来证明「过滤在 stat 之前」）"""
    conn = sqlite3.connect(index_db)

    try:
        return [
            row[0] for row in conn.execute("SELECT path FROM file_index")
        ]

    finally:
        conn.close()


# ------------------------------------------------- 1. 内置默认（程序自带）

def test_defaults_are_non_empty_and_normalized():
    """内置默认必须是一套标准视频格式，且本身已归一化。"""

    assert len(DEFAULT_VIDEO_EXTENSIONS) >= 5

    for ext in DEFAULT_VIDEO_EXTENSIONS:
        assert ext.startswith("."), ext
        assert ext == ext.lower(), ext

    # 实测可信命中全部落在前四种上，必须都在
    for ext in (".mp4", ".mkv", ".avi", ".wmv"):
        assert ext in DEFAULT_VIDEO_EXTENSIONS


def test_webm_not_in_defaults():
    """
    .webm 不进默认：15,593 个文件（语料 59.4%）可信产出 0.00%。

    这条断言是防回归 —— 谁把 .webm 塞回默认，必须显式改测试并说明理由。
    """

    assert ".webm" not in DEFAULT_VIDEO_EXTENSIONS


# ------------------------------------------------------ 2. 追加语义（非覆盖）

def test_custom_extends_instead_of_replacing():
    """
    自定义是**追加**：传 ['.webm'] 之后，内置的 .mp4 依然在。

    覆盖语义最危险的地方是「少写一个格式 → 静默漏扫整类文件」，
    追加语义下最坏只是多扫，方向是安全的。
    """

    resolved = resolve_extensions([".webm"])

    assert ".webm" in resolved
    assert ".mp4" in resolved
    assert ".mkv" in resolved

    assert set(DEFAULT_VIDEO_EXTENSIONS) <= set(resolved)


def test_empty_custom_falls_back_to_defaults():
    """配置留空 = 只用内置默认，不报错、不空扫。"""

    assert resolve_extensions([]) == DEFAULT_VIDEO_EXTENSIONS
    assert resolve_extensions(None) == DEFAULT_VIDEO_EXTENSIONS


def test_custom_dedupes_against_defaults():
    """重复写内置已有的格式，不产生重复项。"""

    resolved = resolve_extensions([".mp4", ".MP4", "mkv"])

    assert list(resolved).count(".mp4") == 1
    assert list(resolved).count(".mkv") == 1


# ------------------------------------------------------------ 3. 归一化

@pytest.mark.parametrize(
    "raw,expected",
    [
        ([".MP4"], [".mp4"]),
        (["mp4"], [".mp4"]),
        (["  mkv  "], [".mkv"]),
        ([".WebM"], [".webm"]),
        (["", None, "  "], []),
    ],
)
def test_normalize_extensions(raw, expected):
    assert normalize_extensions(raw) == expected


def test_normalize_preserves_order():
    assert normalize_extensions(["zzz", "aaa", "zzz"]) == [".zzz", ".aaa"]


# ---------------------------------------------------- 4. 扫描时真的过滤

def test_scan_skips_unlisted_extensions(tmp_path):
    folder = make_files(
        tmp_path,
        [
            "ABP-123.mp4",
            "IPX-456.mkv",
            "random_clip.webm",
            "notes.txt",
            "cover.jpg",
            "no_extension",
        ]
    )

    index_db = str(tmp_path / "index.db")

    found = Scanner(index_db).scan(folder)

    names = sorted(os.path.basename(p) for p in found)

    assert names == ["ABP-123.mp4", "IPX-456.mkv"]


def test_extension_match_is_case_insensitive(tmp_path):
    folder = make_files(tmp_path, ["ABP-123.MP4", "IPX-456.MKV"])

    found = Scanner(str(tmp_path / "index.db")).scan(folder)

    assert len(found) == 2


def test_filter_happens_before_stat(tmp_path):
    """
    被排除的文件不得进 file_index —— 证明过滤发生在 os.stat 之前。

    这一条不只是「省性能」：被排除的文件连索引都不该留，
    否则后续 file_index 里会混进一堆永不解析的垃圾路径。
    """

    folder = make_files(
        tmp_path,
        ["ABP-123.mp4", "junk.webm", "readme.txt"]
    )

    index_db = str(tmp_path / "index.db")

    Scanner(index_db).scan(folder)

    indexed = [os.path.basename(p) for p in index_paths(index_db)]

    assert indexed == ["ABP-123.mp4"]


def test_custom_extension_is_scanned(tmp_path):
    """自定义追加的格式必须真被扫到（不只是配置里好看）。"""

    folder = make_files(tmp_path, ["ABP-123.mp4", "IPX-456.webm"])

    scanner = Scanner(str(tmp_path / "index.db"), extensions=[".webm"])

    found = sorted(os.path.basename(p) for p in scanner.scan(folder))

    assert found == ["ABP-123.mp4", "IPX-456.webm"]


def test_accepts_handles_dotless_and_case():
    scanner = Scanner(":memory:")

    assert scanner.accepts("X:/a.MP4") is True
    assert scanner.accepts("X:/a.webm") is False
    assert scanner.accepts("X:/noext") is False


# ------------------------------------------------- 5. ScanService 透传

def test_scan_service_passes_extensions_through(tmp_path):
    """
    ScanService(extensions=...) 必须真传到 Scanner。

    回归保护：透传断掉时配置静默失效，扫描结果看不出异常。
    """

    folder = make_files(tmp_path, ["ABP-123.mp4", "IPX-456.webm"])

    service = ScanService(
        str(tmp_path / "index.db"),
        load_rules(),
        extensions=[".webm"],
    )

    rows = service.scan(folder, persist=False)["data"]

    assert sorted(os.path.basename(r["file"]) for r in rows) == [
        "ABP-123.mp4",
        "IPX-456.webm",
    ]


def test_scan_service_defaults_when_no_extensions(tmp_path):
    """不传 extensions → 内置默认生效，.webm 被挡在外面。"""

    folder = make_files(tmp_path, ["ABP-123.mp4", "IPX-456.webm"])

    service = ScanService(str(tmp_path / "index.db"), load_rules())

    rows = service.scan(folder, persist=False)["data"]

    assert [os.path.basename(r["file"]) for r in rows] == ["ABP-123.mp4"]


# ------------------------------------- 6. 与「删除门控」的一致性（规矩 ②）

def test_excluded_files_never_reach_delete_gate(tmp_path):
    """
    格式白名单 + 落库条件 + 删除门控 三层的接缝（规矩 ①② 交界）。

    三层依次收紧：
      ① 格式白名单：不在名单里的文件，连进都不进来
      ② 解析：进了扫描、但文件名里没有番号 → 不落库
      ③ 删除门控：落了库、但没有 verified 磁力 → 仍然不可删

    所以「多抓的假阳性」要走到「被误删」需要连过三关，
    而第 ③ 关是硬性的 —— 没有磁力，即使是真 AV 也不删。
    """

    from core.database_v2 import Database

    folder = make_files(
        tmp_path,
        [
            "ABP-123.mp4",     # 有番号、格式在默认名单 → 落库
            "IPX-456.webm",    # 有番号、格式靠自定义追加 → 落库
            "junk.webm",       # 格式被追加了，但没有番号 → 不落库
        ]
    )

    db_path = str(tmp_path / "library_v2.db")

    db = Database(db_path)

    ScanService(
        str(tmp_path / "index.db"),
        load_rules(),
        db=db,
        extensions=[".webm"],
    ).scan(folder)

    stored = {
        os.path.basename(row[0])
        for row in db.conn.execute("SELECT filepath FROM media_files")
    }

    # ② 无番号的不落库（.webm 被追加进来了也不落）
    assert stored == {"ABP-123.mp4", "IPX-456.webm"}

    # ③ 一个磁力都没有 → 可删列表为空
    #    （包括 IPX-456.webm 这种「格式本来被排除、靠追加才收进来」的）
    assert db.deletable_titles() == []

    # ④ 拿到 verified 磁力后，才出现在可删列表
    db.add_magnet("IPX-456", "magnet:?xt=urn:btih:verified", verified=1)

    assert [t["number"] for t in db.deletable_titles()] == ["IPX-456"]
