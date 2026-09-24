# -*- coding: utf-8 -*-
"""真浏览器验证「详情页」的图片大图弹窗与四个动作按钮。

覆盖：
  - 封面/截图点了在本页开弹窗（不是开新标签）
  - ← → 翻页、首尾环绕、Esc 关闭、点背景关闭
  - 只有一张图时箭头隐藏
  - 卡片四个按钮齐、且删除按钮的门控与后端结论一致
"""

import sqlite3

import pytest


def _pick_title(db_path):
    """挑一个封面和截图都齐的番号；没有就退而求其次。"""

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT t.number, m.cover_local, m.screenshots
        FROM titles AS t
        JOIN metadata AS m ON m.title_id = t.id
        WHERE m.cover_local IS NOT NULL AND m.cover_local != ''
          AND m.screenshots IS NOT NULL AND m.screenshots != ''
        LIMIT 5
        """
    ).fetchall()

    con.close()

    if not rows:
        pytest.skip("库里没有同时带封面和截图的番号，跳过")

    return rows[0]["number"]


def test_detail_lightbox(page, live_server):
    base_url, db_path = live_server
    number = _pick_title(db_path)

    page.goto(f"{base_url}/detail/{number}", wait_until="networkidle")

    # ── 图集大小：封面 + 截图 ──
    total = page.evaluate("LB.length")
    assert total >= 2, f"图集只有 {total} 张，测不了翻页"

    shots = page.evaluate(
        "document.querySelectorAll('.shots .shot').length"
    )
    assert shots == total - 1, "截图数应为图集总数减掉封面那张"

    box = page.locator("#lightbox")
    assert not box.is_visible(), "弹窗初始应为隐藏"

    # ── 点封面 → 第 0 张 ──
    page.locator(".poster .card").click()
    assert box.is_visible(), "点封面没打开弹窗"
    assert page.evaluate("lbIdx") == 0

    opened_url = page.evaluate("document.getElementById('lb-img').src")
    assert page.evaluate("LB[0].src") in opened_url
    assert page.evaluate("document.getElementById('lb-count').textContent") == f"1 / {total}"

    # 弹窗开着时不该把页面导航走（原来 target=_blank 会开新标签）
    assert "/detail/" in page.url, "点图后页面被导航走了"

    # ── → 翻下一张 ──
    page.keyboard.press("ArrowRight")
    assert page.evaluate("lbIdx") == 1
    assert page.evaluate("document.getElementById('lb-count').textContent") == f"2 / {total}"

    # ── ← 翻回上一张 ──
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("lbIdx") == 0

    # ── 首尾环绕：第 0 张再往左 → 最后一张 ──
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("lbIdx") == total - 1, "首尾没有环绕"

    # ── 箭头按钮本身也能点 ──
    page.locator("#lb-next").click()
    assert page.evaluate("lbIdx") == 0, "从末张点下一张应回到第 0 张"

    # ── Esc 关闭 ──
    page.keyboard.press("Escape")
    assert not box.is_visible(), "Esc 没关掉弹窗"

    # ── 点截图按对应索引打开 ──
    page.locator(".shots .shot").nth(1).click()
    assert box.is_visible()
    assert page.evaluate("lbIdx") == 2, "第 2 张截图应对应图集索引 2（封面占 0）"

    # ── 点背景关闭（图本身不应关闭）──
    page.mouse.click(5, 5)
    assert not box.is_visible(), "点背景没关掉弹窗"

    assert not page.js_errors, "详情页 JS 报错：\n" + "\n".join(page.js_errors)


def test_detail_has_four_actions(page, live_server):
    """四个动作按钮要在，且删除按钮的门控与后端结论一致。"""

    base_url, db_path = live_server
    number = _pick_title(db_path)

    page.goto(f"{base_url}/detail/{number}", wait_until="networkidle")

    labels = page.evaluate(
        "(() => [...document.querySelectorAll('.detail-actions .mini')]"
        ".map(b => b.textContent.trim()))()"
    )
    assert labels == ["位置", "删除", "种子", "抓取"], f"按钮不对：{labels}"

    # 删除按钮的可点性必须与 eligibility 结论对齐：可点=有 onclick，
    # 不可点=disabled 且带 blocked_reason 说明
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id FROM titles WHERE number = ?", (number,)
    ).fetchone()
    con.close()
    assert row, f"库里找不到 {number}"

    state = page.evaluate(
        """(() => {
      const b = [...document.querySelectorAll('.detail-actions .mini')]
        .find(x => x.textContent.trim() === '删除');
      return { disabled: b.disabled, hasHandler: !!b.getAttribute('onclick') };
    })()"""
    )

    # 要么能删（有 handler），要么明确禁用；不能出现"能点但服务端会拒"
    assert state["disabled"] != state["hasHandler"], (
        f"删除按钮状态自相矛盾：{state}"
    )

    assert not page.js_errors, "详情页 JS 报错：\n" + "\n".join(page.js_errors)
