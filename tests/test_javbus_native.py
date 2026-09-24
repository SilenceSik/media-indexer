# -*- coding: utf-8 -*-
"""`core.javbus_native` 的解析测试 —— 纯 HTML 解析，**不走网络**。

夹具是照真实页面结构**手写的最小 HTML**，所以任何机器上都能确定复现。

## 这些测试锁住的东西

每条都对应一个在真实页面上踩过的坑：

| 用例 | 锁住的行为 |
|---|---|
| info 区定位 | 类是 `col-md-3 info`（带 grid 前缀），**不能精确匹配** `class="info"`
  —— 匹配不上会退化成整页搜，类别/演员全丢 |
| 类别 vs 演员 | 判别器是 **href 前缀 `/genre/` 与 `/star/`**，
  不是所在容器：页面上「女優」段的链接也渲染在 `span.genre` 里 |
| 多个演员 | 用 `id="star_XXX"` 定位；用 `class="star-box"` + `</div></div>`
  收尾的正则会**吃掉第二个盒子**（实测只抓到 1 个） |
| 磁力两步 | `gid`/`uc` 从详情页正则取，磁力靠它查 |
| 体积解析 | `"7.74GB"` -> 字节；解不出返回 None，**不编 0** |
| 磁力排序 | 按体积降序（与上游 `convertMagnetsHTML` 一致） |
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from core.javbus_native import (  # noqa: E402
    parse_detail,
    parse_magnets,
    size_text_to_bytes,
)


# ═══════════════════════ 夹具

def _detail_html(number="SSIS-001", length="150分鐘", with_stars=2):
    """照真实结构手写的最小详情页。"""

    stars = ""

    names = ["葵つかさ", "乙白さやか", "桃乃木かな"][:with_stars]
    ids = ["2xi", "w5a", "abc"][:with_stars]

    for nm, sid in zip(names, ids):
        stars += (
            '<div id="star_{sid}" class="star-box star-box-common '
            'star-box-up idol-box"> <li> '
            '<a href="https://www.javbus.com/star/{sid}">'
            '<img src="/pics/actress/{sid}_a.jpg" title="{nm}"></a> '
            '<div class="star-name"><a href="https://www.javbus.com/star/{sid}"'
            ' title="{nm}">{nm}</a></div> </li> </div> '
        ).format(sid=sid, nm=nm)

    return """<html><body>
<div class="container">
  <h3>{number} テスト作品タイトル</h3>
  <div class="row movie">
    <div class="col-md-9 screencap">
      <a class="bigImage" href="/pics/cover/83ie_b.jpg">
        <img src="/pics/cover/83ie_b.jpg" title="タイトル"></a>
    </div>
    <div class="col-md-3 info">
      <p><span class="header">識別碼:</span>
         <span style="color:#CC0000;">{number}</span></p>
      <p><span class="header">發行日期:</span> 2021-02-18</p>
      <p><span class="header">長度:</span> {length}</p>
      <p><span class="header">導演:</span>
         <a href="https://www.javbus.com/director/o7">苺原</a></p>
      <p><span class="header">製作商:</span>
         <a href="https://www.javbus.com/studio/7q">エスワン</a></p>
      <p><span class="header">發行商:</span>
         <a href="https://www.javbus.com/label/9x">S1 NO.1 STYLE</a></p>
      <p class="header">類別:<span id="genre-toggle"></span></p>
      <p>
        <span class="genre"><label><input type="checkbox" name="gr_sel"
          value="3"><a href="https://www.javbus.com/genre/3">多P</a></label></span>
        <span class="genre"><label><input type="checkbox" name="gr_sel"
          value="4u"><a href="https://www.javbus.com/genre/4u">戲劇</a></label></span>
      </p>
      <p class="star-show"><span class="header">演員</span>:</p>
      <ul> {stars} </ul>
    </div>
  </div>
  <div id="sample-waterfall"></div>
</div>
<script>
  var gid = 45622861236;
  var uc = 0;
