# -*- coding: utf-8 -*-
"""真浏览器验证「扫描」页的预设按钮。

为什么非要浏览器：这两个 bug 都只在 Chromium 里显形，后端测试完全看不见。

1. **整块脚本被炸掉** —— `size_filter.js` 用 `const SIZE_STOPS` 在全局声明，
   模板里若再写 `var SIZE_STOPS = ...`，同一全局作用域先 const 后 var 是
   SyntaxError，**整个** <script> 块不执行。表现是预设按钮点了没反应，
   但页面不白、控制台外看不出异常。
2. **step 吸附** —— `preset(60,69)` 遇到 `step="5"` 会被吸附成 70，
   界面上写「60-69」实际筛的是「60-70」。

启动服务、开浏览器、JS 报错收集都在 conftest.py 里。缺 playwright 或
本机 Chrome 时跳过，不挡后端测试。
"""

from conftest import MB


def test_scan_page_has_no_js_error(page, base_url):
    """扫描页不该有任何 JS 报错 —— 上面那类"整块脚本被炸"最先在这里现形。"""

    page.goto(f"{base_url}/scan", wait_until="networkidle")

    assert page.evaluate("typeof LMMSize") == "object", "size_filter.js 没加载上"
    assert not page.js_errors, "页面 JS 报错：\n" + "\n".join(page.js_errors)


def test_scan_presets(page, base_url):
    """点遍两组预设，逐个核对滑块取值、标签、以及真正会发出去的字节数。"""

    page.goto(f"{base_url}/scan", wait_until="networkidle")

    def size_preset(idx):
        page.evaluate(
            "(() => { const g = document.querySelectorAll('.conf-presets');"
            f" g[g.length-1].querySelectorAll('button')[{idx}].click(); }})()"
        )
        return (
            page.input_value("#min-size"),
            page.input_value("#max-size"),
            page.inner_text("#size-label"),
        )

    # 大小预设：挡位 → 标签必须严丝合缝
    assert size_preset(0) == ("0", "9", "不限")
    assert size_preset(1) == ("2", "9", "≥ 300 MB")
    assert size_preset(2) == ("5", "9", "≥ 3 GB")
    assert size_preset(3) == ("6", "9", "≥ 5 GB")

    # 预设落到 start() 里的真实字节数（0 = 该端不限制）
    size_preset(1)
    sent = page.evaluate(
        """(() => {
      const mi = parseInt(document.getElementById('min-size').value, 10);
      const ma = parseInt(document.getElementById('max-size').value, 10);
      return [LMMSize.sizeToBytes(mi, true), LMMSize.sizeToBytes(ma, false)];
    })()"""
    )
    assert sent == [300 * MB, 0], f"≥300MB 实际发出 {sent}"

    # 置信度预设：区间必须原样落位，不能被 step 吸附
    page.evaluate("document.querySelectorAll('.conf-presets button')[3].click()")
    assert page.input_value("#min-conf") == "60"
    assert page.input_value("#max-conf") == "69", "60-69 被 step 吸附了"
    assert page.inner_text("#conf-label") == "60 - 69"

    assert not page.js_errors, "页面 JS 报错：\n" + "\n".join(page.js_errors)
