/**
 * 文件大小滑块映射 测试。
 *
 * 跑法：node tests/size_filter.test.js
 *
 * 这些用例锁住两个真实修过的 bug：
 *
 *   1. 最大值的滑块原本从第 0 挡起步，而第 0 挡的值是 0 —— 后端把 0 当
 *      「不限制」。于是界面上写着「最大 0 MB」，实际却是全放行，标签和
 *      行为的含义正好相反。
 *
 *   2. 预设按钮「≥3GB」原本指向第 6 挡 = 5000 MB，标注与实际应用的字节数
 *      对不上。根因是挡位取了 3000/5000 这种十进制整千。
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { SIZE_STOPS, sizeLabel, sizeToBytes, sizeSummary } =
  require('../web/static/size_filter.js');

const MB = 1024 * 1024;
const LAST = SIZE_STOPS.length - 1;

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  ok   ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL ' + name + '\n       ' + e.message);
  }
}

console.log('size_filter');

// ────────────── 最小端：第 0 挡 = 不限

test('最小 第0挡 = 0（不设下限）', () => {
  assert.strictEqual(sizeToBytes(0, true), 0);
  assert.strictEqual(sizeLabel(0, true), '不限');
});

test('最小 第1挡 = 100MB', () => {
  assert.strictEqual(sizeToBytes(1, true), 100 * MB);
});

test('最小 第6挡 = 5120MB = 5 GB', () => {
  assert.strictEqual(sizeToBytes(6, true), 5120 * MB);
  assert.strictEqual(sizeLabel(6, true), '5 GB');
});

test('最小 最后一挡 = 51200MB = 50 GB', () => {
  assert.strictEqual(sizeToBytes(LAST, true), 51200 * MB);
  assert.strictEqual(sizeLabel(LAST, true), '50 GB');
});

// ────────────── 最大端：最后一挡 = 不限，其余挡位都必须 > 0

test('最大 最后一挡 = 0（不设上限）', () => {
  assert.strictEqual(sizeToBytes(LAST, false), 0);
  assert.strictEqual(sizeLabel(LAST, false), '不限');
});

test('最大 第1挡 = 100MB（不是不限）', () => {
  assert.strictEqual(sizeToBytes(1, false), 100 * MB);
  assert.strictEqual(sizeLabel(1, false), '100 MB');
});

test('最大端除最后一挡外，其余可选挡位都不得映射成 0', () => {
  // 最大滑块的可选范围是 1..9（HTML min=1, max=9）；第 9 挡 = 不限是刻意的。
  for (let i = 1; i < LAST; i++) {
    assert.ok(
      sizeToBytes(i, false) > 0,
      `最大第 ${i} 挡映射成了 0 —— 会被后端当成"不限制"`
    );
  }
});

test('最大端第 0 挡在语义上等价于不限（故必须在 HTML 里禁选）', () => {
  // 记录这个坑本身：0 挡 = 0 字节 = 不限。
  // 真正防止它被选中的是 scan.html 里 max-size 的 min="1"，见下面那条。
  assert.strictEqual(sizeToBytes(0, false), 0);
  assert.strictEqual(sizeLabel(0, false), '0 MB');
});

// ────────────── 把 HTML 的滑块范围与这里的约定绑死

test('scan.html：最大滑块必须 min=1, max=9（碰不到第 0 挡）', () => {
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'web', 'templates', 'scan.html'), 'utf-8'
  );

  const m = html.match(/id="max-size"[^>]*/);

  assert.ok(m, 'scan.html 里找不到 max-size 滑块');

  const tag = m[0];

  assert.match(tag, /min="1"/,
    'max-size 的 min 不是 1 —— 用户能拉到第 0 挡，界面写着"0 MB"却全放行');
  assert.match(tag, /max="9"/, 'max-size 的 max 不是最后一挡');
});

test('scan.html：最小滑块从第 0 挡起步（= 不限）', () => {
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'web', 'templates', 'scan.html'), 'utf-8'
  );

  const m = html.match(/id="min-size"[^>]*/);

  assert.ok(m, 'scan.html 里找不到 min-size 滑块');
  assert.match(m[0], /min="0"/, 'min-size 的 min 不是 0');
});

test('scan.html：预设按钮指向的挡位与标注一致', () => {
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'web', 'templates', 'scan.html'), 'utf-8'
  );

  const want = [
    ['不限', 0],
    ['≥300MB', 300],
    ['≥3GB', 3 * 1024],
    ['≥5GB', 5 * 1024],
  ];

  const calls = [...html.matchAll(/presetSize\(\s*(\d+)\s*,\s*(\d+)\s*\)/g)];

  assert.strictEqual(calls.length, want.length,
    `预设按钮数量是 ${calls.length}，期望 ${want.length}`);

  want.forEach(([label, mb], i) => {
    const lo = Number(calls[i][1]);

    assert.strictEqual(SIZE_STOPS[lo], mb,
      `按钮「${label}」指向第 ${lo} 挡 = ${SIZE_STOPS[lo]} MB，期望 ${mb} MB`);
  });
});

// ────────────── 标签

test('标签：整数 GB 不带小数', () => {
  assert.strictEqual(sizeLabel(5, true), '3 GB');
  assert.strictEqual(sizeLabel(6, true), '5 GB');
  assert.strictEqual(sizeLabel(7, true), '10 GB');
  assert.strictEqual(sizeLabel(8, true), '20 GB');
});

test('标签：非整数 GB 保留一位小数', () => {
  assert.strictEqual(sizeLabel(4, true), '1.5 GB');
});

test('标签：1GB 以下用 MB', () => {
  assert.strictEqual(sizeLabel(1, true), '100 MB');
  assert.strictEqual(sizeLabel(2, true), '300 MB');
  assert.strictEqual(sizeLabel(3, true), '700 MB');
});

// ────────────── 汇总文案

test('两端都不限 → 不限', () => {
  assert.strictEqual(sizeSummary(0, LAST), '不限');
});

test('只有下限 → ≥ X', () => {
  assert.strictEqual(sizeSummary(2, LAST), '≥ 300 MB');
  assert.strictEqual(sizeSummary(6, LAST), '≥ 5 GB');
});

test('只有上限 → ≤ X', () => {
  assert.strictEqual(sizeSummary(0, 4), '≤ 1.5 GB');
});

test('区间 → X - Y', () => {
  assert.strictEqual(sizeSummary(2, 5), '300 MB - 3 GB');
});

// ────────────── 与后端约定一致

test('0 的语义：两端一致地表示"该端不限制"', () => {
  // 后端 core/scanner_v2.py: `if min_size and ...` / `if max_size and ...`
  // 0 是 falsy，所以跳过该端判断。
  assert.strictEqual(sizeToBytes(0, true), 0, '下限不限');
  assert.strictEqual(sizeToBytes(LAST, false), 0, '上限不限');
});

test('挡位单调递增', () => {
  for (let i = 1; i < SIZE_STOPS.length; i++) {
    assert.ok(SIZE_STOPS[i] > SIZE_STOPS[i - 1],
      `第 ${i} 挡 ${SIZE_STOPS[i]} 不大于前一挡 ${SIZE_STOPS[i - 1]}`);
  }
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');

process.exit(failed === 0 ? 0 : 1);