</script>
</body></html>""".format(number=number, length=length, stars=stars)


# ═══════════════════════ 详情解析

def test_detail_basic_fields():
    d = parse_detail(_detail_html())

    assert d["id"] == "SSIS-001"
    assert d["date"] == "2021-02-18"
    assert d["videoLength"] == 150
    assert "テスト作品" in d["title"]


def test_detail_finds_info_region_with_grid_class():
    """**回归**：info 区是 `col-md-3 info`，精确匹配 `class="info"` 会框不到。

    框不到就退化成整页搜 —— 类别与演员会全丢。
    """

    d = parse_detail(_detail_html())

    assert d["producer"]["name"] == "エスワン"
    assert d["publisher"]["name"] == "S1 NO.1 STYLE"
    assert d["director"]["name"] == "苺原"


def test_detail_genres_exclude_stars():
    """**回归**：判别器是 href 前缀，不是所在容器。

    页面上「女優」段的链接也渲染在 `span.genre` 里 ——
    只按容器收会把演员名当类别。
    """

    d = parse_detail(_detail_html(with_stars=2))

    genre_names = [g["name"] for g in d["genres"]]

    assert genre_names == ["多P", "戲劇"], genre_names

    # 演员名一个都不该混进类别
    for bad in ("葵つかさ", "乙白さやか"):
        assert bad not in genre_names


def test_detail_parses_all_stars():
    """**回归**：多个演员盒子都要抓到。

    用 `class="star-box"` + `</div></div>` 收尾的正则会吃掉第二个盒子
    （实测只抓到 1 个，而页面上有 2 个）——必须用 `id="star_XXX"` 定位。
    """

    d = parse_detail(_detail_html(with_stars=2))

    names = [s["name"] for s in d["stars"]]

    assert names == ["葵つかさ", "乙白さやか"], names


def test_detail_single_star():
    d = parse_detail(_detail_html(with_stars=1))

    assert [s["name"] for s in d["stars"]] == ["葵つかさ"]


def test_detail_extracts_gid_uc():
    """磁力是两步：gid/uc 从详情页取，缺了就查不到磁力。"""

    d = parse_detail(_detail_html())

    assert d["gid"] == "45622861236"
    assert d["uc"] == "0"


def test_detail_cover_is_absolute():
    d = parse_detail(_detail_html())

    assert d["img"] == "https://www.javbus.com/pics/cover/83ie_b.jpg"


def test_detail_handles_empty_html():
    assert parse_detail("") is None
    assert parse_detail(None) is None


def test_detail_duration_variants():
    assert parse_detail(_detail_html(length="150分鐘"))["videoLength"] == 150
    assert parse_detail(_detail_html(length="120分"))["videoLength"] == 120
    assert parse_detail(_detail_html(length="—"))["videoLength"] is None


# ═══════════════════════ 磁力解析

def _magnets_html():
    return """<html><body><table>
    <tr>
      <td><a href="magnet:?xt=urn:btih:aaaabbbbcccc1111">
        SSIS-001_uncensored 高清</a></td>
      <td><a href="/x">7.74GB</a></td>
      <td><a href="/y">2024-01-02</a></td>
    </tr>
    <tr>
      <td><a href="magnet:?xt=urn:btih:ddddeeeeffff2222">
        SSIS-001 中文字幕</a></td>
      <td><a href="/x">1.50GB</a></td>
      <td><a href="/y">2024-03-05</a></td>
    </tr>
    <tr>
      <td><a href="https://example.com/not-a-magnet">不是磁力</a></td>
      <td><a href="/x">1MB</a></td>
      <td><a href="/y">2024-01-01</a></td>
    </tr>
    </table></body></html>"""


def test_magnets_basic():
    ms = parse_magnets(_magnets_html())

    assert len(ms) == 2, "非磁力行必须被排除"

    hashes = [m["id"] for m in ms]

    assert "aaaabbbbcccc1111" in hashes
    assert "ddddeeeeffff2222" in hashes


def test_magnets_hash_is_from_btih():
    """JavBus 的磁力 hash 在链接里，且**磁力名在 `title`、番号在 `id`**。"""

    ms = parse_magnets(_magnets_html())

    top = ms[0]

    assert top["id"] == "aaaabbbbcccc1111"
    assert top["title"].startswith("SSIS-001_uncensored")
    assert top["link"].startswith("magnet:?xt=urn:btih:")


def test_magnets_flags_hd_and_subtitle():
    ms = parse_magnets(_magnets_html())

    by_hash = {m["id"]: m for m in ms}

    assert by_hash["aaaabbbbcccc1111"]["isHD"] is True
    assert by_hash["aaaabbbbcccc1111"]["hasSubtitle"] is False

    assert by_hash["ddddeeeeffff2222"]["hasSubtitle"] is True
    assert by_hash["ddddeeeeffff2222"]["isHD"] is False


def test_magnets_sorted_by_size_desc():
    """与上游 `convertMagnetsHTML` 一致：按体积降序。"""

    ms = parse_magnets(_magnets_html())

    sizes = [m["numberSize"] for m in ms]

    assert sizes == sorted(sizes, reverse=True)


def test_magnets_size_parsed_to_bytes():
    ms = parse_magnets(_magnets_html())

    top = ms[0]

    assert top["size"] == "7.74GB"
    assert top["numberSize"] == pytest.approx(7.74 * 1024 ** 3, rel=1e-6)


def test_magnets_empty_html():
    assert parse_magnets("") == []
    assert parse_magnets(None) == []


def test_magnets_no_table():
    assert parse_magnets("<html><body>nothing here</body></html>") == []


# ═══════════════════════ 体积解析

@pytest.mark.parametrize("text,expected", [
    ("7.74GB", 7.74 * 1024 ** 3),
    ("1.50GB", 1.50 * 1024 ** 3),
    ("900MB", 900 * 1024 ** 2),
    ("512KB", 512 * 1024),
    ("2TB", 2 * 1024 ** 4),
    ("7.74 GB", 7.74 * 1024 ** 3),
    ("7.74gb", 7.74 * 1024 ** 3),
])
def test_size_text_to_bytes(text, expected):
    assert size_text_to_bytes(text) == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("text", ["", None, "abc", "7.74", "GB", "7.74XB"])
def test_size_text_unparseable_returns_none(text):
    """解不出返回 None —— **宁可缺，不要编 0**。

    （0 会让下游把它当「有数据但为零」，None 才是「没数据」。）
    """

    assert size_text_to_bytes(text) is None
