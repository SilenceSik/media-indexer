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
    args = cli_entry.parse_args([r"X:\迅雷下载"])

    assert args.paths == [r"X:\迅雷下载"]


def test_parse_args_multiple_paths():
    args = cli_entry.parse_args([r"X:\片", r"X:\下载"])

    assert args.paths == [r"X:\片", r"X:\下载"]


def test_parse_args_flags():
    args = cli_entry.parse_args(["--list", "--dry-run", "X:/x"])

    assert args.list_only is True
    assert args.dry_run is True
    assert args.paths == ["X:/x"]


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


def test_dry_run_writes_nothing(tmp_path, capsys):
    """dry-run 必须不写库、不推进索引。"""

    video = tmp_path / "SSIS-531-uncensored.mp4"
    video.write_bytes(b"x")

    rc = cli_entry.main(["--dry-run", str(tmp_path)])

    out = capsys.readouterr().out

    assert rc == 0
    assert "dry-run" in out
    assert "SSIS-531" in out
    assert "未写库" in out

    # 生产库不该被创建/改动
    prod = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "storage",
        "library_v2.db",
    )

    if os.path.exists(prod):

        import sqlite3

        c = sqlite3.connect(prod)

        rows = list(
            c.execute(
                "SELECT COUNT(*) FROM media_files WHERE filepath LIKE ?",
                (f"%{video.name}%",),
            )
        )

        c.close()

        assert rows[0][0] == 0, "dry-run 不得写入 media_files"
