# -*- coding: utf-8 -*-
"""兜底源测试。

覆盖三件事：
  1. 开关解析（含 YAML 布尔陷阱）
  2. JavBus 适配器的**字段翻译**（javdb 形状对齐）
  3. enrich 的兜底时机 —— **只在主源查不到时**才走
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fallback_config import fallback_enabled, parse_fallback   # noqa: E402


# ─────────────────────── 开关解析

def test_parse_fallback_accepts_documented_values():
    assert parse_fallback("auto") is True
    assert parse_fallback("off") is False
    assert parse_fallback("AUTO") is True
    assert parse_fallback("OFF") is False


def test_parse_fallback_handles_yaml_bool_trap():
    """YAML 1.1 把裸写的 off 解析成布尔 False —— 不能因此当成「启用」。

    实测：config 里写 `fallback: off`，yaml.safe_load 给的是 False。
    """

    assert parse_fallback(False) is False
    assert parse_fallback(True) is True

    # 也认字符串化的布尔
    assert parse_fallback("false") is False
    assert parse_fallback("no") is False
    assert parse_fallback("yes") is True


def test_parse_fallback_unknown_value_uses_default():
    """认不出来的取值不猜 —— 按 default 走。"""

    assert parse_fallback("maybe", default=False) is False
    assert parse_fallback("maybe", default=True) is True
    assert parse_fallback(None, default=False) is False
    assert parse_fallback(None, default=True) is True


def test_fallback_enabled_from_config():
    assert fallback_enabled({"fallback": "auto"}) is True
    assert fallback_enabled({"fallback": "off"}) is False
    assert fallback_enabled({}) is False, "缺配置默认关"
    assert fallback_enabled(None) is False


# ─────────────────────── 适配器字段翻译

class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        import json
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_bus(monkeypatch, detail_payload, magnets_payload=None):
    """造一个 JavBusClient，网络层用假响应。"""

    from adapters.javbus_adapter import JavBusClient
    import urllib.request

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/magnets/" in url:
            return FakeResp(magnets_payload if magnets_payload is not None else [])
        return FakeResp(detail_payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    return JavBusClient()


DETAIL = {
    "id": "SSIS-001",
    "title": "SSIS-001 テスト",
    "img": "http://x/c.jpg",
    "date": "2021-02-18",
    "videoLength": 150,
    "gid": "45622881774",
    "uc": "0",
    "producer": {"id": "7q", "name": "エスワン"},
    "publisher": {"id": "9x", "name": "S1"},
    "genres": [{"id": "3", "name": "多P"}, {"id": "30", "name": "美少女"}],
    "stars": [{"id": "2xi", "name": "葵つかさ"}],
}

MAGNETS = [
    {
        "id": "f0eb84a7a5af8a1275800f5af0e89467f35e9ee8",
        "link": "magnet:?xt=urn:btih:f0eb84a7",
        "title": "(無修正-流出) SSIS-001 (Uncensored Leaked)",
        "size": "7.74GB",
        "numberSize": 8310761717,
        "isHD": True,
        "shareDate": "2025-08-02",
        "hasSubtitle": False,
    },
    {
        "id": "28737FEF370B2E8503F18C2650FC77EE3F575313",
        "link": "magnet:?xt=urn:btih:28737FEF",
        "title": "SSIS-001-C.torrent",
        "size": "6.10GB",
        "numberSize": 6549172224,
        "isHD": True,
    },
]


def test_bus_detail_maps_to_javdb_shape(monkeypatch):
    """JavBus 字段要翻成 JavDB 形状，下游才不用改。"""

    bus = make_bus(monkeypatch, DETAIL, MAGNETS)

    d = bus.detail("SSIS-001")

    assert d["number"] == "SSIS-001"          # JavBus 叫 id
    assert d["title"] == "SSIS-001 テスト"
    assert d["cover_url"] == "http://x/c.jpg"
    assert d["release_date"] == "2021-02-18"
    assert d["maker_name"] == "エスワン"
    assert d["actors"] == [{"name": "葵つかさ"}]
    assert d["tags"] == [{"name": "多P"}, {"name": "美少女"}]

    # JavBus 没有评论数 —— 必须是 None，不能是 0
    # （0 会让 tier_of 以为「有数据但为零」，None 才是「没数据」）
    assert d["comments_count"] is None

    # 兜底来源标记
    assert d["lookup_source"] == "javbus"

    # JavDB 没有的字段留着（将来做时长校验用）
    assert d["video_length"] == 150


def test_bus_magnets_name_comes_from_title(monkeypatch):
    """**关键**：JavBus 的磁力名在 `title`，`name` 是空的。

    判定器靠名字比对，取错字段会导致「正确磁力数」恒为 0。
    """

    bus = make_bus(monkeypatch, DETAIL, MAGNETS)

    d = bus.detail("SSIS-001")

    assert len(d["magnets"]) == 2

    m0 = d["magnets"][0]

    assert m0["name"] == "(無修正-流出) SSIS-001 (Uncensored Leaked)"
    assert m0["hash"] == "f0eb84a7a5af8a1275800f5af0e89467f35e9ee8"

    # 体积统一成 MB（下游 size_text 按 MB 解释）
    assert abs(m0["size"] - 8310761717 / 1024 / 1024) < 1


def test_bus_size_falls_back_to_text(monkeypatch):
    """没有 numberSize 时从文本 "6.10GB" 反解。"""

    bus = make_bus(monkeypatch, DETAIL, [
        {"id": "x", "title": "SSIS-001", "size": "6.10GB"},
        {"id": "y", "title": "SSIS-001", "size": "500MB"},
    ])

    d = bus.detail("SSIS-001")

    assert abs(d["magnets"][0]["size"] - 6.10 * 1024) < 1
    assert abs(d["magnets"][1]["size"] - 500) < 1


def test_bus_missing_size_is_none_not_zero(monkeypatch):
    """体积拿不到就 None —— 宁可缺，不要编 0。"""

    bus = make_bus(monkeypatch, DETAIL, [{"id": "x", "title": "SSIS-001"}])

    d = bus.detail("SSIS-001")

    assert d["magnets"][0]["size"] is None


def test_bus_not_found_returns_none(monkeypatch):
    """查不到（404 / 空）要返回 None，让 D8 的闸门照常工作。"""

    bus = make_bus(monkeypatch, {})

    assert bus.detail("ZZZZ-9999") is None


def test_bus_magnets_need_gid_uc(monkeypatch):
    """没有 gid/uc 就别去查磁力（服务端会 400）。"""

    payload = dict(DETAIL)
    payload.pop("gid")

    bus = make_bus(monkeypatch, payload, MAGNETS)

    d = bus.detail("SSIS-001")

    assert d["magnets"] == []


# ─────────────────────── enrich 的兜底时机

class FakePrimary:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def detail(self, number):
        self.calls += 1
        return self.payload


class FakeFallback:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def detail(self, number):
        self.calls += 1
        return self.payload


def build_svc(tmp_path, primary, fallback):
    from core.database_v2 import Database
    from services.enrich_service import EnrichService

    db = Database(str(tmp_path / "lib.db"))

    return EnrichService(
        db,
        covers_dir=str(tmp_path / "covers"),
        screenshots_dir=str(tmp_path / "shots"),
        client=primary,
        fallback_client=fallback,
    ), db


FALLBACK_DETAIL = {
    "number": "ABP-171",
    "title": "T",
    "cover_url": "",
    "release_date": "2020-01-01",
    "maker_name": "M",
    "actors": [],
    "tags": [],
    "comments_count": None,
    "lookup_source": "javbus",
    "magnets": [
        {"name": "ABP-171.mp4", "hash": "a", "size": 1000},
        {"name": "ABP-171-C.torrent", "hash": "b", "size": 900},
    ],
}


def test_fallback_used_only_when_primary_misses(tmp_path):
    primary = FakePrimary(None)
    fallback = FakeFallback(FALLBACK_DETAIL)

    svc, db = build_svc(tmp_path, primary, fallback)

    res = svc.enrich("ABP-171")

    assert fallback.calls == 1, "主源查不到 -> 该走兜底"
    assert res["source"] == "javbus"
    assert res["magnets"] == 2

    r = db.conn.execute(
        "SELECT tier, lookup_source FROM titles WHERE number='ABP-171'"
    ).fetchone()

    assert r[1] == "javbus", "落库要记来源"


def test_fallback_not_used_when_primary_hits(tmp_path):
    """主源查到了就**不该**碰兜底（省一次外部请求）。"""

    primary = FakePrimary(dict(FALLBACK_DETAIL, lookup_source="javdb"))
    fallback = FakeFallback(FALLBACK_DETAIL)

    svc, db = build_svc(tmp_path, primary, fallback)

    res = svc.enrich("ABP-171")

    assert fallback.calls == 0, "主源命中时不得调用兜底"
    assert res["source"] == "javdb"


def test_both_miss_still_reports_not_found(tmp_path):
    """两边都查不到 -> 依旧是「查不到」（D8 闸门不变）。"""

    svc, db = build_svc(tmp_path, FakePrimary(None), FakeFallback(None))

    res = svc.enrich("ZZZZ-1")

    assert res["source"] is None
    assert "查不到" in res["error"]

    n = db.conn.execute(
        "SELECT COUNT(*) FROM titles WHERE number='ZZZZ-1'"
    ).fetchone()[0]

    assert n == 0, "查不到不得建 title"


def test_fallback_exception_does_not_break_flow(tmp_path):
    """兜底自己炸了，不能把「查不到」这个结论带崩。"""

    class Boom:
        def detail(self, number):
            raise RuntimeError("服务没起")

    svc, db = build_svc(tmp_path, FakePrimary(None), Boom())

    res = svc.enrich("ABP-171")

    assert res["source"] is None
    assert "RuntimeError" in res.get("fallback_error", "")


def test_no_fallback_client_means_old_behavior(tmp_path):
    """不传兜底 -> 行为与加兜底之前完全一致。"""

    primary = FakePrimary(None)

    svc, db = build_svc(tmp_path, primary, None)

    res = svc.enrich("ABP-171")

    assert res["source"] is None
