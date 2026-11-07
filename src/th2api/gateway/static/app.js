/* TH2API panel: vanilla JS, no build step. Talks to /api/* with X-Admin-Token. */

const state = {
  token: localStorage.getItem('th2api_token') || '',
  view: 'overview',
  logsTimer: null,
  jobTimer: null,
};

/* ---------- plumbing ---------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': state.token,
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    logout();
    throw new Error('admin token 无效');
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const rel = (ts) => {
  if (!ts) return '—';
  const seconds = Math.round((Date.now() / 1000) - ts);
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h 前`;
  return `${Math.round(seconds / 86400)}d 前`;
};
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—');
const badge = (text, cls = '') => `<span class="pill ${cls}">${esc(text)}</span>`;

/* ---------- auth ---------- */

function showApp() {
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  switchView('overview');
  loadModels();
}

function logout() {
  localStorage.removeItem('th2api_token');
  state.token = '';
  $('app').classList.add('hidden');
  $('login').classList.remove('hidden');
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const token = $('token-input').value.trim();
  $('login-error').textContent = '';
  try {
    const response = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (!response.ok) throw new Error('token 不正确');
    state.token = token;
    localStorage.setItem('th2api_token', token);
    showApp();
  } catch (error) {
    $('login-error').textContent = error.message;
  }
});

$('logout').addEventListener('click', logout);

/* ---------- views ---------- */

const titles = {
  overview: '概览',
  accounts: '账号池',
  keys: '网关密钥',
  logs: '请求日志',
  register: '批量注册',
  test: '在线测试',
};

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.nav-item').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach((section) => {
    section.classList.toggle('hidden', section.id !== `view-${view}`);
  });
  $('view-title').textContent = titles[view];
  render();
}

document.querySelectorAll('.nav-item').forEach((button) => {
  button.addEventListener('click', () => switchView(button.dataset.view));
});
$('refresh-view').addEventListener('click', render);

async function render() {
  try {
    if (state.view === 'overview') await renderOverview();
    if (state.view === 'accounts') await renderAccounts();
    if (state.view === 'keys') await renderKeys();
    if (state.view === 'logs') await renderLogs();
    if (state.view === 'test') await renderTest();
  } catch (error) {
    $('platform-error');
    console.error(error);
  }
  $('refreshed-at').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
}

/* ---------- overview ---------- */

async function renderOverview() {
  const data = await api('/api/overview');
  const accounts = data.accounts;
  $('stat-cards').innerHTML = `
    <div class="stat"><div class="label">账号</div><div class="value">${accounts.total}</div>
      <div class="sub">${accounts.enabled} 启用 · ${accounts.disabled} 停用 · ${accounts.cooling} 冷却</div></div>
    <div class="stat"><div class="label">可走免费额度</div><div class="value">${accounts.free_ready}</div>
      <div class="sub">${accounts.exhausted} 已耗尽 · 均值 ${accounts.avg_free_pct ?? '—'}%</div></div>
    <div class="stat"><div class="label">24h 请求</div><div class="value">${data.requests_24h.total}</div>
      <div class="sub">成功 ${data.requests_24h.ok} · 均延迟 ${data.requests_24h.avg_latency_ms}ms</div></div>
    <div class="stat"><div class="label">可见模型</div><div class="value">${data.models}</div>
      <div class="sub">网关密钥 ${data.gateway_keys} 个</div></div>`;
  $('health-pill').textContent = `${accounts.enabled}/${accounts.total} 账号在线`;
  $('health-pill').className = `pill ${accounts.enabled ? 'ok' : 'bad'}`;

  const list = await api('/api/accounts');
  const rows = list.accounts.slice(0, 10).map((account) => {
    const status = !account.enabled
      ? badge('停用', 'bad')
      : account.free_exhausted
        ? badge('额度耗尽', 'warn')
        : account.cooldown_until > Date.now() / 1000
          ? badge('冷却中', 'warn')
          : badge('可用', 'ok');
    return `<tr>
      <td class="mono">${esc(account.email)}</td>
      <td>${status}</td>
      <td>${quotaBar(account)}</td>
      <td class="muted small wrap">${esc(account.last_error || '—')}</td>
      <td class="muted small">${rel(account.last_used_at)}</td>
    </tr>`;
  });
  $('health-table').innerHTML = rows.length
    ? `<thead><tr><th>邮箱</th><th>状态</th><th>免费额度</th><th>最近错误</th><th>最近使用</th></tr></thead><tbody>${rows.join('')}</tbody>`
    : '<tbody><tr><td class="empty">还没有账号，去「批量注册」跑一次。</td></tr></tbody>';
}

