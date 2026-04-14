// ── State ────────────────────────────────────────────────────
let currentDays = 7;
let currentSource = '';
let currentTab = 'analytics';

// ── localStorage persistence ────────────────────────────────
const LS_PREFIX = 'claude-monitor:';

function savePrefs() {
    localStorage.setItem(LS_PREFIX + 'days', currentDays);
    localStorage.setItem(LS_PREFIX + 'source', currentSource);
    localStorage.setItem(LS_PREFIX + 'tab', currentTab);
}

function loadPrefs() {
    const days = localStorage.getItem(LS_PREFIX + 'days');
    if (days !== null) currentDays = parseInt(days);

    const source = localStorage.getItem(LS_PREFIX + 'source');
    if (source !== null) currentSource = source;

    const tab = localStorage.getItem(LS_PREFIX + 'tab');
    if (tab) currentTab = tab;

    // Restore range button
    document.querySelectorAll('.range-btn').forEach(b => {
        b.classList.toggle('active', parseInt(b.dataset.days) === currentDays);
    });

    // Restore source dropdown
    sourceFilter.value = currentSource;

    // Restore tab
    applyTab(currentTab);
}

// ── Source name lookup ───────────────────────────────────────
let _sourceNames = {};  // { "ssh:srv-xxx": "Dev Box", ... }

function sourceLabel(src) {
    if (!src || src === 'local') return 'Local';
    return _sourceNames[src] || src.replace('ssh:', '');
}

// ── Helpers ──────────────────────────────────────────────────
async function fetchJSON(url) {
    const res = await fetch(url);
    return res.json();
}

function apiUrl(path) {
    const sep = path.includes('?') ? '&' : '?';
    return currentSource ? `${path}${sep}source=${encodeURIComponent(currentSource)}` : path;
}

// ── Tabs ─────────────────────────────────────────────────────
function applyTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    ['tabAnalytics', 'tabSessions', 'tabUsage', 'tabSettings'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', id !== 'tab' + tab.charAt(0).toUpperCase() + tab.slice(1));
    });
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        applyTab(btn.dataset.tab);
        savePrefs();
        if (btn.dataset.tab === 'settings') loadSettingsData();
        else loadActiveTab();
    });
});

// ── Overview (always loads — summary cards) ─────────────────
async function loadOverview() {
    const data = await fetchJSON(apiUrl(`/api/overview?days=${currentDays}`));
    document.getElementById('totalSessions').textContent = formatNumber(data.totalSessions);
    document.getElementById('totalMessages').textContent = formatNumber(data.totalMessages);
    document.getElementById('activeSessions').textContent = data.activeSessions;
    document.getElementById('estimatedCost').textContent = formatCost(data.estimatedCostUSD || 0);
    document.getElementById('activeDot').classList.toggle('hidden', data.activeSessions === 0);

    document.getElementById('recordCount').textContent = `${data.totalMessages} records`;
    document.getElementById('lastUpdated').textContent = new Date().toLocaleTimeString();

    // Active sessions data — render if on sessions tab
    if (currentTab === 'sessions') renderActiveSessions(data.activeSessionDetails || []);
    // Store for later if user switches tabs
    _lastActiveSessions = data.activeSessionDetails || [];
}

let _lastActiveSessions = [];

