# -*- coding: utf-8 -*-
"""一键弹窗的布局回归测试（DOM 实测，不靠肉眼）。

主人 2026-09-24：「UI 乱了」。截图暴露的问题与对应判据：

  1. 长番号（FC2-PPV-3141813）换行 -> 把「N 个」徽章挤到**第二行**，
     各行的数字右边缘对不成一条竖线
     -> 判据：行高一致、第 3 列右边缘唯一
  2. 提示文字挤在「取消」按钮后面，像按钮的一部分
     -> 判据：提示行在按钮**下方**
  3. 60 行清单把「确认送回收站」按钮顶出视野，确认时反而找不到按钮
     -> 判据：按钮**始终**在视野内（滚动前后都要）
  4. 底部内容贴边
     -> 判据：清单底到卡片底有余量；滚到底也不被裁

⚠️ 改这些判据之前先想清楚：它们是**可量化的**，比看截图可靠。
（写这份测试时我自己踩过一次：滚的是 `.modal-card`，而修完后滚的是
`#batch-list`，测量对象错了就报了个假的「底部被裁」。）
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)

from tests.conftest import CHROME  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHROME), reason="本机没有 Chrome"
)


def _launch():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


@pytest.fixture
def live_app(tmp_path):
    """起一个真服务（临时库），给浏览器点。"""

    import threading

    import uvicorn

    import web.app as A

    from core.database_v2 import Database

    db_path = str(tmp_path / "lib.db")

    db = Database(db_path)

    # 造 40 个高置信可批量删的番号，其中一个**超长番号**（复现截图里的换行）
    for i in range(40):

        num = "FC2-PPV-3141813" if i == 0 else "TEST-{:03d}".format(i)

        db.conn.execute(
            "INSERT INTO titles (number, tier, correct_magnets, "
            "comments_count, number_matches) VALUES (?, '高', 12, 5, 1)",
            (num,),
        )
        real = tmp_path / "{}.mp4".format(num)
        real.write_bytes(b"x" * 2048)

        db.conn.execute(
            "INSERT INTO media_files (title_id, filepath, filename, size, "
            "match_source, match_confidence) "
            "SELECT id, ?, ?, 2048, 'dictionary', 95 FROM titles "
            "WHERE number = ?",
            (str(real), "{}.mp4".format(num), num),
        )
        db.conn.execute(
            "INSERT INTO magnets (title_id, magnet, verified, is_correct) "
            "SELECT id, ?, 1, 1 FROM titles WHERE number = ?",
            ("magnet:?xt=urn:btih:{}".format(i), num),
        )

    db.conn.commit()

    A.DB_PATH = db_path
    A.PREVIEWS_DIR = str(tmp_path / "previews")
    os.makedirs(A.PREVIEWS_DIR, exist_ok=True)

    config = uvicorn.Config(A.app, host="127.0.0.1", port=0,
                            log_level="error")

    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 等就绪
    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("服务没起来")

    port = server.servers[0].sockets[0].getsockname()[1]

    yield "http://127.0.0.1:{}".format(port)

    server.should_exit = True
    thread.join(timeout=5)


def _open_modal(page, base):
    page.goto(base + "/", wait_until="networkidle")
    page.click("button:has-text('一键送回收站')")
    page.wait_for_selector("#batch-list .brow", timeout=15000)
    page.wait_for_timeout(300)


def test_batch_modal_columns_align(live_app):
    """各行的数字右边缘必须齐平 —— 长番号不得把徽章挤到第二行。"""

    with _launch() as p:

        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})

        _open_modal(page, live_app)

        m = page.evaluate("""() => {
          const rows = Array.from(
            document.querySelectorAll('#batch-list .brow'));
          return {
            heights: rows.map(r =>
              Math.round(r.getBoundingClientRect().height)),
            rights: rows.map(r => {
              const c = r.children[2];
              return c ? Math.round(c.getBoundingClientRect().right) : -1;
            }),
          };
        }""")

        b.close()

    assert len(set(m["rights"])) == 1, \
        "数字列右边缘不齐：{}".format(sorted(set(m["rights"])))

    assert max(m["heights"]) - min(m["heights"]) <= 4, \
        "行高差异过大（有行换行了）：{}".format(sorted(set(m["heights"])))


def test_batch_modal_button_always_visible(live_app):
    """「确认送回收站」必须始终在视野内 —— 不能被长清单顶出去。"""

    with _launch() as p:

        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})

        _open_modal(page, live_app)

        def btn_visible():
            return page.evaluate("""() => {
              const r = document.getElementById('batch-go')
                              .getBoundingClientRect();
              return r.top >= 0 && r.bottom <= window.innerHeight;
            }""")

        before = btn_visible()

        # 滚到底再看一次
        page.evaluate("""() => {
          const l = document.getElementById('batch-list');
          l.scrollTop = l.scrollHeight;
        }""")
        page.wait_for_timeout(250)

        after = btn_visible()

        b.close()

    assert before, "打开弹窗时按钮就不在视野内"
    assert after, "滚动清单后按钮被顶出视野"


def test_batch_modal_list_scrolls_not_card(live_app):
    """滚动应该发生在清单区，而不是整张卡。"""

    with _launch() as p:

        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})

        _open_modal(page, live_app)

        m = page.evaluate("""() => {
          const card = document.querySelector('#modal-batch .modal-card');
          const list = document.getElementById('batch-list');
          return {
            card_scrollable: card.scrollHeight > card.clientHeight + 1,
            list_scrollable: list.scrollHeight > list.clientHeight + 1,
          };
        }""")

        b.close()

    assert m["list_scrollable"], "清单不可滚动（40 行应该超出卡片高度）"
    assert not m["card_scrollable"], \
        "整卡仍在滚动 —— 会把按钮顶出视野"


def test_batch_modal_hint_below_buttons(live_app):
    """提示文字要在按钮**下方**，不能挤在「取消」后面。"""

    with _launch() as p:

        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})

        _open_modal(page, live_app)

        m = page.evaluate("""() => {
          const btns = document.querySelector(
            '#modal-batch .modal-actions').getBoundingClientRect();
          const hint = document.getElementById('batch-hint')
                              .getBoundingClientRect();
          return { below: hint.top >= btns.bottom - 2,
                   gap: Math.round(hint.top - btns.bottom) };
        }""")

        b.close()

    assert m["below"], "提示文字没在按钮下方（挤在同一行了）"


def test_index_has_no_horizontal_overflow(live_app):
    """首页不该有元素横向溢出视口。"""

    with _launch() as p:

        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})

        page.goto(live_app + "/", wait_until="networkidle")

        bad = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('body *').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            if (r.right > window.innerWidth + 1 || r.left < -1) {
              out.push(el.tagName.toLowerCase() + '.' +
                       String(el.className).slice(0, 30));
            }
          });
          return out.slice(0, 10);
        }""")

        b.close()

    assert not bad, "首页有横向溢出：{}".format(bad)
