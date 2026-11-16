// 最新模型可用性面板：依赖 app.js 中的 $ / api / esc / toast / fmtTime / reload。
// 门户模式（由统一门户负责登录）下 app.js 没有 authenticated 变量，视为已登录。
const modelsAuthed = () => typeof authenticated === 'undefined' || authenticated;
const STATUS_TEXT = { ok: '可用', rate_limited: '限流', auth: '鉴权/额度', unavailable: '无此模型', error: '失败' };

async function loadModels() {
  const data = await api('/api/models/status');
  $('#probeInterval').value = data.interval_minutes;
  $('#probeEnabled').checked = data.enabled;
  const meta = [`上次探测 ${fmtTime(data.last_run)}`, data.provider_count != null ? `${data.provider_count} 个上游站点` : '', data.running ? '探测中…' : ''].filter(Boolean).join(' · ');
  $('#modelMeta').textContent = meta;
  const alerts = [];
  if (!data.configured) alerts.push('未配置 CPA_BASE_URL / CPA_MANAGEMENT_KEY，无法读取公益站上游配置。');
  if (data.last_error) alerts.push(`上次探测失败：${data.last_error}`);
  const down = (data.provider_errors || []).filter((e) => e.message.startsWith('站点不可达'));
  if (down.length) alerts.push(`不可达站点：${down.map((e) => e.provider).join('、')}`);
  const cards = data.families.map((family) => {
    const rows = family.models.length ? family.models.map((m) => `
      <div class="model-row">
        <div class="model-name"><span>${esc(m.model)}</span><span class="model-count ${m.available ? '' : 'none'}">${m.available}/${m.providers.length} 可用</span></div>
        <div class="provider-chips">${m.providers.map((p) => `<span class="chip ${esc(p.status)}" title="${esc(STATUS_TEXT[p.status] || p.status)}${p.http_status ? ` · HTTP ${p.http_status}` : ''}\n${esc(p.message)}${p.last_ok_at ? `\n上次可用 ${fmtTime(p.last_ok_at)}` : ''}">${esc(p.provider)}${p.status === 'ok' && p.latency_ms != null ? ` · ${(p.latency_ms / 1000).toFixed(1)}s` : ''}</span>`).join('')}</div>
      </div>`).join('') : '<div class="model-empty">没有站点提供该系列模型</div>';
    return `<article class="model-family"><div class="model-family-head"><h3>${esc(family.label)}</h3><small>${family.latest ? `最新 v${esc(family.latest)}` : '—'}</small></div>${rows}</article>`;
  });
  $('#modelBoard').innerHTML = alerts.map((a) => `<div class="model-alert">${esc(a)}</div>`).join('') + cards.join('');
  return data;
}

$('#probeNowBtn').addEventListener('click', async () => {
  const button = $('#probeNowBtn'); button.disabled = true; button.textContent = '探测中…';
  try {
    const result = await api('/api/models/probe', { method: 'POST' });
    toast(result.message, !result.ok);
    for (let i = 0; i < 40; i += 1) { await new Promise((r) => setTimeout(r, 5000)); const data = await loadModels(); if (!data.running) { await loadCpaSync(); break; } }
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; button.textContent = '◎ 立即探测'; }
});
$('#probeSaveBtn').addEventListener('click', async () => {
  try { await api('/api/models/settings', { method: 'PUT', body: JSON.stringify({ enabled: $('#probeEnabled').checked, interval_minutes: Number($('#probeInterval').value) || 60 }) }); toast('探测设置已保存'); } catch (error) { toast(error.message, true); }
});

const ACTION_TEXT = { add_provider: '接入站点', add_model: '加入模型', remove_model: '移除模型', disable_provider: '停用站点', enable_provider: '启用站点', rollback: '回滚', skip: '跳过', skip_site: '不再接入', error: '错误' };
async function loadCpaSync() {
  const data = await api('/api/cpa/sync');
  $('#cpaSyncEnabled').checked = data.enabled;
  $('#cpaSyncMeta').textContent = `测活后自动更新 CPA；连续失败 ${data.threshold} 次才移除模型或停用站点，恢复后自动加回`;
  $('#cpaSites').innerHTML = data.sites.map((s) => { const ok = (s.cpa_status || '').startsWith('已接入') || (s.cpa_status || '').startsWith('已创建') || (s.cpa_status || '').startsWith('使用'); return `<div class="cpa-item ${ok ? 'ok' : s.cpa_status ? 'warn' : ''}"><b>${esc(s.name)}</b><span>${esc(s.cpa_status || '等待下一轮同步')}</span></div>`; }).join('') || '<div class="model-empty">没有启用的签到站</div>';
  $('#cpaLogs').innerHTML = data.logs.map((l) => `<div class="cpa-item ${l.action === 'error' || l.action.startsWith('remove') || l.action.startsWith('disable') ? 'warn' : 'ok'}"><b>${fmtTime(l.created_at)} · ${esc(ACTION_TEXT[l.action] || l.action)} · ${esc(l.provider)}</b><span>${esc(l.detail)}</span></div>`).join('') || '<div class="model-empty">还没有变更</div>';
  $('#cpaBackups').innerHTML = data.backups.map((b) => `<div class="cpa-item"><b>#${b.id} · ${fmtTime(b.created_at)}</b><span>${esc(b.reason)} <button data-backup="${b.id}">回滚到此前</button></span></div>`).join('') || '<div class="model-empty">还没有备份</div>';
}
$('#cpaSyncEnabled').addEventListener('change', async (event) => { try { await api('/api/cpa/sync/settings', { method: 'PUT', body: JSON.stringify({ enabled: event.target.checked }) }); toast(event.target.checked ? '已启用 CPA 自动同步' : '已暂停 CPA 自动同步'); } catch (error) { toast(error.message, true); } });
$('#cpaBackups').addEventListener('click', async (event) => {
  const id = event.target.closest('button')?.dataset.backup; if (!id || !confirm(`确认把 CPA 的提供商列表回滚到备份 #${id} 记录的状态吗？回滚后会暂停自动同步。`)) return;
  try { await api(`/api/cpa/rollback/${id}`, { method: 'POST' }); toast('已回滚，自动同步已暂停'); await loadCpaSync(); } catch (error) { toast(error.message, true); }
});

// 登录后、手动刷新时 app.js 会调用 reload()，在其后顺带刷新模型面板。
const baseReload = reload;
const loadModelPanels = () => Promise.all([loadModels(), loadCpaSync()]);
reload = async () => { await baseReload(); loadModelPanels().catch((error) => toast(error.message, true)); };
if (modelsAuthed()) loadModelPanels().catch((error) => toast(error.message, true));
setInterval(() => { if (modelsAuthed()) loadModelPanels().catch(() => {}); }, 60000);