// ── Active Sessions ─────────────────────────────────────────
function renderActiveSessions(sessions) {
    const card = document.getElementById('activeSessionsCard');
    const tbody = document.getElementById('activeSessionsBody');
    if (!sessions.length) { card.classList.add('hidden'); return; }
    card.classList.remove('hidden');
    document.getElementById('activeCount').textContent = sessions.length;
    tbody.innerHTML = '';
    for (const s of sessions) {
        const src = s._source || 'local';
        const srcLabel = s._host || sourceLabel(src);
        const modelName = getModelShortName(s.model || '');
        const modelCls = getModelClass(s.model || '');
        // Look up plan/task info from session history data
        const hist = _allSessions.find(h => h.sessionId === s.sessionId);
        const planSlug = hist?.planSlug || null;
        const taskCount = hist?.taskCount || 0;
        const completedTaskCount = hist?.completedTaskCount || 0;
        const planCell = planSlug
            ? `<button class="plan-badge" data-slug="${planSlug}">Plan</button>`
            : '<span style="color:var(--text-muted)">—</span>';
        const taskCell = taskCount > 0
            ? `<button class="task-chip ${completedTaskCount === taskCount ? 'all-done' : ''}" data-sid="${s.sessionId}">${completedTaskCount}/${taskCount}</button>`
            : '<span style="color:var(--text-muted)">—</span>';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code>${s.pid}</code></td>
            <td>${(s.cwd || '').split('/').pop()}</td>
            <td><span class="model-badge ${modelCls}">${modelName}</span></td>
            <td>${planCell}</td>
            <td>${taskCell}</td>
            <td><span class="source-tag">${srcLabel}</span></td>
            <td>${formatDate(new Date(s.startedAt).toISOString())}</td>
            <td>${formatDuration(Date.now() - s.startedAt)}</td>
            <td>${s.entrypoint}</td>
            <td><button class="kill-btn" data-pid="${s.pid}" data-source="${src}">Terminate</button></td>`;
        tbody.appendChild(tr);
    }
    tbody.querySelectorAll('.plan-badge').forEach(btn => {
        btn.addEventListener('click', () => openPlanModal(_plansCache[btn.dataset.slug]));
    });
    tbody.querySelectorAll('.task-chip').forEach(btn => {
        btn.addEventListener('click', () => openTasksModal(btn.dataset.sid));
    });
    tbody.querySelectorAll('.kill-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const pid = parseInt(e.target.dataset.pid);
            const source = e.target.dataset.source;
            const label = source === 'local' ? 'local' : 'remote';
            if (!confirm(`Terminate ${label} Claude session (PID ${pid})?`)) return;
            e.target.disabled = true; e.target.textContent = 'Killing...';
            const res = await fetch('/api/sessions/kill', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pid, source }) });
            const data = await res.json();
            if (data.success) { e.target.textContent = 'Done'; setTimeout(loadActiveTab, 1500); }
            else { e.target.textContent = data.error || 'Failed'; e.target.disabled = false; }
        });
    });
}

// ── Analytics loaders ───────────────────────────────────────
async function loadActivity() {
    const data = await fetchJSON(apiUrl(`/api/activity/daily?days=${currentDays}`));
    createActivityChart('activityChart', data);
}

async function loadProjects() { createProjectChart('projectChart', await fetchJSON(apiUrl(`/api/projects?days=${currentDays}`))); }
async function loadHeatmap() { renderHeatmap('heatmapContainer', await fetchJSON(apiUrl('/api/activity/daily?days=365'))); }
async function loadTokens() { const d = await fetchJSON(apiUrl(`/api/tokens?days=${currentDays}`)); createModelDoughnut('modelChart', d); renderModelTable('modelTable', d); }
async function loadCostTrend() { createCostChart('costChart', await fetchJSON(apiUrl(`/api/tokens/daily?days=${currentDays}`))); }

// ── Sessions pagination state ───────────────────────────────
let _allSessions = [];
let _sessionsPage = 0;
let _sessionsPageSize = 25;

function _renderSessionsPage() {
    const tbody = document.getElementById('sessionsBody');
    const total = _allSessions.length;
    const totalPages = Math.max(1, Math.ceil(total / _sessionsPageSize));
    _sessionsPage = Math.min(_sessionsPage, totalPages - 1);

    const start = _sessionsPage * _sessionsPageSize;
    const slice = _allSessions.slice(start, start + _sessionsPageSize);

    tbody.innerHTML = '';
    for (const s of slice) {
        const srcLabel = sourceLabel(s.source);
        const planCell = s.planSlug
            ? `<button class="plan-badge" data-slug="${s.planSlug}">Plan</button>`
            : '<span style="color:var(--text-muted)">—</span>';
        const taskCell = s.taskCount > 0
            ? `<button class="task-chip ${s.completedTaskCount === s.taskCount ? 'all-done' : ''}" data-sid="${s.sessionId}">${s.completedTaskCount}/${s.taskCount}</button>`
            : '<span style="color:var(--text-muted)">—</span>';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code>${truncateId(s.sessionId)}</code></td>
            <td>${s.projectName}</td>
            <td><span class="source-tag">${srcLabel}</span></td>
            <td>${formatDate(s.startTime)}</td>
            <td>${formatDuration(s.durationMs)}</td>
            <td>${s.messageCount}</td>
            <td>${planCell}</td>
            <td>${taskCell}</td>
            <td><span class="status-badge ${s.isActive ? 'status-active' : 'status-completed'}">${s.isActive ? 'Active' : 'Done'}</span></td>`;
        tbody.appendChild(tr);
    }
    tbody.querySelectorAll('.plan-badge').forEach(btn => {
        btn.addEventListener('click', () => openPlanModal(_plansCache[btn.dataset.slug]));
    });
    tbody.querySelectorAll('.task-chip').forEach(btn => {
        btn.addEventListener('click', () => openTasksModal(btn.dataset.sid));
    });

    document.getElementById('sessionsTotalLabel').textContent = `${total} sessions`;
    document.getElementById('pageInfo').textContent = `Page ${_sessionsPage + 1} of ${totalPages}`;
    document.getElementById('pageFirst').disabled = _sessionsPage === 0;
    document.getElementById('pagePrev').disabled = _sessionsPage === 0;
    document.getElementById('pageNext').disabled = _sessionsPage >= totalPages - 1;
    document.getElementById('pageLast').disabled = _sessionsPage >= totalPages - 1;
    document.getElementById('sessionsPagination').classList.toggle('hidden', total === 0);
}

