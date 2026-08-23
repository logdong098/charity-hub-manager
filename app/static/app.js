const $ = (selector) => document.querySelector(selector);
const state = { sites: [], accounts: new Map(), pendingImport: null };
let authenticated = false;

const esc = (value = '') => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const api = async (url, options = {}) => {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { authenticated = false; showLogin(); }
  if (!response.ok) throw new Error(data.detail || data.message || `请求失败（${response.status}）`);
  return data;
};
const toast = (message, error = false) => { const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2800); };
const fmtTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const money = (value) => value == null ? '—' : Number(value).toFixed(2);
const showLogin = () => { $('#authGate').classList.add('visible'); setTimeout(() => $('#loginPassword').focus(), 80); };
const hideLogin = () => { $('#authGate').classList.remove('visible'); $('#loginPassword').value = ''; $('#loginHint').textContent = ''; };

async function loadDashboard() {
  const data = await api('/api/dashboard');
  state.sites = data.sites;
  $('#statSites').textContent = data.stats.sites;
  $('#statAccounts').textContent = data.stats.accounts;
  $('#statActive').textContent = data.stats.active_accounts;
  $('#statChecked').textContent = data.stats.checked_today;
  $('#statFailures').textContent = data.stats.failures_today;
  $('#scheduleEnabled').checked = data.schedule.enabled;
  $('#scheduleTime').value = data.schedule.checkin_time || '02:00';
  const stateEl = $('#scheduleState'); stateEl.textContent = data.schedule.enabled ? `每日 ${data.schedule.checkin_time || '02:00'}` : '已暂停'; stateEl.classList.toggle('off', !data.schedule.enabled);
  renderSites();
}

function renderSites() {
  const grid = $('#siteGrid');
  if (!state.sites.length) { grid.innerHTML = '<div class="empty">还没有站点，点击右上角添加第一个公益站。</div>'; return; }
  grid.innerHTML = state.sites.map((site) => `
    <article class="site-card ${site.status === 'disabled' ? 'disabled' : ''}" data-site="${site.id}">
      <div class="site-top"><a class="site-title site-title-link" href="${esc(site.url)}" target="_blank" rel="noopener noreferrer"><div class="site-icon">✦</div><div><div class="site-name">${esc(site.name)}</div><div class="site-url">${esc(site.url)}</div></div></a><span class="pill ${site.status === 'disabled' ? 'off' : ''}">${site.status === 'disabled' ? '已停用' : '运行中'}</span></div>
      <div class="site-meta"><div class="meta-box"><span>账号</span><b>${site.account_count}</b></div><div class="meta-box"><span>活跃</span><b>${site.healthy_count}</b></div><div class="meta-box"><span>平台</span><b>${esc(site.platform).replace('new-api', 'New API')}</b></div></div>
      <div class="site-actions"><a class="mini-btn site-link" href="${esc(site.url)}" target="_blank" rel="noopener noreferrer">↗ 打开站点</a><button class="mini-btn add-account" data-id="${site.id}">＋ 添加账号</button><button class="mini-btn show-accounts" data-id="${site.id}">查看账号</button><button class="mini-btn toggle-site" data-id="${site.id}">${site.status === 'active' ? '停用' : '启用'}</button><button class="mini-btn danger delete-site" data-id="${site.id}">删除</button></div>
      <div class="account-list" id="accounts-${site.id}"></div>
    </article>`).join('');
}

async function loadAccounts(siteId) {
  const list = await api(`/api/sites/${siteId}/accounts`); state.accounts.set(siteId, list);
  const el = $(`#accounts-${siteId}`); if (!el) return;
  el.innerHTML = list.length ? list.map((account, index) => `<div class="account-row"><div class="account-avatar">${String(index + 1).padStart(2, '0')}</div><div class="account-main"><b>${esc(account.username || `账号 ${index + 1}`)}</b><small><span class="credential-tag">${esc(account.credential_type)}</span><span>余额 ${money(account.balance)}</span><span>更新 ${fmtTime(account.last_balance_at)}</span></small></div><span class="account-state ${account.status === 'active' ? '' : 'bad'}">${account.status === 'active' ? '正常' : '停用'}</span><div class="account-actions"><button class="account-action checkin-account" data-id="${account.id}" title="立即签到">签到</button><button class="account-action refresh-account" data-id="${account.id}" title="刷新余额">刷新余额</button><button class="account-action danger delete-account" data-id="${account.id}" title="删除账号">删除</button></div></div>`).join('') : '<div class="account-empty">暂无账号，点击上方“添加账号”开始。</div>';
}

