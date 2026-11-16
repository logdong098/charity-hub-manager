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
    for (let i = 0; i < 40; i += 1) { await new Promise((r) => setTimeout(r, 5000)); const data = await loadModels(); if (!data.running) break; }
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; button.textContent = '◎ 立即探测'; }
});
$('#probeSaveBtn').addEventListener('click', async () => {
  try { await api('/api/models/settings', { method: 'PUT', body: JSON.stringify({ enabled: $('#probeEnabled').checked, interval_minutes: Number($('#probeInterval').value) || 60 }) }); toast('探测设置已保存'); } catch (error) { toast(error.message, true); }
});

// 登录后、手动刷新时 app.js 会调用 reload()，在其后顺带刷新模型面板。
const baseReload = reload;
reload = async () => { await baseReload(); loadModels().catch((error) => toast(error.message, true)); };
if (modelsAuthed()) loadModels().catch((error) => toast(error.message, true));
setInterval(() => { if (modelsAuthed()) loadModels().catch(() => {}); }, 60000);