// ── Sessions loaders ────────────────────────────────────────
async function loadSessions() {
    const all = await fetchJSON(apiUrl('/api/sessions'));
    _allSessions = all.filter(s => !s.isActive);
    _sessionsPage = 0;
    _renderSessionsPage();
}

document.getElementById('pageFirst').addEventListener('click', () => { _sessionsPage = 0; _renderSessionsPage(); });
document.getElementById('pagePrev').addEventListener('click', () => { _sessionsPage--; _renderSessionsPage(); });
document.getElementById('pageNext').addEventListener('click', () => { _sessionsPage++; _renderSessionsPage(); });
document.getElementById('pageLast').addEventListener('click', () => { _sessionsPage = Math.ceil(_allSessions.length / _sessionsPageSize) - 1; _renderSessionsPage(); });
document.getElementById('pageSizeSelect').addEventListener('change', (e) => {
    _sessionsPageSize = parseInt(e.target.value);
    _sessionsPage = 0;
    _renderSessionsPage();
});

// ── Tab-aware loading ───────────────────────────────────────
async function loadAnalytics() {
    await Promise.all([loadOverview(), loadActivity(), loadProjects(), loadHeatmap(), loadTokens(), loadCostTrend()]);
}

async function loadModelSetting() {
    try {
        const data = await fetchJSON('/api/settings/model');
        const sel = document.getElementById('globalModelSelect');
        if (sel && data.model) sel.value = data.model;
    } catch {}
}

async function loadPlans() {
    const plans = await fetchJSON('/api/plans');
    _plansCache = Object.fromEntries(plans.map(p => [p.slug, p]));
}

let _plansCache = {};

