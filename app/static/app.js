// 导航审查页面逻辑。
// 单调修订号守卫（createRevisionGuard）是“旧结果不得覆盖最新页面”的唯一裁决点，
// 同时被浏览器页面与 Node 验收测试复用。

function createRevisionGuard(initial) {
  let displayed = Number.isFinite(initial) ? initial : -1;
  let suppressed = 0;
  return {
    // 只接受修订号不小于当前已显示修订号的投影；旧修订响应一律抑制
    accept(state) {
      const rev = (state && Number.isFinite(state.revision)) ? state.revision : 0;
      if (rev < displayed) {
        suppressed += 1;
        return false;
      }
      displayed = rev;
      return true;
    },
    // 重置/重新建档后修订号回到 0，守卫基线也必须回到初始值
    reset(initial) { displayed = Number.isFinite(initial) ? initial : -1; suppressed = 0; },
    get displayed() { return displayed; },
    get suppressed() { return suppressed; },
  };
}

const guard = createRevisionGuard(-1);

function num(id) { return parseFloat(document.getElementById(id).value); }
function fmt(v, d) { d = (d === undefined) ? 4 : d; return (v === null || v === undefined) ? '—' : Number(v).toFixed(d); }
function fmtArr(a, d) { d = (d === undefined) ? 3 : d; return a ? '[' + a.map(v => fmt(v, d)).join(', ') + ']' : '—'; }

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

async function submitConfig() {
  const cfg = {
    initial_state: [num('cfgPx'), num('cfgPy'), num('cfgVx'), num('cfgVy')],
    initial_cov: [
      [num('cfgC0'), 0, 0, 0], [0, num('cfgC1'), 0, 0],
      [0, 0, num('cfgC2'), 0], [0, 0, 0, num('cfgC3')]
    ],
    process_noise: num('cfgQ'),
    measurement_noise: [[num('cfgRa'), num('cfgRb')], [num('cfgRb'), num('cfgRc')]],
    lag: parseInt(document.getElementById('cfgLag').value, 10),
    initial_timestamp: num('cfgT0')
  };
  const r = await api('/api/config', { method: 'POST', body: JSON.stringify(cfg) });
  document.getElementById('cfgErr').textContent = r.ok ? '' : (r.data.error || '建档失败');
  if (r.ok) {
    guard.reset();  // 新建档：修订号从 0 重新开始
    document.getElementById('staleNote').style.display = 'none';
    refresh();
  }
}

async function resetAll() {
  if (!confirm('确认清空日志与轨迹并重置？')) return;
  await api('/api/reset', { method: 'POST', body: '{}' });
  guard.reset();
  document.getElementById('staleNote').style.display = 'none';
  refresh();
}

async function submitObs() {
  const payload = {
    stable_id: document.getElementById('obsId').value.trim(),
    timestamp: num('obsT'), x: num('obsX'), y: num('obsY')
  };
  const r = await api('/api/observations', { method: 'POST', body: JSON.stringify(payload) });
  document.getElementById('obsErr').textContent = r.ok ? '' : (r.data.error || r.data.reason || '录入被拒绝');
  // 单条结论不直接上屏；整页投影一律以 GET /api/state 的权威结果为准
  await refresh();
}

