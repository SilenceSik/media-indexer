# -*- coding: utf-8 -*-
"""运行时设置（`core.runtime_settings`）与代理解析的测试。

## 为什么这些测试重要

代理地址是**使用者自己填**的（软件不预设端口），填错/被静默忽略是最难查
的一类问题 —— 用户会以为「我明明配了」。所以这里锁住：

* 白名单：只接受约定好的键，别的键**拒绝写入**（不静默丢弃）
* 优先级：构造参数 > 环境变量 > settings.json > config.yaml > 默认
* 拿不到配置时**回落默认而不是崩**（WebUI 起来就会构造客户端）
* 写文件是**原子替换**（中途失败不留半个文件）

settings.json 落在项目 `storage/` 下 —— 测试用临时文件，**必须把
模块里的路径重定向**，否则会污染真实设置。
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from core import runtime_settings as RS  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """把 settings.json 指到临时目录，避免污染真实设置。"""

    target = tmp_path / "settings.json"

    monkeypatch.setattr(RS, "_PATH", str(target))
    RS.reload_settings()

    yield target

    RS.reload_settings()


# ═══════════════════════ 读写

def test_save_then_load_roundtrip(isolated_settings):

    ok, note = RS.save({"javbus_proxy": "http://127.0.0.1:10809"})

    assert ok, note

    RS.reload_settings()

    assert RS.get("javbus_proxy") == "http://127.0.0.1:10809"


def test_save_is_whitelisted(isolated_settings):
    """白名单之外的键要**拒绝**，而不是静默丢弃。"""

    ok, note = RS.save({"database": "somewhere/else.db"})

    assert not ok
    assert "database" in note

    assert not isolated_settings.exists(), "拒绝时不该落盘"


def test_save_rejects_mixed_keys(isolated_settings):
    """一锅里有非法键 -> 整体拒绝，不能写一半。"""

    ok, note = RS.save({
        "javdb_backend": "native",
        "evil_key": "x",
    })

    assert not ok

    RS.reload_settings()

    assert RS.get("javdb_backend") != "native", "整体拒绝时不该写入任何键"


def test_save_merges_without_dropping_other_keys(isolated_settings):
    """保存是合并语义 —— 没提到的键不能被抹掉。"""

    RS.save({"javbus_proxy": "http://a:1", "javdb_backend": "native"})
    RS.reload_settings()

    RS.save({"javbus_proxy": "http://b:2"})
    RS.reload_settings()

    assert RS.get("javbus_proxy") == "http://b:2"
    assert RS.get("javdb_backend") == "native", "保存时被误删了"


def test_save_is_atomic_no_leftover_tmp(isolated_settings):
    """落盘走临时文件 + replace —— 不留 .tmp。"""

    RS.save({"javbus_backend": "native"})

    leftovers = [
        p for p in os.listdir(os.path.dirname(str(isolated_settings)))
        if p.endswith(".tmp")
    ]

    assert not leftovers, "留下了临时文件：{}".format(leftovers)


def test_empty_value_clears(isolated_settings):
    """填了再清空 -> 回到「未设置」，能回落 config.yaml。"""

    RS.save({"javbus_proxy": "http://a:1"})
    RS.reload_settings()
    assert RS.get("javbus_proxy") == "http://a:1"

    RS.save({"javbus_proxy": ""})
    RS.reload_settings()

    assert RS.get("javbus_proxy") == "", "空串应表示「清空」"


def test_corrupt_file_does_not_raise(isolated_settings):
    """文件坏了不能把 WebUI 拖垮。"""

    isolated_settings.write_text("{ this is not json", encoding="utf-8")

    RS.reload_settings()

    assert RS.get("javbus_backend") == "service", "应回落默认"


def test_load_returns_default_for_unknown_key(isolated_settings):
    assert RS.get("nope") is None


# ═══════════════════════ snapshot

def test_snapshot_reports_source(isolated_settings):
    """UI 要能显示「这个值是从哪来的」。"""

    RS.save({"javdb_backend": "native"})
    RS.reload_settings()

    snap = RS.snapshot()

    assert snap["javdb_backend"]["value"] == "native"
    assert snap["javdb_backend"]["source"] == "settings.json"

    # 没设过的键要标出来源是 config.yaml 或默认
    assert snap["javbus_backend"]["source"] in ("config.yaml", "默认")


def test_snapshot_covers_all_editable_keys(isolated_settings):

    snap = RS.snapshot()

    assert set(snap) == set(RS.EDITABLE_KEYS)


# ═══════════════════════ 代理解析（软件不预设端口）

def test_proxy_comes_from_settings(isolated_settings, monkeypatch):

    monkeypatch.delenv("LMM_JAVBUS_PROXY", raising=False)

    from core import datasource_config as DC

    DC.reload_config()

    RS.save({"javbus_proxy": "socks5h://127.0.0.1:10808"})
    RS.reload_settings()

    assert DC.get_proxy("javbus", "LMM_JAVBUS_PROXY") == \
        "socks5h://127.0.0.1:10808"


def test_env_beats_settings(isolated_settings, monkeypatch):
    """环境变量优先于 UI 保存的值（便于临时试验）。"""

    from core import datasource_config as DC

    RS.save({"javbus_proxy": "http://from-settings:1"})
    RS.reload_settings()

    monkeypatch.setenv("LMM_JAVBUS_PROXY", "http://from-env:2")

    assert DC.get_proxy("javbus", "LMM_JAVBUS_PROXY") == "http://from-env:2"


def test_no_proxy_anywhere_returns_none(isolated_settings, monkeypatch):
    """哪里都没配 -> None，**不能编一个默认端口出来**。

    这是「软件不预设端口」的核心约束：宁可返回 None 让上层提示
    「去配代理」，也不能悄悄用 7890 之类的端口。
    """

    for var in ("LMM_JAVBUS_PROXY", "LMM_JAVDB_PROXY",
                "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)

    from core import datasource_config as DC

    DC.reload_config()

    assert DC.get_proxy("javbus", "LMM_JAVBUS_PROXY") is None
    assert DC.proxy_configured("javbus", "LMM_JAVBUS_PROXY") is False


def test_proxy_configured_true_when_set(isolated_settings, monkeypatch):

    monkeypatch.setenv("LMM_JAVBUS_PROXY", "http://x:1")

    from core import datasource_config as DC

    assert DC.proxy_configured("javbus", "LMM_JAVBUS_PROXY") is True


# ═══════════════════════ 后端解析

def test_backend_from_settings(isolated_settings, monkeypatch):

    monkeypatch.delenv("LMM_JAVBUS_BACKEND", raising=False)

    from core import datasource_config as DC

    DC.reload_config()

    RS.save({"javbus_backend": "native"})
    RS.reload_settings()

    assert DC.get_backend("javbus", "LMM_JAVBUS_BACKEND",
                          ("service", "native"), "service") == "native"


def test_backend_env_beats_settings(isolated_settings, monkeypatch):

    from core import datasource_config as DC

    RS.save({"javbus_backend": "native"})
    RS.reload_settings()

    monkeypatch.setenv("LMM_JAVBUS_BACKEND", "service")

    assert DC.get_backend("javbus", "LMM_JAVBUS_BACKEND",
                          ("service", "native"), "service") == "service"


def test_invalid_backend_falls_back_to_default(isolated_settings, monkeypatch):

    monkeypatch.delenv("LMM_JAVBUS_BACKEND", raising=False)

    from core import datasource_config as DC

    DC.reload_config()

    RS.save({"javbus_backend": "bogus"})
    RS.reload_settings()

    assert DC.get_backend("javbus", "LMM_JAVBUS_BACKEND",
                          ("service", "native"), "service") == "service"


def test_client_resolve_backend_uses_settings(isolated_settings, monkeypatch):
    """端到端：settings.json 改一下，客户端构造出来就该换后端。"""

    monkeypatch.delenv("LMM_JAVBUS_BACKEND", raising=False)

    from adapters.javbus_adapter import JavBusClient
    from core import datasource_config as DC

    DC.reload_config()

    assert JavBusClient().backend == "service"

    RS.save({"javbus_backend": "native"})
    RS.reload_settings()

    c = JavBusClient()

    assert c.backend == "native"

    # native 后端要真的挂上原生客户端（否则只是名字变了）
    assert c._native is not None
