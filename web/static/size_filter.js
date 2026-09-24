/**
 * 文件大小滑块的取值映射。
 *
 * 抽成独立模块是因为这里有个**只靠肉眼看不出来**的坑：
 * 后端约定「0 字节 = 该端不限制」，而滑块最左挡的值恰好就是 0。
 * 如果最大值的滑块允许停在最左挡，界面上写着「最大 0 MB」，
 * 实际却变成全放行 —— 标签和行为的含义正好相反。
 *
 * 所以：
 *   - 最小值的第 0 挡 = 0     → 不限（不设下限）
 *   - 最大值的第 9 挡 = 51200 → 不限（不设上限）
 *   - 最大值的可选范围从第 1 挡起步，永远碰不到 0
 *
 * 挡位取 1024 的整数倍，是为了让「≥3GB」这类按钮标注和实际应用的
 * 字节数严格相等。之前用 3000/5000/10000 这种十进制整千，界面上写
 * 「≥3GB」实际却应用了 5000MB —— 标注与行为对不上。
 */

const SIZE_STOPS = [
  0,        // 0      → 不限（仅最小值端）
  100,      // 100 MB
  300,      // 300 MB
  700,      // 700 MB
  1536,     // 1.5 GB
  3072,     // 3 GB
  5120,     // 5 GB
  10240,    // 10 GB
  20480,    // 20 GB
  51200,    // 50 GB → 不限（仅最大值端）
];

const MB = 1024 * 1024;

/** 人类可读的档位文本。 */
function sizeLabel(idx, isMin) {
  const mb = SIZE_STOPS[idx];

  if (isMin && idx === 0) return '不限';
  if (!isMin && idx === SIZE_STOPS.length - 1) return '不限';

  if (mb >= 1024) {
    const gb = mb / 1024;
    if (Number.isInteger(gb)) return gb + ' GB';
    return gb.toFixed(1) + ' GB';
  }

  return mb + ' MB';
}

/**
 * 档位 → 字节数。0 表示该端不限制。
 *
 * 注意两个方向的不限落在**不同的**挡位上，不是对称的。
 */
function sizeToBytes(idx, isMin) {
  if (isMin) {
    return idx === 0 ? 0 : SIZE_STOPS[idx] * MB;
  }

  return idx === SIZE_STOPS.length - 1 ? 0 : SIZE_STOPS[idx] * MB;
}

/** 两挡合成一句人话，用于界面上那行汇总。 */
function sizeSummary(loIdx, hiIdx) {
  const loText = sizeLabel(loIdx, true);
  const hiText = sizeLabel(hiIdx, false);

  if (loText === '不限' && hiText === '不限') return '不限';
  if (hiText === '不限') return '≥ ' + loText;
  if (loText === '不限') return '≤ ' + hiText;

  return loText + ' - ' + hiText;
}

// 浏览器里挂到 window；node 里走 module.exports 供测试用。
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { SIZE_STOPS, sizeLabel, sizeToBytes, sizeSummary };
}

if (typeof window !== 'undefined') {
  window.LMMSize = { SIZE_STOPS, sizeLabel, sizeToBytes, sizeSummary };
}