function render(state) {
  const rev = state.revision || 0;
  document.getElementById('revPill').textContent = rev;
  document.getElementById('modePill').textContent = state.configured
    ? '已建档 L=' + (state.config ? state.config.lag : '?')
    : '未建档（默认配置，首录自动建档）';
  document.getElementById('curRev').textContent = rev;
  document.getElementById('curLeft').textContent =
    (state.window_left === null || state.window_left === undefined) ? '—' : fmt(state.window_left);
  document.getElementById('curLatest').textContent =
    (state.latest_time === null || state.latest_time === undefined) ? '—' : fmt(state.latest_time);
  document.getElementById('curPos').textContent = fmtArr(state.current_position, 4);
  document.getElementById('curCov').textContent = fmtArr(state.cov_diag, 5);
  document.getElementById('curErr').textContent = state.last_error || '无';
  document.getElementById('logCount').textContent = (state.log || []).length;
  document.getElementById('obsBtn').disabled = (state.log || []).length >= (state.capacity || 32);

  const labels = { accepted: '接受/重算', replayed: '回放', rejected: '拒绝' };
  const tb = document.querySelector('#logTable tbody');
  tb.innerHTML = '';
  (state.log || []).forEach(e => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${e.seq}</td><td>${escapeHtml(e.stable_id)}</td>
      <td class="mono">${fmt(e.timestamp)}</td>
      <td class="mono">(${fmt(e.x)},${fmt(e.y)})</td>
      <td><span class="tag ${e.decision}">${labels[e.decision] || e.decision}</span></td>
      <td class="mono">${e.revision || '—'}</td>
      <td class="mono">${fmtArr(e.position, 4)}</td>
      <td class="mono">${fmtArr(e.cov_diag, 4)}</td>
      <td class="mono">${e.residual ? fmtArr(e.residual, 4) : '—'}</td>
      <td style="color:${e.decision === 'rejected' ? 'var(--bad)' : 'var(--mut)'}">${escapeHtml(e.reason || '')}</td>`;
    tb.appendChild(tr);
  });

  const tt = document.querySelector('#trajTable tbody');
  tt.innerHTML = '';
  (state.trajectory || []).forEach(p => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="mono">${p.revision}</td>
      <td class="mono ${p.frozen ? 'frozen' : 'window-pt'}">${fmt(p.timestamp)}</td>
      <td class="mono">${fmtArr(p.position, 4)}</td>
      <td class="mono">${fmtArr(p.velocity, 4)}</td>
      <td class="mono">${fmtArr(p.cov_diag, 4)}</td>
      <td>${p.frozen ? '<span class="frozen">🔒 已封存</span>' : '<span class="window-pt">窗口后缀</span>'}</td>
      <td>${p.stable_id ? escapeHtml(p.stable_id) + ' #' + p.seq : '检查点'}</td>`;
    tt.appendChild(tr);
  });
  drawPlot(state.trajectory || []);
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function drawPlot(points) {
  const svg = document.getElementById('plot');
  svg.innerHTML = '';
  if (!points.length) return;
  const W = 600, H = 240, m = 34;
  const xs = points.map(p => p.position[0]), ys = points.map(p => p.position[1]);
  const minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
  const minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
  const sx = v => maxX === minX ? W / 2 : m + (v - minX) / (maxX - minX) * (W - 2 * m);
  const sy = v => maxY === minY ? H / 2 : H - m - (v - minY) / (maxY - minY) * (H - 2 * m);
  const win = points.filter(p => !p.frozen);
  if (win.length > 1) {
    const pl = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    pl.setAttribute('points', win.map(p => `${sx(p.position[0])},${sy(p.position[1])}`).join(' '));
    pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', '#60a5fa');
    pl.setAttribute('stroke-width', '1.5'); pl.setAttribute('opacity', '0.7');
    svg.appendChild(pl);
  }
  points.forEach(p => {
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', sx(p.position[0])); c.setAttribute('cy', sy(p.position[1]));
    c.setAttribute('r', p.frozen ? 5 : 6);
    c.setAttribute('fill', p.frozen ? '#94a3c4' : '#60a5fa');
    if (p.frozen) c.setAttribute('stroke', '#0f1420');
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `t=${p.timestamp} pos=(${p.position[0].toFixed(3)},${p.position[1].toFixed(3)}) rev=${p.revision} ${p.frozen ? '已封存' : '窗口'}`;
    c.appendChild(title); svg.appendChild(c);
  });
}

// 应用投影前执行单调修订号检查：旧修订结果（含计算/录入竞态期间的迟到响应）
// 不得覆盖最新页面。
function applyState(state) {
  if (!state) return false;
  if (!guard.accept(state)) {
    document.getElementById('staleNote').style.display = 'block';
    return false;
  }
  render(state);
  return true;
}

async function refresh() {
  const r = await api('/api/state');
  if (r.data) applyState(r.data);
}

// 计算期间继续录入：轮询可能撞上竞态下的旧修订快照，守卫统一抑制
if (typeof document !== 'undefined') {
  setInterval(refresh, 1500);
  refresh();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { createRevisionGuard, applyState };
}
