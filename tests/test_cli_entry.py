# -*- coding: utf-8 -*-
"""main.py 命令行入口测试（自定义目录扫描）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as cli_entry                                      # noqa: E402


def test_parse_args_defaults():
    """不传路径 -> paths 为空（回退 config.yaml 的 scan_paths）。"""

    args = cli_entry.parse_args([])

    assert args.paths == []
    assert args.list_only is False
    assert args.dry_run is False


def test_parse_args_single_path():
    args = cli_entry.parse_args([r"E:\迅雷下载"])

    assert args.paths == [r"E:\迅雷下载"]


def test_parse_args_multiple_paths():
    args = cli_entry.parse_args([r"E:\片", r"F:\下载"])

    assert args.paths == [r"E:\片", r"F:\下载"]


def test_parse_args_flags():
    args = cli_entry.parse_args(["--list", "--dry-run", "D:/x"])

    assert args.list_only is True
    assert args.dry_run is True
    assert args.paths == ["D:/x"]


def test_list_only_does_not_scan(tmp_path, capsys):
    """--list 只列出目录，不能触发扫描（不写库）。"""

    rc = cli_entry.main(["--list", str(tmp_path)])

    out = capsys.readouterr().out

    assert rc == 0
    assert "仅列出" in out
    assert str(tmp_path) in out


def test_missing_dir_is_reported_not_crashed(tmp_path, capsys):
    """目录不存在时跳过并提示，不抛异常。"""

    missing = str(tmp_path / "definitely_not_here")

    rc = cli_entry.main(["--dry-run", missing])

    out = capsys.readouterr().out

    assert rc == 0
    assert "目录不存在" in out


def test_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    """dry-run 必须不写库、不推进索引。

    ⚠️ 用**独立库**跑，不碰生产库。原版去断言 `storage/library_v2.db`，
    结果随生产库内容变化而假失败 —— 实测：E 盘上真有
    `SSIS-531-uncensored\\SSIS-531-uncensored.mp4`，而本用例恰好用同名文件，
    于是「dry-run 没写库」被一个**真实存在**的行判成失败。

    断言方式也改了：直接盯**自己那个库**没有新行，而不是去看别人写没写。
    """

    db = tmp_path / "dry.db"
    idx = tmp_path / "dry_idx.db"

    monkeypatch.setenv("LMM_DB", str(db))
    monkeypatch.setenv("LMM_INDEX_DB", str(idx))

    work = tmp_path / "media"
    work.mkdir()

    video = work / "SSIS-531-uncensored.mp4"
    video.write_bytes(b"x")

    rc = cli_entry.main(["--dry-run", str(work)])

    out = capsys.readouterr().out

    assert rc == 0
    assert "dry-run" in out
    assert "SSIS-531" in out
    assert "未写库" in out

    # 自己的库里不该有任何 media_files
    if db.exists():

        import sqlite3

        c = sqlite3.connect(str(db))

        try:
            rows = c.execute(
                "SELECT COUNT(*) FROM media_files WHERE filepath LIKE ?",
                (f"%{video.name}%",),
            ).fetchone()
        except sqlite3.OperationalError:

            rows = (0,)          # 表都没建 -> 更不可能写入

        c.close()

        assert rows[0] == 0, "dry-run 不得写入 media_files"
