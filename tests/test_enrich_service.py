# -*- coding: utf-8 -*-
"""抓取服务测试（用假的 JavDB 客户端，不联网）。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import Database                          # noqa: E402
from services.enrich_service import EnrichService              # noqa: E402


class FakeClient:
    """假客户端：只回固定数据，不联网。"""

    def __init__(self, detail=None, assets=None, download=None):
        self._detail = detail
        self._assets = assets or []
        self._download = download
        self.calls = []

    def detail(self, number):
        self.calls.append(("detail", number))
        return self._detail

    def assets(self, number, kind="image"):
        self.calls.append(("assets", number))
        return self._assets

    def download_assets(self, lines, directory, timeout=300):
        self.calls.append(("download", directory, len(lines)))
        os.makedirs(directory, exist_ok=True)
        made = []
        for i in range(self._download if self._download is not None else len(lines)):
            p = os.path.join(directory, f"image-{i + 1:03d}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"x" * (100 - i))
            made.append(p)
        return made


DETAIL = {
    "title": "测试标题",
    "origin_title": "テスト",
    "release_date": "2014-07-19",
    "maker_name": "プレステージ",
    "cover_url": "http://x/c.jpg",
    "actors": [{"name": "桃谷エリカ"}],
    "tags": [{"name": "多P"}],
    "magnets": [
        {"hash": "aaa", "name": "ABP-171.mp4", "size": 5880},
        {"hash": "bbb", "name": "[FHD]abp-171.mp4", "size": 700},
        {"hash": "ccc", "name": "ABP-171-UC.torrent.非同厂版本"},
    ],
}


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))

    covers = tmp_path / "covers"
    shots = tmp_path / "shots"

    return db, str(covers), str(shots)


def test_enrich_writes_metadata_and_magnets(env):
    db, covers, shots = env

    svc = EnrichService(db, covers, shots, client=FakeClient(detail=DETAIL))

    res = svc.enrich("ABP-171", want="all")

    assert res["metadata"] is True
    assert res["magnets"] == 3

    row = db.conn.execute(
        "SELECT title, maker, actresses, tags FROM metadata"
    ).fetchone()

    assert row[0] == "测试标题"
    assert row[1] == "プレステージ"
    assert json.loads(row[2]) == [{"name": "桃谷エリカ"}]

    mags = db.magnets_for_title("ABP-171")

    assert len(mags) == 3

    # verified 的语义已收紧（P0-1）：只有**判定为属于该番号**的磁力才标 1。
    # 旧实现无条件写 1 —— 1 条挂错的磁力就能让门控放行删除。
    # 这里三条名字都含 ABP-171，所以三条都该是 1。
    assert all(m["verified"] == 1 for m in mags)


def test_size_unit_is_mb_not_bytes(env):
    """JavDB 的 size 单位是 MB（5880 -> 5.74 GB）。按字节算会全变 0.00 GB。"""

    db, covers, shots = env

    svc = EnrichService(db, covers, shots, client=FakeClient(detail=DETAIL))

    svc.enrich("ABP-171", want="all")

    sizes = {m["size_text"] for m in db.magnets_for_title("ABP-171")}

    assert "5.74 GB" in sizes, f"实测得到 {sizes}"
    assert "700 MB" in sizes
    assert "0.00 GB" not in sizes


def test_size_text_edge_cases():
    assert EnrichService.size_text(1024) == "1.00 GB"
    assert EnrichService.size_text(512) == "512 MB"
    assert EnrichService.size_text(0) == ""
    assert EnrichService.size_text(None) == ""


def test_enrich_magnets_only_skips_media(env):
    """want=magnets 时不下载素材（不碰网络文件）。"""

    db, covers, shots = env

    client = FakeClient(detail=DETAIL)

    svc = EnrichService(db, covers, shots, client=client)

    res = svc.enrich("ABP-171", want="magnets")

    assert res["magnets"] == 3
    assert res["screenshots"] == 0
    assert ("assets", "ABP-171") not in client.calls


def test_enrich_handles_not_found(env):
    db, covers, shots = env

    svc = EnrichService(db, covers, shots, client=FakeClient(detail=None))

    res = svc.enrich("NOPE-999")

    assert res["metadata"] is False
    assert res["error"]


def test_enrich_downloads_cover_and_screenshots(env):
    db, covers, shots = env

    assets = [
        ("image", "http://x/small.jpg"),
        ("image", "http://x/big.jpg"),
        ("image", "http://x/samples/a_l_0.jpg"),
        ("image", "http://x/samples/a_l_1.jpg"),
    ]

    client = FakeClient(detail=DETAIL, assets=assets)

    svc = EnrichService(db, covers, shots, client=client)

    res = svc.enrich("ABP-171", want="all")

    assert res["cover"], "应下载封面"
    assert res["screenshots"] == 2

    row = db.conn.execute("SELECT cover_local, screenshots FROM metadata").fetchone()

    # 存的是**相对路径**（带番号子目录），不是 basename ——
    # 各番号的封面文件名完全一样，只存 basename 会全站撞名。
    assert row[0] == res["cover"]
    assert "/" in row[0], f"应保留子目录层级，实际 {row[0]}"

    shots_saved = json.loads(row[1])

    assert len(shots_saved) == 2
    assert all("/" in s for s in shots_saved), f"截图同样保留层级：{shots_saved}"


def test_media_failure_keeps_magnets(env):
    """素材（封面/截图）失败时，元数据与磁力仍要落库。

    早前素材异常会写进 result["error"]，让整条看起来像失败 ——
    磁力明明拿到了也白抓，用户只看到"抓取失败"。
    """

    db, covers, shots = env

    class ExplodingClient(FakeClient):

        def assets(self, number, kind="image"):
            raise RuntimeError("assets 挂了")

    svc = EnrichService(db, covers, shots, client=ExplodingClient(detail=DETAIL))

    res = svc.enrich("ABP-171", want="all")

    assert res["error"] is None, "素材失败不该让整条判为失败"
    assert res["media_error"], "但要如实记录素材失败"
    assert res["metadata"] is True
    assert res["magnets"] == 3
    assert len(db.magnets_for_title("ABP-171")) == 3


def test_magnet_uri_from_hash():
    assert EnrichService.magnet_uri({"hash": "abc", "name": "n"}) == \
        "magnet:?xt=urn:btih:abc&dn=n"


def test_magnet_uri_prefers_existing_field():
    assert EnrichService.magnet_uri(
        {"magnet": "magnet:?xt=urn:btih:zzz", "hash": "ignored"}
    ) == "magnet:?xt=urn:btih:zzz"


def test_magnet_uri_without_hash():
    assert EnrichService.magnet_uri({"name": "no hash"}) is None