function openPlanModal(plan) {
    if (!plan) return;
    document.getElementById('planModalTitle').textContent = plan.title;
    const date = plan.createdAt ? new Date(plan.createdAt).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
    document.getElementById('planModalMeta').innerHTML =
        `${plan.sessionId ? `Session: <code>${plan.sessionId}</code>` : ''} ${date ? `&nbsp;·&nbsp; ${date}` : ''}`;
    document.getElementById('planModalContent').textContent = plan.content;
    const promptsEl = document.getElementById('planModalPrompts');
    const promptsList = document.getElementById('planModalPromptsList');
    if (plan.allowedPrompts?.length) {
        promptsList.innerHTML = plan.allowedPrompts.map(p =>
            `<li><span class="prompt-tool">${p.tool}</span> ${p.prompt.replace(/</g, '&lt;')}</li>`
        ).join('');
        promptsEl.classList.remove('hidden');
    } else {
        promptsEl.classList.add('hidden');
    }
    document.getElementById('planModal').classList.remove('hidden');
}

async function openTasksModal(sessionId) {
    const tasks = await fetchJSON(`/api/sessions/${sessionId}/tasks`);
    document.getElementById('tasksModalMeta').innerHTML = `Session: <code>${sessionId}</code>`;
    const list = document.getElementById('tasksModalList');
    list.innerHTML = tasks.length
        ? tasks.map(t => {
            const done = t.status === 'completed';
            const deleted = t.status === 'deleted';
            return `<li class="task-item ${done ? 'done' : ''} ${deleted ? 'deleted' : ''}">
                <span class="task-status-icon">${done ? '✓' : deleted ? '✕' : '○'}</span>
                <span class="task-subject">${t.subject.replace(/</g, '&lt;')}</span>
            </li>`;
        }).join('')
        : '<li class="task-item" style="color:var(--text-muted)">No tasks recorded</li>';
    document.getElementById('tasksModal').classList.remove('hidden');
}

