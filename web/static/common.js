/* 共用的前端助手。
 *
 * 抽出来的原因：index.html 与 settings.html 都要用这几个 ——
 * 复制两份迟早会改一边忘一边（而且两个页面都得同步改）。
 */

function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : '{}'
  }).then(function (r) {
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  });
}

function closeModal(id) {
  var el = document.getElementById(id);
  if (el) { el.style.display = 'none'; }
}

function openModal(id) {
  var el = document.getElementById(id);
  if (el) { el.style.display = 'flex'; }
}

/* 点遮罩关闭：只有点在遮罩本身（不是卡片内部）才关 */
function backdropClose(ev, id) {
  if (ev.target === ev.currentTarget) { closeModal(id); }
}