function quotaBar(account) {
  if (account.free_pct === null || account.free_pct === undefined) {
    return '<span class="muted small">未测量</span>';
  }
  const pct = Math.max(0, Math.min(100, account.free_pct));
  const cls = pct >= 95 ? 'bar bad' : pct >= 70 ? 'bar warn' : 'bar';
  return `<div class="${cls}"><i style="width:${pct}%"></i></div><span class="muted small">${pct}%</span>`;
}

/* ---------- accounts ---------- */

async function renderAccounts() {
  const data = await api('/api/accounts');
  $('accounts-hint').textContent = `共 ${data.accounts.length} 个账号`;
  if (!data.accounts.length) {
    $('accounts-table').innerHTML = '<tbody><tr><td class="empty">空。先到「批量注册」创建账号。</td></tr></tbody>';
    return;
  }
  const rows = data.accounts.map((account) => {
    const cooling = account.cooldown_until > Date.now() / 1000
      ? ` <span class="muted small">(${Math.ceil(account.cooldown_until - Date.now() / 1000)}s)</span>`
      : '';
    return `<tr>
      <td class="mono">${esc(account.email)}</td>
      <td class="mono">${esc(account.api_key)}</td>
      <td>${account.free_models_enabled ? badge('已开', 'ok') : badge('未开', 'warn')}</td>
      <td>${quotaBar(account)}${cooling}</td>
      <td>${account.enabled ? badge('启用', 'ok') : badge('停用', 'bad')}</td>
      <td class="small">${account.ok_count} / ${account.err_count}</td>
      <td class="small muted wrap">${esc(account.last_error || '—')}</td>
      <td>
        <button class="ghost" data-action="refresh" data-email="${esc(account.email)}">刷新额度</button>
        <button class="ghost" data-action="toggle" data-email="${esc(account.email)}" data-enabled="${account.enabled ? 0 : 1}">${account.enabled ? '停用' : '启用'}</button>
        <button class="danger" data-action="delete" data-email="${esc(account.email)}">删除</button>
      </td>
    </tr>`;
  });
  $('accounts-table').innerHTML =
    '<thead><tr><th>邮箱</th><th>API Key</th><th>免费模型</th><th>额度</th><th>状态</th><th>成功/失败</th><th>最近错误</th><th>操作</th></tr></thead>' +
    `<tbody>${rows.join('')}</tbody>`;
}

$('accounts-table').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const { action, email, enabled } = button.dataset;
  button.disabled = true;
  try {
    if (action === 'refresh') {
      await api(`/api/accounts/${encodeURIComponent(email)}/refresh`, { method: 'POST' });
    } else if (action === 'toggle') {
      await api(`/api/accounts/${encodeURIComponent(email)}/toggle`, {
        method: 'POST',
        body: JSON.stringify({ enabled: enabled === '1' }),
      });
    } else if (action === 'delete') {
      if (!confirm(`删除 ${email}？记录会被打上墓碑标记，key 立即失效。`)) return;
      await api(`/api/accounts/${encodeURIComponent(email)}`, { method: 'DELETE' });
    }
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    render();
  }
});

$('refresh-quota').addEventListener('click', async (event) => {
  event.target.disabled = true;
  event.target.textContent = '刷新中…';
  try {
    const data = await api('/api/accounts/refresh', { method: 'POST' });
    const failed = data.results.filter((item) => item.error).length;
    alert(`已刷新 ${data.results.length} 个账号，失败 ${failed} 个。`);
  } catch (error) {
    alert(error.message);
  } finally {
    event.target.disabled = false;
    event.target.textContent = '刷新全部免费额度';
    render();
  }
});

/* ---------- keys ---------- */

async function renderKeys() {
  const data = await api('/api/keys');
  $('base-url').textContent = `${location.origin}/v1`;
  if (!data.keys.length) {
    $('keys-table').innerHTML = '<tbody><tr><td class="empty">还没有网关密钥。</td></tr></tbody>';
    return;
  }
  const rows = data.keys.map((key) => `<tr>
    <td>${esc(key.name)}</td>
    <td class="mono">${esc(key.preview)}</td>
    <td>${key.enabled ? badge('启用', 'ok') : badge('停用', 'bad')}</td>
    <td class="small">${key.requests}</td>
    <td class="small muted">${rel(key.last_used_at)}</td>
    <td><button class="danger" data-key="${esc(key.id)}">删除</button></td>
  </tr>`);
  $('keys-table').innerHTML =
    '<thead><tr><th>名称</th><th>密钥</th><th>状态</th><th>请求数</th><th>最近使用</th><th></th></tr></thead>' +
    `<tbody>${rows.join('')}</tbody>`;
}

$('keys-table').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-key]');
  if (!button) return;
  if (!confirm('删除这个网关密钥？使用它的客户端会立刻 401。')) return;
  await api(`/api/keys/${button.dataset.key}`, { method: 'DELETE' });
  render();
});

$('key-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const created = await api('/api/keys', {
    method: 'POST',
    body: JSON.stringify({ name: $('key-name').value || 'default' }),
  });
  $('new-key').classList.remove('hidden');
  $('new-key').textContent = created.key;
  $('key-name').value = '';
  renderKeys();
});

