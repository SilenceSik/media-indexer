# -*- coding: utf-8 -*-
"""置信分明细（详情页）+ 可调配比（首页）的测试。

主人 2026-09-24 两条要求：
  1. 「在卡片详情里写置信分的详情」—— 只有一个数字看不出为什么低。
  2. 「首页增加一个支持自己配比置信分」—— 四项权重可调。

顺带钉住一条**踩过的坑**：明细的键名不能叫 `items`，
Jinja 里 `bd.items` 会解析成 dict 的 `.items()` 方法，模板 for 直接 TypeError。
"""

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)


@pytest.fixture
def web(tmp_path, monkeypatch):
    import web.app as A

    from core.database_v2 import Database

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    # 一条证据充足的（磁力多、核对过）
    db.conn.execute(
        "INSERT INTO titles (number, tier, correct_magnets, comments_count, "
        "number_matches) VALUES ('FULL-001', '高', 22, 2, 1)"
    )
    # 一条证据薄的（磁力 0、没评论）
    db.conn.execute(
        "INSERT INTO titles (number, tier, correct_magnets, comments_count, "
        "number_matches) VALUES ('THIN-002', '低', 0, 0, 1)"
    )

    for num in ("FULL-001", "THIN-002"):
        db.conn.execute(
            "INSERT INTO media_files (title_id, filepath, filename, size, "
            "match_source, match_confidence) "
            "SELECT id, 'D:/x/' || number || '.mp4', number || '.mp4', 1000000, "
            "'dictionary', 95 FROM titles WHERE number = ?",
            (num,),
        )

    db.conn.commit()

    monkeypatch.setattr(A, "DB_PATH", db_path)
    monkeypatch.setattr(A, "PREVIEWS_DIR", str(tmp_path / "previews"))
    os.makedirs(str(tmp_path / "previews"), exist_ok=True)

    from fastapi.testclient import TestClient

    return TestClient(A.app), db, A


# ═══════════════════ 详情页：置信分明细

def test_detail_has_breakdown(web):
    client, _, _ = web

    h = client.get("/detail/FULL-001").text

    assert "怎么来的" in h, "详情页没有置信分明细"
    assert 'class="bd"' in h

    # 四项 + 合计 + 时长
    for label in ("磁力条数", "番号核对", "识别形态", "评论数",
                  "服务器侧合计", "时长一致性"):
        assert label in h, "明细缺项：{}".format(label)


def test_breakdown_names_are_parts_not_items(web):
    """键名必须是 `parts` —— `items` 会被 Jinja 当成 dict 方法。

    这是真踩过的坑：模板 `{% for it in bd.items %}` 直接
    `TypeError: 'builtin_function_or_method' object is not iterable`。
    """

    client, _, _ = web

    h = client.get("/detail/FULL-001").text

    assert "bd-k" in h, "明细没渲染出来（多半是键名撞了 dict.items）"


def test_breakdown_explains_the_math(web):
    """明细要能自圆其说：（服务器侧 + 门控加成）× 时长 = 最终分。"""

    client, _, _ = web

    h = client.get("/detail/FULL-001").text

    m = re.search(
        r"最终分 = （服务器侧 \+ 门控加成）× 时长一致性 =\s*"
        r"(\d+) \+ (\d+)\s*"
        r"= (\d+) ×\s*([\d.]+)\s*"
        r"= <b>(\d+)</b>",
        h,
    )

    assert m, "没渲染出算式"

    base, bonus, gated, factor, score = (
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        float(m.group(4)), int(m.group(5)),
    )

    assert base + bonus == gated, \
        "加成没算进「服务器侧合计」：{} + {} != {}".format(base, bonus, gated)

    assert abs(gated * factor - score) <= 1, \
        "算式对不上：{} × {} = {}".format(gated, factor, score)


def test_breakdown_notes_where_points_are_lost(web):
    """ABF-087 那类问题的答案：要指出哪项几乎没贡献。"""

    client, _, _ = web

    h = client.get("/detail/FULL-001").text

    # 评论 2 条 -> 应写明「到 50 条封顶」，让人看出为什么这项低
    assert "到 50 条封顶" in h
    assert "到 15 条封顶" in h