document.getElementById('planModalClose').addEventListener('click', () => document.getElementById('planModal').classList.add('hidden'));
document.getElementById('tasksModalClose').addEventListener('click', () => document.getElementById('tasksModal').classList.add('hidden'));
document.getElementById('planModal').addEventListener('click', e => { if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden'); });
document.getElementById('tasksModal').addEventListener('click', e => { if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden'); });

async function loadSessionsTab() {
    await Promise.all([loadOverview(), loadSessions(), loadModelSetting(), loadPlans()]);
    renderActiveSessions(_lastActiveSessions);
}

async function loadActiveTab() {
    if (currentTab === 'analytics') await loadAnalytics();
    else if (currentTab === 'sessions') await loadSessionsTab();
    else if (currentTab === 'usage') await loadAccountUsage();
    // settings tab is excluded from auto-refresh — it reloads only on user actions
}

// ── Account Usage (left panel) ──────────────────────────────
let _accountCache = {};

function barColor(pct) {
    return parseFloat(pct) > 80 ? 'linear-gradient(90deg, var(--accent3), var(--danger))' : 'linear-gradient(90deg, var(--accent2), var(--accent))';
}

function renderStatCard(label, value, bar, sub) {
    return `<div class="usage-stat-card">
        <div class="usc-label">${label}</div>
        <div class="usc-value">${value}</div>
        ${bar || ''}
        ${sub ? `<div class="usc-sub">${sub}</div>` : ''}
    </div>`;
}

function buildAccountHTML(data, linkedSource, userGivenName) {
    const acc = data.account || {};
    const name = userGivenName || acc.display_name || acc.full_name || '';
    const email = acc.email || '';
    const tier = data.tier || '';
    const tierLabel = tier === 'personal' ? 'Personal' : tier;
    const linkedLabel = _sourceLabel(linkedSource);
    let cards = '';

    if (data.rate_5h_pct !== undefined) {
        const p = data.rate_5h_pct;
        const reset = data.rate_5h_reset ? timeAgo(data.rate_5h_reset) : '--';
        cards += renderStatCard('5-Hour Rate', `${p}%`,
            `<div class="usage-bar"><div class="usage-bar-fill" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            p > 0 ? `Resets ${reset}` : 'No usage');
    }
    if (data.rate_7d_pct !== undefined) {
        const p = data.rate_7d_pct;
        let resetLabel = '--';
        if (data.rate_7d_reset) { const d = new Date(data.rate_7d_reset); resetLabel = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }); }
        cards += renderStatCard('7-Day Rate', `${p}%`,
            `<div class="usage-bar"><div class="usage-bar-fill" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            `Resets ${resetLabel}`);
    }
    if (data.monthly_limit_usd) {
        const u = data.monthly_spend_usd || 0, l = data.monthly_limit_usd, p = data.monthly_pct || 0;
        cards += renderStatCard('Extra Usage', `$${u.toFixed(2)} <span class="usc-dim">/ $${l.toFixed(2)}</span>`,
            `<div class="usage-bar"><div class="usage-bar-fill" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            `${p}% — $${(l-u).toFixed(2)} remaining`);
    }
    if (data.prepaid_balance_usd !== undefined)
        cards += renderStatCard('Prepaid Balance', `$${data.prepaid_balance_usd.toFixed(2)}`, null, data.prepaid_currency || 'USD');
    if (data.overage_grant_usd !== undefined) {
        const status = data.overage_granted ? 'Active' : (data.overage_eligible ? 'Eligible' : 'Not eligible');
        const color = data.overage_granted ? 'var(--accent2)' : 'var(--text-muted)';
        cards += renderStatCard('Overage Credit', `$${data.overage_grant_usd.toFixed(2)}`, null, `<span style="color:${color}">${status}</span>`);
    }

    return `<div class="usage-account-header">
        <span class="usage-account-name">${name}</span>
        <span class="usage-account-meta">${email} — <span class="tier-badge">${tierLabel}</span>${linkedLabel ? ` <span class="source-tag">${linkedLabel}</span>` : ''}</span>
    </div>
    <div class="usage-stats-row">${cards}</div>`;
}

function _sourceLabel(src) {
    if (!src) return '';
    return sourceLabel(src);
}

async function loadAccountUsage() {
    const container = document.getElementById('usageAccountsList');
    let accounts;
    try { accounts = await fetchJSON('/api/accounts'); } catch { return; }

    // Filter accounts by current source filter if set
    let filtered = accounts;
    if (currentSource) {
        filtered = accounts.filter(a => !a.linked_source || a.linked_source === currentSource);
    }

    if (!filtered.length || !filtered.some(a => a.hasKey)) {
        container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.82rem;">No accounts for this source. Add one in Settings.</p>';
        return;
    }

    for (const acc of filtered) {
        if (!acc.hasKey) continue;
        let block = container.querySelector(`[data-acc-id="${acc.id}"]`);
        if (!block) {
            block = document.createElement('div');
            block.className = 'usage-account-block';
            block.dataset.accId = acc.id;
            if (_accountCache[acc.id]) {
                block.innerHTML = buildAccountHTML(_accountCache[acc.id], acc.linked_source, acc.name);
            } else {
                block.innerHTML = `<div class="usage-account-header"><span class="usage-account-name">${acc.name || acc.id}</span><span class="usage-account-meta">Loading...</span></div>
                <div class="usage-stats-row">
                    <div class="usage-stat-card skeleton"><div class="skel-line w60"></div><div class="skel-block"></div><div class="skel-line w40"></div></div>
                    <div class="usage-stat-card skeleton"><div class="skel-line w60"></div><div class="skel-block"></div><div class="skel-line w40"></div></div>
                    <div class="usage-stat-card skeleton"><div class="skel-line w60"></div><div class="skel-block"></div><div class="skel-line w40"></div></div>
                </div>`;
            }
            container.appendChild(block);
        }
        const linkedSrc = acc.linked_source;
        const accName = acc.name;
        fetchJSON(`/api/accounts/${acc.id}/usage`).then(data => {
            if (data.error) { block.querySelector('.usage-account-meta').textContent = data.error; return; }
            _accountCache[acc.id] = data;
            block.innerHTML = buildAccountHTML(data, linkedSrc, accName);
        }).catch(() => {});
    }

    // Remove blocks not in the filtered list
    container.querySelectorAll('[data-acc-id]').forEach(block => {
        if (!filtered.find(a => a.id === block.dataset.accId)) block.remove();
    });
}

// ── Background Sync ─────────────────────────────────────────
let syncPollInterval = null;
const STEP_LABELS = { starting: 'Starting...', connecting: 'Connecting via SSH', discovering: 'Locating ~/.claude/', reading_history: 'Reading history', reading_history_done: 'History loaded', reading_projects: 'Scanning project logs', reading_plans: 'Reading plans', reading_plans_done: 'Plans loaded', done: 'Finished' };

function startSyncPolling() {
    if (syncPollInterval) return;
    syncPollInterval = setInterval(pollSyncStatus, 1200);
    pollSyncStatus();
}

function stopSyncPolling() { if (syncPollInterval) { clearInterval(syncPollInterval); syncPollInterval = null; } }

async function pollSyncStatus() {
    const syncBar = document.getElementById('syncBar');
    const syncText = document.getElementById('syncBarText');
    try {
        const jobs = await fetchJSON('/api/sources/sync-status');
        const syncing = Object.entries(jobs).filter(([, j]) => j.status === 'syncing');
        if (syncing.length > 0) {
            syncBar.classList.remove('hidden');
            syncText.textContent = syncing.map(([, j]) => {
                const label = STEP_LABELS[j.step] || j.step || 'Working';
                return `${label}${j.step_detail ? ' — ' + j.step_detail : ''} (${j.elapsed_seconds}s)`;
            }).join(' | ');
        } else {
            syncBar.classList.add('hidden');
            stopSyncPolling();
            if (Object.values(jobs).some(j => j.status === 'done' || j.status === 'error')) {
                refreshSourceDropdown();
                if (currentTab === 'settings') loadServersList();
                else loadActiveTab();
            }
        }
    } catch { syncBar.classList.add('hidden'); stopSyncPolling(); }
}

// ── Range Selector ──────────────────────────────────────────
document.querySelectorAll('.range-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentDays = parseInt(btn.dataset.days);
        savePrefs();
        loadOverview();
        if (currentTab === 'analytics') { loadActivity(); loadProjects(); loadCostTrend(); loadTokens(); }
    });
});

// ── Source Filter ────────────────────────────────────────────
const sourceFilter = document.getElementById('sourceFilter');
sourceFilter.addEventListener('change', () => {
    currentSource = sourceFilter.value;
    savePrefs();
    loadActiveTab();
});

async function refreshSourceDropdown() {
    const servers = await fetchJSON('/api/sources');
    _sourceNames = {};
    while (sourceFilter.options.length > 2) sourceFilter.remove(2);
    for (const srv of servers) {
        const key = `ssh:${srv.id}`;
        const name = srv.name || srv.host;
        _sourceNames[key] = name;
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = name;
        if (srv.synced_at) opt.textContent += ` (${srv.history_count} msgs)`;
        sourceFilter.appendChild(opt);
    }
    if (currentSource) sourceFilter.value = currentSource;
}

// ── Settings Data ───────────────────────────────────────────
async function loadSettingsData() {
    await Promise.all([loadServersList(), loadAccountsList()]);
}

// ── SSH Servers List ────────────────────────────────────────
async function loadServersList() {
    const [servers, syncJobs] = await Promise.all([
        fetchJSON('/api/sources'),
        fetchJSON('/api/sources/sync-status'),
    ]);
    const container = document.getElementById('serversList');
    container.innerHTML = '';

    for (const srv of servers) {
        const job = syncJobs[srv.id];
        let statusHtml = '';
        if (job && job.status === 'syncing') {
            statusHtml = `<span style="color:var(--accent)">${STEP_LABELS[job.step] || 'Syncing...'}</span>`;
        } else if (job && job.status === 'error') {
            statusHtml = `<span style="color:var(--danger)">Error</span>`;
        } else if (srv.synced_at) {
            statusHtml = `<span>Synced ${_timeAgo(srv.synced_at)}</span>`;
        } else {
            statusHtml = `<span style="color:var(--text-muted)">Not synced</span>`;
        }

        const el = document.createElement('div');
        el.className = 'list-item';
        el.innerHTML = `<div class="list-item-info">
                <div class="item-name">${srv.name || srv.host}</div>
                <div class="item-detail">${srv.user}@${srv.host}</div>
                <div class="item-status">${statusHtml}</div>
            </div>
            <div class="list-item-actions">
                <button class="btn btn-sm btn-ghost" data-sync-srv="${srv.id}" data-sync-type="">Sync All</button>
                <button class="btn btn-sm btn-ghost" data-sync-srv="${srv.id}" data-sync-type="history">History</button>
                <button class="btn btn-sm btn-ghost" data-sync-srv="${srv.id}" data-sync-type="sessions">Sessions</button>
                <button class="btn btn-sm btn-ghost" data-sync-srv="${srv.id}" data-sync-type="plans">Plans</button>
                <button class="btn btn-sm btn-ghost" data-test-srv="${srv.id}">Test</button>
                <button class="btn btn-sm btn-danger" data-remove-srv="${srv.id}">Remove</button>
            </div>`;
        container.appendChild(el);
    }

    // Bind actions
    container.querySelectorAll('[data-sync-srv]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.syncSrv;
            const syncType = btn.dataset.syncType;
            btn.disabled = true; btn.textContent = 'Starting...';
            const body = syncType ? { types: [syncType] } : {};
            const res = await fetch(`/api/sources/${id}/sync`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (data.status === 'started' || data.status === 'already_syncing') {
                btn.textContent = 'Syncing...'; startSyncPolling();
            } else {
                btn.textContent = 'Failed';
                setTimeout(() => { btn.textContent = syncType || 'Sync All'; btn.disabled = false; }, 2000);
            }
        });
    });

    container.querySelectorAll('[data-test-srv]').forEach(btn => {
        btn.addEventListener('click', async () => {
            btn.disabled = true; btn.textContent = 'Testing...';
            const res = await fetch(`/api/sources/${btn.dataset.testSrv}/test`, { method: 'POST' });
            const data = await res.json();
            btn.textContent = data.success ? 'OK' : 'Failed';
            btn.style.color = data.success ? 'var(--accent2)' : 'var(--danger)';
            if (!data.success) alert('Connection failed: ' + (data.error || 'Unknown error'));
            setTimeout(() => { btn.textContent = 'Test'; btn.disabled = false; btn.style.color = ''; }, 3000);
        });
    });

    container.querySelectorAll('[data-remove-srv]').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Remove this server?')) return;
            await fetch(`/api/sources/${btn.dataset.removeSrv}`, { method: 'DELETE' });
            loadServersList(); refreshSourceDropdown(); loadActiveTab();
        });
    });
}

function _timeAgo(isoString) {
    const diff = Math.floor((Date.now() - new Date(isoString)) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

// ── Accounts Management ─────────────────────────────────────
async function loadAccountsList() {
    const accounts = await fetchJSON('/api/accounts');
    const container = document.getElementById('accountsList');
    container.innerHTML = '';

    for (const acc of accounts) {
        const el = document.createElement('div');
        el.className = 'list-item';
        const identity = acc.full_name || acc.display_name || acc.name || 'Unnamed';
        const orgParts = [acc.org_name, acc.org_role].filter(Boolean);
        const keyBadge = acc.hasKey
            ? `<span class="acc-chip acc-chip--ok" title="${acc.maskedKey}">Key</span>`
            : '<span class="acc-chip acc-chip--dim">No Key</span>';
        el.innerHTML = `<div class="list-item-info">
                <div class="item-name">${identity}</div>
                ${acc.email ? `<div class="item-detail">${acc.email}${orgParts.length ? ' · ' + orgParts.join(' · ') : ''}</div>` : orgParts.length ? `<div class="item-detail">${orgParts.join(' · ')}</div>` : ''}
                <div class="item-status" style="display:flex;gap:4px;flex-wrap:wrap;align-items:center;margin-top:3px;">
                    ${keyBadge}
                </div>
            </div>
            <div class="list-item-actions">
                <button class="btn btn-sm btn-danger" data-delete-acc="${acc.id}">Remove</button>
            </div>`;
        container.appendChild(el);
    }

    container.querySelectorAll('[data-delete-acc]').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('Remove this account?')) return;
            await fetch(`/api/accounts/${btn.dataset.deleteAcc}`, { method: 'DELETE' });
            delete _accountCache[btn.dataset.deleteAcc];
            const block = document.querySelector(`[data-acc-id="${btn.dataset.deleteAcc}"]`);
            if (block) block.remove();
            loadAccountsList();
        });
    });
}

document.getElementById('toggleAddAccount').addEventListener('click', () => {
    const form = document.getElementById('addAccountForm');
    form.classList.toggle('hidden');
});

document.getElementById('addAccountBtn').addEventListener('click', async () => {
    const name = document.getElementById('newAccountName').value.trim();
    const key = document.getElementById('newAccountKey').value.trim();
    if (!key) return;
    const status = document.getElementById('addAccountStatus');
    status.textContent = 'Connecting...'; status.className = 'key-status';
    try {
        const res = await fetch('/api/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, session_key: key }) });
        const data = await res.json();
        if (data.success) {
            document.getElementById('newAccountName').value = ''; document.getElementById('newAccountKey').value = '';
            document.getElementById('addAccountForm').classList.add('hidden');
            status.textContent = `Added: ${data.account.display_name || data.account.name}`; status.className = 'key-status success';
            loadAccountsList(); loadAccountUsage();
        } else { status.textContent = data.error || 'Failed'; status.className = 'key-status error'; }
    } catch (e) { status.textContent = 'Error: ' + e.message; status.className = 'key-status error'; }
});

document.getElementById('toggleAddServer').addEventListener('click', () => {
    const form = document.getElementById('addServerForm');
    form.classList.toggle('hidden');
});

document.getElementById('addServerBtn').addEventListener('click', async () => {
    const name = document.getElementById('newServerName').value.trim();
    const host = document.getElementById('newServerHost').value.trim();
    const user = document.getElementById('newServerUser').value.trim() || 'root';
    const key_path = document.getElementById('newServerKey').value.trim() || '~/.ssh/id_rsa';
    if (!host) return;
    const status = document.getElementById('addServerStatus');
    status.textContent = 'Adding...'; status.className = 'key-status';
    const res = await fetch('/api/sources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, host, user, key_path }) });
    const data = await res.json();
    if (data.success) {
        ['newServerName', 'newServerHost', 'newServerUser', 'newServerKey'].forEach(id => document.getElementById(id).value = '');
        status.textContent = 'Server added'; status.className = 'key-status success';
        document.getElementById('addServerForm').classList.add('hidden');
        loadServersList(); refreshSourceDropdown();
    } else { status.textContent = data.error || 'Failed'; status.className = 'key-status error'; }
});

document.getElementById('refreshUsageBtn').addEventListener('click', loadAccountUsage);

document.getElementById('globalModelSelect').addEventListener('change', async (e) => {
    const model = e.target.value;
    const status = document.getElementById('modelSaveStatus');
    status.textContent = 'Saving...';
    status.style.color = 'var(--text-muted)';
    try {
        const res = await fetch('/api/settings/model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model }),
        });
        const data = await res.json();
        if (data.success) {
            status.textContent = 'Saved';
            status.style.color = 'var(--accent2)';
        } else {
            status.textContent = data.error || 'Failed';
            status.style.color = 'var(--danger)';
        }
    } catch {
        status.textContent = 'Error';
        status.style.color = 'var(--danger)';
    }
    setTimeout(() => { status.textContent = ''; }, 2000);
});

// ── Initial load ────────────────────────────────────────────
loadPrefs();                       // Restore saved state
refreshSourceDropdown();           // Populate source dropdown
if (currentTab === 'settings') loadSettingsData(); else loadActiveTab();