/* ---------- logs ---------- */

async function renderLogs() {
  const data = await api('/api/logs?limit=100');
  if (!data.logs.length) {
    $('logs-table').innerHTML = '<tbody><tr><td class="empty">还没有请求。</td></tr></tbody>';
  } else {
    const rows = data.logs.map((log) => {
      const ok = log.status >= 200 && log.status < 300;
      return `<tr>
        <td class="mono">${clock(log.ts)}</td>
        <td class="mono">${esc(log.model || '—')}</td>
        <td>${badge(log.status ?? '—', ok ? 'ok' : 'bad')}</td>
        <td class="mono small">${esc((log.account || '').replace(/^(.{2}).*(@.*)$/, '$1***$2'))}</td>
        <td class="small">${log.latency_ms ?? '—'}ms${log.ttft_ms ? ` / 首字 ${log.ttft_ms}ms` : ''}</td>
        <td class="small">${(log.prompt_tokens ?? 0) + (log.completion_tokens ?? 0) || '—'}</td>
        <td class="small muted wrap">${esc(log.error || '')}</td>
      </tr>`;
    });
    $('logs-table').innerHTML =
      '<thead><tr><th>时间</th><th>模型</th><th>状态</th><th>账号</th><th>耗时</th><th>tokens</th><th>错误</th></tr></thead>' +
      `<tbody>${rows.join('')}</tbody>`;
  }
  clearTimeout(state.logsTimer);
  if ($('logs-live').checked && state.view === 'logs') {
    state.logsTimer = setTimeout(renderLogs, 5000);
  }
}

$('logs-live').addEventListener('change', renderLogs);

/* ---------- register ---------- */

$('reg-provider').addEventListener('change', (event) => {
  const forward = event.target.value === 'domain-forward';
  document.querySelectorAll('.df-field').forEach((field) => field.classList.toggle('hidden', !forward));
});

$('register-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const body = {
    count: Number($('reg-count').value || 1),
    label: $('reg-label').value || 'th2api',
    proxy: $('reg-proxy').value || undefined,
    provider: $('reg-provider').value,
    domain: $('reg-domain').value || undefined,
    forward_to: $('reg-forward').value || undefined,
    pin: $('reg-pin').value || undefined,
  };
  try {
    const job = await api('/api/register', { method: 'POST', body: JSON.stringify(body) });
    $('job-pill').textContent = '运行中';
    $('job-pill').className = 'pill warn';
    pollJob(job.id);
  } catch (error) {
    $('job-console').textContent = `启动失败：${error.message}`;
  }
});

async function pollJob(jobId) {
  clearTimeout(state.jobTimer);
  try {
    const job = await api(`/api/jobs/${jobId}`);
    $('job-console').textContent = job.lines.join('\n') || '等待输出…';
    $('job-console').scrollTop = $('job-console').scrollHeight;
    $('job-pill').textContent = `${job.status} · 成功 ${job.created} · 失败 ${job.failed}`;
    $('job-pill').className = `pill ${job.status === 'running' ? 'warn' : job.status === 'completed' ? 'ok' : 'bad'}`;
    if (job.status === 'running') {
      state.jobTimer = setTimeout(() => pollJob(jobId), 2000);
    } else {
      render();
    }
  } catch (error) {
    $('job-console').textContent += `\n${error.message}`;
  }
}

/* ---------- test ---------- */

async function loadModels() {
  try {
    const data = await api('/api/models');
    $('model-list').innerHTML = data.models.map((model) => `<option value="${esc(model)}"></option>`).join('');
  } catch (error) {
    /* the panel works without a model list */
  }
}

async function renderTest() {
  loadModels();
}

$('test-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const box = $('test-result');
  box.classList.remove('hidden');
  box.innerHTML = '<p class="muted">请求中…</p>';
  try {
    const result = await api('/api/test', {
      method: 'POST',
      body: JSON.stringify({ model: $('test-model').value, prompt: $('test-prompt').value }),
    });
    const tokens = result.usage ? `${result.usage.prompt_tokens}/${result.usage.completion_tokens}` : '—';
    box.innerHTML = `
      <div class="meta">
        ${badge(result.status, result.status === 200 ? 'ok' : 'bad')}
        <span>账号 ${esc(result.account)}</span>
        <span>延迟 ${result.latency_ms}ms</span>
        <span>tokens ${tokens}</span>
      </div>
      <div class="content">${esc(result.content || JSON.stringify(result.error, null, 2) || '（空）')}</div>`;
  } catch (error) {
    box.innerHTML = `<div class="content">${esc(error.message)}</div>`;
  }
});

/* ---------- boot ---------- */

if (state.token) {
  api('/api/overview').then(showApp).catch(() => logout());
}