def test_breakdown_shows_duration_as_multiplicative(web):
    """时长要标明是**乘性**因子，不混进四项求和里。"""

    client, _, _ = web

    h = client.get("/detail/FULL-001").text

    assert "乘性" in h
    assert "×0." in h or "×1." in h


# ═══════════════════ 首页：可调配比

def test_index_has_weight_panel(web):
    client, _, _ = web

    h = client.get("/").text

    assert 'class="wpanel"' in h, "首页没有配比面板"
    for name in ("w_magnets", "w_match", "w_source", "w_comments"):
        assert name in h, "面板缺字段：{}".format(name)


def test_weights_change_scores(web):
    """改配比要真的改变分数。"""

    client, _, _ = web

    def score_of(number):
        h = client.get("/").text
        for blk in re.split(r'<div class="shell">', h)[1:]:
            if '<a class="num" href="/detail/{}"'.format(number) in blk:
                m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', blk)
                return int(m.group(1)) if m else None
        return None

    base = score_of("FULL-001")

    only_magnets = None

    h = client.get("/?w_magnets=100&w_match=0&w_source=0&w_comments=0").text
    for blk in re.split(r'<div class="shell">', h)[1:]:
        if '<a class="num" href="/detail/FULL-001"' in blk:
            m = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', blk)
            only_magnets = int(m.group(1)) if m else None

    assert base is not None and only_magnets is not None
    assert base != only_magnets, "配比没生效（{} vs {}）".format(base, only_magnets)


def test_weights_are_normalized(web):
    """填 2/2/2/2 与填 36/29/21/14 不同，但都不越界。"""

    client, _, _ = web

    def scores(url):
        h = client.get(url).text
        out = {}
        for blk in re.split(r'<div class="shell">', h)[1:]:
            m = re.search(r'<a class="num" href="/detail/([^"]+)"', blk)
            s = re.search(r'class="conf c-\w+"[^>]*>\s*(\d+)', blk)
            if m and s:
                out[m.group(1)] = int(s.group(1))
        return out

    eq = scores("/?w_magnets=2&w_match=2&w_source=2&w_comments=2")
    big = scores("/?w_magnets=200&w_match=200&w_source=200&w_comments=200")

    for got in (eq, big):
        assert all(0 <= v <= 100 for v in got.values()), \
            "分数越界（归一化没做）：{}".format(got)

    # 2/2/2/2 与 200/200/200/200 等价（都是等权）
    assert eq == big, "等权配比不该因数值大小而不同"


def test_zero_weights_fall_back_to_default(web):
    """全填 0 -> 退回默认，而不是把分数做成 0/0。"""

    client, _, _ = web

    import web.app as A

    w = A.normalize_weights(0, 0, 0, 0)

    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w == pytest.approx(A.DEFAULT_WEIGHTS)


def test_normalize_weights_helper():
    import web.app as A

    # 已归一 -> 原样
    w = A.normalize_weights(36, 29, 21, 14)
    assert abs(w["magnets"] - 0.36) < 1e-9
    assert abs(sum(w.values()) - 1.0) < 1e-9

    # 任意数值 -> 归一
    w2 = A.normalize_weights(1, 1, 1, 1)
    assert all(abs(v - 0.25) < 1e-9 for v in w2.values())

    # 缺项 -> 用默认补齐
    w3 = A.normalize_weights(w_magnets=100)
    assert abs(sum(w3.values()) - 1.0) < 1e-9

    # 非法值 -> 忽略
    w4 = A.normalize_weights(w_magnets="abc")
    assert abs(sum(w4.values()) - 1.0) < 1e-9


def test_default_weights_match_confidence_module():
    """首页默认配比必须与 `core.confidence` 的常量一致。

    两处各写一份数字迟早漂移 —— 那时候「默认」就成了假的。
    """

    import web.app as A

    from core import confidence as C

    assert A.DEFAULT_WEIGHTS["magnets"] == C.W_MAGNETS
    assert A.DEFAULT_WEIGHTS["match"] == C.W_MATCH
    assert A.DEFAULT_WEIGHTS["source"] == C.W_SOURCE
    assert A.DEFAULT_WEIGHTS["comments"] == C.W_COMMENTS