async function loadLogs() {
  const logs = await api('/api/logs?limit=10');
  $('#logList').innerHTML = logs.length ? logs.map((log) => `<div class="log-item"><div class="log-time">${fmtTime(log.created_at)}</div><div><div class="log-site">${esc(log.site_name)} · ${esc(log.username || '账号')}</div><div class="log-msg">${esc(log.message)}</div></div><span class="log-status ${log.status === 'failed' ? 'failed' : ''}">${log.status === 'success' ? '成功' : log.status === 'skipped' ? '跳过' : '失败'}</span></div>`).join('') : '<div class="empty">还没有签到记录</div>';
}

async function reload() { try { await Promise.all([loadDashboard(), loadLogs()]); } catch (error) { toast(error.message, true); } }

$('#refreshBtn').addEventListener('click', reload);
$('#addSiteBtn').addEventListener('click', () => { $('#siteForm').reset(); $('#siteHint').textContent = ''; $('#siteDialog').showModal(); });
$('#logoutBtn').addEventListener('click', async () => { await fetch('/api/auth/logout', { method: 'POST' }); authenticated = false; showLogin(); });
$('#loginForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const hint = $('#loginHint'); hint.textContent = '正在登录…'; hint.classList.remove('error');
  try {
    const response = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: $('#loginUsername').value, password: $('#loginPassword').value }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '登录失败');
    authenticated = true; hideLogin(); toast('登录成功'); await reload();
  } catch (error) { hint.textContent = error.message; hint.classList.add('error'); }
});
$('#exportBtn').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/config/export');
    if (!response.ok) throw new Error('导出失败');
    const blob = await response.blob();
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'charity-hub-config.json'; link.click(); URL.revokeObjectURL(link.href);
    toast('配置已导出，请妥善保管文件');
  } catch (error) { toast(error.message, true); }
});
$('#importBtn').addEventListener('click', () => { state.pendingImport = null; $('#configFile').value = ''; $('#importPreview').textContent = '请选择文件后查看导入预览。'; $('#configHint').textContent = ''; $('#previewImportBtn').disabled = true; $('#confirmImportBtn').disabled = true; $('#configDialog').showModal(); });
$('#closeConfigBtn').addEventListener('click', () => $('#configDialog').close());
$('#configFile').addEventListener('change', (event) => { $('#previewImportBtn').disabled = !event.target.files?.[0]; $('#confirmImportBtn').disabled = true; $('#importPreview').textContent = event.target.files?.[0] ? '文件已选择，点击“解析预览”确认内容。' : '请选择文件后查看导入预览。'; });
$('#previewImportBtn').addEventListener('click', async () => {
  const file = $('#configFile').files?.[0]; if (!file) return;
  try {
    const data = JSON.parse(await file.text()); state.pendingImport = data;
    const preview = await api('/api/config/import/preview', { method: 'POST', body: JSON.stringify({ data }) });
    $('#importPreview').innerHTML = preview.ok ? `<b>可导入 ${preview.sites} 个站点、${preview.accounts} 个账号</b><small>${preview.site_names.map(esc).join('、')}${preview.sites > preview.site_names.length ? ' 等' : ''}</small>` : `<b class="preview-error">${esc(preview.message)}</b>`;
    $('#configHint').textContent = preview.warnings?.length ? preview.warnings.join('；') : '确认后将采用合并模式，已有相同凭据的账号会自动跳过。';
    $('#configHint').classList.toggle('error', !preview.ok); $('#confirmImportBtn').disabled = !preview.ok;
  } catch (error) { state.pendingImport = null; $('#importPreview').textContent = '无法解析这个 JSON 文件。'; $('#configHint').textContent = error.message; $('#configHint').classList.add('error'); $('#confirmImportBtn').disabled = true; }
});
$('#confirmImportBtn').addEventListener('click', async () => {
  if (!state.pendingImport || !confirm('确认导入这份配置吗？已有相同凭据的账号会跳过，当前数据不会被删除。')) return;
  try { const result = await api('/api/config/import', { method: 'POST', body: JSON.stringify({ data: state.pendingImport, mode: 'merge' }) }); $('#configDialog').close(); toast(`导入完成：${result.inserted_sites} 个新站点，${result.inserted_accounts} 个新账号`); await reload(); } catch (error) { $('#configHint').textContent = error.message; $('#configHint').classList.add('error'); }
});
$('#siteForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = new FormData(event.currentTarget); const body = Object.fromEntries(form.entries());
  try { await api('/api/sites', { method: 'POST', body: JSON.stringify(body) }); $('#siteDialog').close(); toast('站点已添加'); await reload(); } catch (error) { $('#siteHint').textContent = error.message; $('#siteHint').classList.add('error'); }
});
$('#probeBtn').addEventListener('click', async () => { const form = new FormData($('#siteForm')); const hint = $('#siteHint'); hint.classList.remove('error'); hint.textContent = '正在测试站点…'; try { const result = await api('/api/sites/probe', { method: 'POST', body: JSON.stringify(Object.fromEntries(form.entries())) }); hint.textContent = result.ok ? `连接成功：${result.message}` : `连接失败：${result.message}`; hint.classList.toggle('error', !result.ok); } catch (error) { hint.textContent = error.message; hint.classList.add('error'); } });
$('#credentialType').addEventListener('change', (event) => { $('#credentialLabel').firstChild.textContent = event.target.value === 'password' ? '登录密码' : event.target.value === 'cookie' ? 'Cookie / Session' : 'Access Token'; });
$('#accountForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = new FormData(event.currentTarget); const body = Object.fromEntries(form.entries()); body.site_id = Number(body.site_id); body.checkin_enabled = form.get('checkin_enabled') === 'on'; if (body.platform_user_id) body.platform_user_id = Number(body.platform_user_id); else delete body.platform_user_id;
  try { await api('/api/accounts', { method: 'POST', body: JSON.stringify(body) }); $('#accountDialog').close(); toast('账号已安全保存'); await reload(); } catch (error) { $('#accountHint').textContent = error.message; $('#accountHint').classList.add('error'); }
});
$('#scheduleForm').addEventListener('submit', async (event) => { event.preventDefault(); try { await api('/api/schedule', { method: 'PUT', body: JSON.stringify({ enabled: $('#scheduleEnabled').checked, checkin_time: $('#scheduleTime').value }) }); toast('自动化设置已保存'); await loadDashboard(); } catch (error) { toast(error.message, true); } });
$('#checkinAllBtn').addEventListener('click', async () => { if (!confirm('确定立即对所有启用账号执行签到吗？')) return; const button = $('#checkinAllBtn'); button.disabled = true; button.textContent = '执行中…'; try { const result = await api('/api/checkin/all', { method: 'POST' }); toast(`完成：${result.success}/${result.total} 个账号成功`); await reload(); } catch (error) { toast(error.message, true); } finally { button.disabled = false; button.textContent = '⚡ 立即签到全部'; } });

$('#siteGrid').addEventListener('click', async (event) => {
  const button = event.target.closest('button'); if (!button) return; const id = Number(button.dataset.id);
  try {
    if (button.classList.contains('show-accounts')) { const box = $(`#accounts-${id}`); if (box.innerHTML) box.innerHTML = ''; else await loadAccounts(id); }
    if (button.classList.contains('add-account')) { const site = state.sites.find((item) => item.id === id); $('#accountForm').reset(); $('#accountForm [name=site_id]').value = id; $('#accountSiteName').textContent = site?.name || ''; $('#accountHint').textContent = ''; $('#accountDialog').showModal(); }
    if (button.classList.contains('toggle-site')) { await api(`/api/sites/${id}/toggle`, { method: 'POST' }); toast('站点状态已更新'); await reload(); }
    if (button.classList.contains('delete-site') && confirm('删除站点会同时删除其账号与签到记录，确认继续？')) { await api(`/api/sites/${id}`, { method: 'DELETE' }); toast('站点已删除'); await reload(); }
    if (button.classList.contains('checkin-account')) { const result = await api(`/api/accounts/${id}/checkin`, { method: 'POST' }); toast(result.message, result.status === 'failed'); await reload(); const site = state.sites.find((item) => item.id === result.site_id); if (site) await loadAccounts(site.id); }
    if (button.classList.contains('refresh-account')) { const result = await api(`/api/accounts/${id}/refresh`, { method: 'POST' }); toast(`余额已刷新：${money(result.balance)}`); await reload(); }
    if (button.classList.contains('delete-account') && confirm('确认删除这个账号？')) { await api(`/api/accounts/${id}`, { method: 'DELETE' }); toast('账号已删除'); await reload(); }
  } catch (error) { toast(error.message, true); }
});

async function bootstrap() {
  try { const status = await fetch('/api/auth/status').then((response) => response.json()); authenticated = status.authenticated === true; if (authenticated) await reload(); else showLogin(); } catch { showLogin(); }
}
bootstrap();
setInterval(() => { if (authenticated) { loadDashboard().catch(() => {}); loadLogs().catch(() => {}); } }, 60000);

document.querySelectorAll('.nav-item').forEach((link) => link.addEventListener('click', (event) => {
  const target = document.querySelector(link.getAttribute('href'));
  if (!target) return;
  event.preventDefault();
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.remove('active'));
  link.classList.add('active');
}));
