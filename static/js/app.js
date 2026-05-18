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
    ['tabAnalytics', 'tabSessions', 'tabUsage', 'tabSkills', 'tabSettings'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', id !== 'tab' + tab.charAt(0).toUpperCase() + tab.slice(1));
    });
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        applyTab(btn.dataset.tab);
        savePrefs();
        if (btn.dataset.tab === 'settings') loadSettingsData();
        else if (btn.dataset.tab === 'skills') loadSkills();
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

function _populateSelect(sel, items, current, placeholder) {
    if (!sel) return;
    sel.innerHTML = '';
    const defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = placeholder;
    sel.appendChild(defOpt);
    items.forEach(it => {
        const opt = document.createElement('option');
        opt.value = it.id;
        opt.textContent = it.name;
        sel.appendChild(opt);
    });
    // Show current value even if it's not in the known list
    if (current && !items.some(it => it.id === current)) {
        const opt = document.createElement('option');
        opt.value = current;
        opt.textContent = `${current} (unknown)`;
        sel.appendChild(opt);
    }
    sel.value = current || '';
}

async function loadModelSetting() {
    try {
        const data = await fetchJSON('/api/settings/model');
        _populateSelect(
            document.getElementById('globalModelSelect'),
            data.available || [],
            data.model,
            'Default (Claude Code)'
        );
    } catch {}
    try {
        const data = await fetchJSON('/api/settings/effort');
        _populateSelect(
            document.getElementById('globalEffortSelect'),
            data.available || [],
            data.effort,
            'Default (Claude Code)'
        );
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
    else if (currentTab === 'skills') await loadSkills();
    // settings tab is excluded from auto-refresh — it reloads only on user actions
}

// ── Account Usage (left panel) ──────────────────────────────
let _accountCache = {};

function barColor(pct) {
    return parseFloat(pct) > 80 ? 'linear-gradient(90deg, var(--accent3), var(--danger))' : 'linear-gradient(90deg, var(--accent2), var(--accent))';
}

// "Active" bars (below the 80% warning) get a moving shine; near-limit bars
// stay static so the visual attention shifts from healthy → alarming.
function barFillClass(pct) {
    return parseFloat(pct) > 80 ? 'usage-bar-fill' : 'usage-bar-fill bar-active';
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
    const planLabel = data.plan || '';
    const orgName = data.org_name || '';
    const linkedLabel = _sourceLabel(linkedSource);
    let cards = '';

    if (data.rate_5h_pct !== undefined) {
        const p = data.rate_5h_pct;
        let resetLabel5h = '--';
        if (data.rate_5h_reset) {
            const diff = Math.floor((new Date(data.rate_5h_reset) - Date.now()) / 1000);
            if (diff > 0) {
                const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60);
                resetLabel5h = h > 0 ? `in ${h}h ${m}m` : `in ${m}m`;
            } else {
                resetLabel5h = 'just now';
            }
        }
        cards += renderStatCard('5-Hour Rate', `${p}%`,
            `<div class="usage-bar"><div class="${barFillClass(p)}" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            p > 0 ? `Resets ${resetLabel5h}` : 'No usage');
    }
    if (data.rate_7d_pct !== undefined) {
        const p = data.rate_7d_pct;
        let resetLabel = '--';
        if (data.rate_7d_reset) { const d = new Date(data.rate_7d_reset); resetLabel = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }); }
        cards += renderStatCard('7-Day Rate', `${p}%`,
            `<div class="usage-bar"><div class="${barFillClass(p)}" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            `Resets ${resetLabel}`);
    }
    if (data.monthly_limit_usd) {
        const u = data.monthly_spend_usd || 0, l = data.monthly_limit_usd, p = data.monthly_pct || 0;
        cards += renderStatCard('Extra Usage', `$${u.toFixed(2)} <span class="usc-dim">/ $${l.toFixed(2)}</span>`,
            `<div class="usage-bar"><div class="${barFillClass(p)}" style="width:${Math.min(100,p)}%;background:${barColor(p)}"></div></div>`,
            `${p}% — $${(l-u).toFixed(2)} remaining`);
    }
    if (data.prepaid_balance_usd !== undefined)
        cards += renderStatCard('Prepaid Balance', `$${data.prepaid_balance_usd.toFixed(2)}`, null, data.prepaid_currency || 'USD');
    if (data.overage_grant_usd !== undefined) {
        const status = data.overage_granted ? 'Active' : (data.overage_eligible ? 'Eligible' : 'Not eligible');
        const color = data.overage_granted ? 'var(--accent2)' : 'var(--text-muted)';
        cards += renderStatCard('Overage Credit', `$${data.overage_grant_usd.toFixed(2)}`, null, `<span style="color:${color}">${status}</span>`);
    }

    const metaParts = [];
    if (email) metaParts.push(email);
    if (planLabel) metaParts.push(`<span class="tier-badge">${planLabel}</span>`);
    else if (tierLabel) metaParts.push(`<span class="tier-badge">${tierLabel}</span>`);
    if (orgName) metaParts.push(`<span class="usc-dim">${orgName}</span>`);
    if (linkedLabel) metaParts.push(`<span class="source-tag">${linkedLabel}</span>`);

    return `<div class="usage-account-header">
        <span class="usage-account-name">${name}</span>
        <span class="usage-account-meta">${metaParts.join(' — ')}</span>
    </div>
    <div class="usage-stats-row">${cards}</div>`;
}

function buildAccountErrorHTML(name, errorMsg, linkedSource) {
    const linkedLabel = _sourceLabel(linkedSource);
    return `<div class="usage-account-header">
        <span class="usage-account-name">${name || 'Account'}</span>
        <span class="usage-account-meta">${errorMsg}${linkedLabel ? ` <span class="source-tag">${linkedLabel}</span>` : ''}</span>
    </div>`;
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
            if (data.error) {
                block.innerHTML = buildAccountErrorHTML(accName, data.error, linkedSrc);
                return;
            }
            _accountCache[acc.id] = data;
            block.innerHTML = buildAccountHTML(data, linkedSrc, accName);
        }).catch(err => {
            block.innerHTML = buildAccountErrorHTML(accName, (err && err.message) || 'Failed to load usage', linkedSrc);
        });
    }

    // Remove blocks not in the filtered list
    container.querySelectorAll('[data-acc-id]').forEach(block => {
        if (!filtered.find(a => a.id === block.dataset.accId)) block.remove();
    });
}

// ── Background Sync ─────────────────────────────────────────
let syncPollInterval = null;
const STEP_LABELS = { starting: 'Starting...', connecting: 'Connecting via SSH', discovering: 'Locating ~/.claude/', reading_history: 'Reading history', reading_history_done: 'History loaded', reading_projects: 'Scanning project logs', reading_plans: 'Reading plans', reading_plans_done: 'Plans loaded', reading_skills: 'Reading skills', reading_skills_done: 'Skills loaded', reading_skills_failed: 'Skills sync failed', done: 'Finished' };

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
let _syncCategoriesMeta = null;

async function _getSyncCategories() {
    if (_syncCategoriesMeta) return _syncCategoriesMeta;
    _syncCategoriesMeta = await fetchJSON('/api/sources/sync-categories');
    return _syncCategoriesMeta;
}

async function loadServersList() {
    const [servers, syncJobs, catsMeta] = await Promise.all([
        fetchJSON('/api/sources'),
        fetchJSON('/api/sources/sync-status'),
        _getSyncCategories(),
    ]);
    const categories = catsMeta.categories || [];
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

        const enabled = new Set(srv.sync_categories || categories.map(c => c.id));
        const checkboxes = categories.map(c => `
            <label class="sync-cat" title="${_escapeHTML(c.desc)}">
                <input type="checkbox" data-sync-cat="${srv.id}" data-cat-id="${c.id}" ${enabled.has(c.id) ? 'checked' : ''}>
                <span>${_escapeHTML(c.label)}</span>
            </label>`).join('');

        const el = document.createElement('div');
        el.className = 'server-card';
        el.innerHTML = `
            <div class="server-card-row">
                <div class="server-card-info">
                    <div class="item-name">${_escapeHTML(srv.name || srv.host)}</div>
                    <div class="item-detail">${_escapeHTML(srv.user)}@${_escapeHTML(srv.host)}</div>
                    <div class="item-status">${statusHtml}</div>
                </div>
                <div class="server-card-actions">
                    <button class="btn btn-sm btn-primary" data-sync-srv="${srv.id}">Sync</button>
                    <button class="btn btn-sm btn-ghost" data-test-srv="${srv.id}">Test</button>
                    <button class="btn btn-sm btn-danger" data-remove-srv="${srv.id}">Remove</button>
                </div>
            </div>
            <div class="server-card-cats">
                <span class="sync-cat-label">Sync:</span>
                ${checkboxes}
                <span class="sync-cat-status" data-cat-status="${srv.id}"></span>
            </div>`;
        container.appendChild(el);
    }

    // Sync button — uses the server's saved sync_categories.
    container.querySelectorAll('[data-sync-srv]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.syncSrv;
            btn.disabled = true; btn.textContent = 'Starting...';
            const res = await fetch(`/api/sources/${id}/sync`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            const data = await res.json();
            if (data.status === 'started' || data.status === 'already_syncing') {
                btn.textContent = 'Syncing...'; startSyncPolling();
            } else {
                btn.textContent = data.error || 'Failed';
                setTimeout(() => { btn.textContent = 'Sync'; btn.disabled = false; }, 2500);
            }
        });
    });

    // Category checkboxes — debounced auto-save per server.
    const saveTimers = {};
    container.querySelectorAll('[data-sync-cat]').forEach(cb => {
        cb.addEventListener('change', () => {
            const id = cb.dataset.syncCat;
            const statusEl = container.querySelector(`[data-cat-status="${id}"]`);
            statusEl.textContent = 'Saving…';
            statusEl.style.color = 'var(--text-muted)';
            clearTimeout(saveTimers[id]);
            saveTimers[id] = setTimeout(async () => {
                const selected = Array.from(container.querySelectorAll(`[data-sync-cat="${id}"]:checked`))
                    .map(x => x.dataset.catId);
                const res = await fetch(`/api/sources/${id}`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sync_categories: selected }),
                });
                const data = await res.json();
                if (data.success) {
                    statusEl.textContent = 'Saved';
                    statusEl.style.color = 'var(--accent2)';
                } else {
                    statusEl.textContent = data.error || 'Error';
                    statusEl.style.color = 'var(--danger)';
                }
                setTimeout(() => { statusEl.textContent = ''; }, 1500);
            }, 250);
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
        const realName = acc.full_name || acc.display_name || '';
        const title = acc.name || realName || 'Unnamed';
        const subRealName = (acc.name && realName && realName !== acc.name) ? realName : '';
        const orgParts = [acc.org_name, acc.org_role].filter(Boolean);
        const detailParts = [];
        if (subRealName) detailParts.push(subRealName);
        if (acc.email) detailParts.push(acc.email);
        if (orgParts.length) detailParts.push(orgParts.join(' · '));
        const keyBadge = acc.hasKey
            ? `<span class="acc-chip acc-chip--ok" title="${acc.maskedKey}">Key</span>`
            : '<span class="acc-chip acc-chip--dim">No Key</span>';
        el.innerHTML = `<div class="list-item-info">
                <div class="item-name">${title}</div>
                ${detailParts.length ? `<div class="item-detail">${detailParts.join(' · ')}</div>` : ''}
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

// Auto-reload Account Usage every 60s while that tab is active and the page is visible.
setInterval(() => {
    if (currentTab === 'usage' && !document.hidden) loadAccountUsage();
}, 60000);

async function _saveSetting(url, body, statusEl) {
    statusEl.textContent = 'Saving...';
    statusEl.style.color = 'var(--text-muted)';
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            statusEl.textContent = 'Saved';
            statusEl.style.color = 'var(--accent2)';
        } else {
            statusEl.textContent = data.error || 'Failed';
            statusEl.style.color = 'var(--danger)';
        }
    } catch {
        statusEl.textContent = 'Error';
        statusEl.style.color = 'var(--danger)';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 2000);
}

document.getElementById('globalModelSelect').addEventListener('change', (e) => {
    _saveSetting('/api/settings/model', { model: e.target.value }, document.getElementById('modelSaveStatus'));
});

document.getElementById('globalEffortSelect').addEventListener('change', (e) => {
    _saveSetting('/api/settings/effort', { effort: e.target.value }, document.getElementById('effortSaveStatus'));
});

// ── Skills ──────────────────────────────────────────────────
let _allSkills = [];
let _skillsFilter = '';

function _escapeHTML(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _skillSourceBadge(source) {
    if (source === 'user') return '<span class="source-tag" style="background:var(--accent2);color:white">User</span>';
    if (source === 'project') return '<span class="source-tag" style="background:var(--accent3);color:white">Project</span>';
    if (source && source.startsWith('plugin:')) {
        return `<span class="source-tag">Plugin: ${_escapeHTML(source.slice(7))}</span>`;
    }
    return `<span class="source-tag">${_escapeHTML(source)}</span>`;
}

function _skillHostBadge(skill) {
    const src = skill._source || 'local';
    if (src === 'local') return '<span class="source-tag" style="background:rgba(111,168,92,0.15)">Local</span>';
    const label = sourceLabel(src);
    return `<span class="source-tag" style="background:rgba(232,164,92,0.18);color:var(--accent3)">${_escapeHTML(label)}</span>`;
}

// Persisted open/closed state per node, keyed by a stable node id.
const SKILL_TREE_LS = LS_PREFIX + 'skillsTree';
function _loadSkillTreeState() {
    try { return JSON.parse(localStorage.getItem(SKILL_TREE_LS) || '{}'); }
    catch { return {}; }
}
function _saveSkillTreeState(state) {
    try { localStorage.setItem(SKILL_TREE_LS, JSON.stringify(state)); } catch {}
}

function _kindOrder(kind) {
    if (kind === 'user') return 0;
    if (kind === 'project') return 2;
    return 1;  // plugin:* in the middle
}

function _buildSkillTree(items) {
    // host (local|ssh:srv-x) → kind (user|plugin:X|project) → (project path?) → [skills]
    const tree = {};
    for (const s of items) {
        const host = s._source || 'local';
        const kind = s.source || 'unknown';
        if (!tree[host]) tree[host] = {};
        if (!tree[host][kind]) tree[host][kind] = kind === 'project' ? {} : [];
        if (kind === 'project') {
            const proj = s.project || s.scope_root || 'unknown';
            if (!tree[host][kind][proj]) tree[host][kind][proj] = [];
            tree[host][kind][proj].push(s);
        } else {
            tree[host][kind].push(s);
        }
    }
    return tree;
}

function renderSkills() {
    const root = document.getElementById('skillsList');
    const q = _skillsFilter.trim().toLowerCase();
    const items = q
        ? _allSkills.filter(s =>
            (s.name || '').toLowerCase().includes(q) ||
            (s.description || '').toLowerCase().includes(q))
        : _allSkills;

    if (!items.length) {
        root.innerHTML = `<div class="empty-state">${_allSkills.length ? 'No skills match the filter.' : 'No skills found on this machine.'}</div>`;
        return;
    }

    const tree = _buildSkillTree(items);
    const state = _loadSkillTreeState();
    // When filtering, force-open every node so matches are visible.
    const forceOpen = q.length > 0;

    const sortedHosts = Object.keys(tree).sort((a, b) =>
        a === 'local' ? -1 : b === 'local' ? 1 : a.localeCompare(b));

    const renderSkillNode = (s) => {
        const isLocal = (s._source || 'local') === 'local';
        const openBtn = isLocal
            ? `<button class="btn btn-sm btn-ghost" data-skill-open="${_escapeHTML(s.path)}">Open in editor</button>`
            : '';
        const desc = s.description
            ? _escapeHTML(s.description)
            : '<span class="usc-dim">No description</span>';
        return `<div class="skill-row">
            <div class="skill-row-main">
                <div class="skill-row-name">${_escapeHTML(s.name)} ${_skillSourceBadge(s.source)}</div>
                <div class="skill-row-desc">${desc}</div>
                <div class="skill-row-path">${_escapeHTML(s.path)}</div>
            </div>
            <div class="skill-row-actions">
                <button class="btn btn-sm btn-ghost" data-skill-view="${_escapeHTML(s.path)}" data-skill-name="${_escapeHTML(s.name)}" data-skill-source="${_escapeHTML(s._source || 'local')}">View</button>
                ${openBtn}
            </div>
        </div>`;
    };

    const openAttr = (nodeId, defaultOpen = true) => {
        if (forceOpen) return ' open';
        const v = state[nodeId];
        if (v === undefined) return defaultOpen ? ' open' : '';
        return v ? ' open' : '';
    };

    let html = '';
    for (const host of sortedHosts) {
        const kinds = tree[host];
        const hostId = `host|${host}`;
        const hostLabel = host === 'local' ? 'Local' : sourceLabel(host);
        let hostTotal = 0;
        for (const k of Object.keys(kinds)) {
            hostTotal += kinds[k] instanceof Array
                ? kinds[k].length
                : Object.values(kinds[k]).reduce((n, arr) => n + arr.length, 0);
        }
        const hostBadge = host === 'local'
            ? '<span class="source-tag" style="background:rgba(111,168,92,0.15)">Local</span>'
            : `<span class="source-tag" style="background:rgba(232,164,92,0.18);color:var(--accent3)">${_escapeHTML(hostLabel)}</span>`;

        html += `<details class="tree-node tree-host" data-node="${_escapeHTML(hostId)}"${openAttr(hostId, true)}>
            <summary><span class="tree-chevron"></span>${hostBadge}<span class="tree-label">${_escapeHTML(hostLabel)}</span><span class="badge">${hostTotal}</span></summary>
            <div class="tree-children">`;

        const sortedKinds = Object.keys(kinds).sort((a, b) => _kindOrder(a) - _kindOrder(b) || a.localeCompare(b));
        for (const kind of sortedKinds) {
            const kindId = `kind|${host}|${kind}`;
            let kindLabel;
            if (kind === 'user') kindLabel = 'User';
            else if (kind === 'project') kindLabel = 'Project';
            else if (kind.startsWith('plugin:')) kindLabel = `Plugin — ${kind.slice(7)}`;
            else kindLabel = kind;

            if (kind === 'project') {
                const projects = kinds[kind];
                const projTotal = Object.values(projects).reduce((n, arr) => n + arr.length, 0);
                html += `<details class="tree-node tree-kind" data-node="${_escapeHTML(kindId)}"${openAttr(kindId, false)}>
                    <summary><span class="tree-chevron"></span><span class="tree-label">${_escapeHTML(kindLabel)}</span><span class="badge">${projTotal}</span></summary>
                    <div class="tree-children">`;
                const sortedProjs = Object.keys(projects).sort();
                for (const proj of sortedProjs) {
                    const projId = `proj|${host}|${proj}`;
                    const skills = projects[proj].slice().sort((a, b) => a.name.localeCompare(b.name));
                    html += `<details class="tree-node tree-project" data-node="${_escapeHTML(projId)}"${openAttr(projId, false)}>
                        <summary><span class="tree-chevron"></span><span class="tree-label tree-path">${_escapeHTML(proj)}</span><span class="badge">${skills.length}</span></summary>
                        <div class="tree-children">${skills.map(renderSkillNode).join('')}</div>
                    </details>`;
                }
                html += `</div></details>`;
            } else {
                const skills = kinds[kind].slice().sort((a, b) => a.name.localeCompare(b.name));
                html += `<details class="tree-node tree-kind" data-node="${_escapeHTML(kindId)}"${openAttr(kindId, kind === 'user')}>
                    <summary><span class="tree-chevron"></span><span class="tree-label">${_escapeHTML(kindLabel)}</span><span class="badge">${skills.length}</span></summary>
                    <div class="tree-children">${skills.map(renderSkillNode).join('')}</div>
                </details>`;
            }
        }
        html += `</div></details>`;
    }
    root.innerHTML = html;

    // Persist open/closed state per node — but only when NOT in filter mode,
    // since filter mode force-opens everything and would otherwise clobber the
    // user's saved preferences.
    if (!forceOpen) {
        root.querySelectorAll('details[data-node]').forEach(el => {
            el.addEventListener('toggle', () => {
                const s = _loadSkillTreeState();
                s[el.dataset.node] = el.open;
                _saveSkillTreeState(s);
            });
        });
    }
}

function _setAllSkillTreeOpen(open) {
    const root = document.getElementById('skillsList');
    const state = _loadSkillTreeState();
    root.querySelectorAll('details[data-node]').forEach(el => {
        el.open = open;
        state[el.dataset.node] = open;
    });
    _saveSkillTreeState(state);
}

async function loadSkills() {
    const root = document.getElementById('skillsList');
    root.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
        // Honor the global source filter — backend list_skills(source=...) does the right thing.
        const data = await fetchJSON(apiUrl('/api/skills'));
        _allSkills = data.skills || [];
        renderSkills();

        // If we're filtered to an SSH server and got nothing back, offer a probe.
        if (currentSource && currentSource.startsWith('ssh:') && _allSkills.length === 0) {
            const id = currentSource.slice(4);
            root.innerHTML = `<div class="empty-state">
                <p>No skills cached for this server.</p>
                <p style="font-size:0.8rem;">Either the remote machine has no SKILL.md files in the expected locations, or sync hasn't run for this category yet.</p>
                <button class="btn btn-sm btn-primary" id="probeSkillsBtn" style="margin-top:8px;">Diagnose remote</button>
                <div id="probeSkillsBox" style="display:none; margin-top:12px;">
                    <div style="display:flex; gap:8px; align-items:center; margin-bottom:6px;">
                        <button class="btn btn-sm btn-ghost" id="probeCopyBtn">Copy</button>
                        <span id="probeCopyStatus" class="usc-dim" style="font-size:0.75rem;"></span>
                    </div>
                    <textarea id="probeSkillsOut" readonly spellcheck="false"
                        style="width:100%; max-height:55vh; min-height:240px; font-family:ui-monospace,SFMono-Regular,monospace; font-size:0.78rem; padding:10px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; resize:vertical; user-select:text; -webkit-user-select:text;"></textarea>
                </div>
            </div>`;
            document.getElementById('probeSkillsBtn').addEventListener('click', async () => {
                const box = document.getElementById('probeSkillsBox');
                const out = document.getElementById('probeSkillsOut');
                box.style.display = 'block';
                out.value = 'Probing remote...';
                try {
                    const r = await fetch(`/api/sources/${id}/skills/probe`);
                    const d = await r.json();
                    out.value = JSON.stringify(d, null, 2);
                } catch (e) {
                    out.value = `Error: ${e}`;
                }
            });
            document.getElementById('probeCopyBtn').addEventListener('click', async () => {
                const out = document.getElementById('probeSkillsOut');
                const status = document.getElementById('probeCopyStatus');
                try {
                    if (navigator.clipboard && window.isSecureContext) {
                        await navigator.clipboard.writeText(out.value);
                    } else {
                        out.select();
                        document.execCommand('copy');
                        out.setSelectionRange(0, 0);
                    }
                    status.textContent = 'Copied';
                    status.style.color = 'var(--accent2)';
                } catch (e) {
                    status.textContent = 'Copy failed — select text manually';
                    status.style.color = 'var(--danger)';
                }
                setTimeout(() => { status.textContent = ''; }, 2000);
            });
        }
    } catch (e) {
        root.innerHTML = `<div class="empty-state">Failed to load skills: ${_escapeHTML(String(e))}</div>`;
    }
}

async function viewSkill(path, name, source) {
    const titleEl = document.getElementById('skillModalTitle');
    const metaEl = document.getElementById('skillModalMeta');
    const bodyEl = document.getElementById('skillModalContent');
    titleEl.textContent = name || 'Skill';
    metaEl.textContent = path;
    bodyEl.textContent = 'Loading...';
    document.getElementById('skillModal').classList.remove('hidden');
    try {
        const qs = new URLSearchParams({ path });
        if (source && source !== 'local') qs.set('source', source);
        const res = await fetch(`/api/skills/content?${qs.toString()}`);
        const data = await res.json();
        if (data.error) { bodyEl.textContent = `Error: ${data.error}`; return; }
        bodyEl.textContent = data.body || '(empty)';
    } catch (e) {
        bodyEl.textContent = `Error: ${e}`;
    }
}

async function openSkillInEditor(path) {
    try {
        const res = await fetch('/api/skills/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
        const data = await res.json();
        if (data.error) alert(`Could not open: ${data.error}`);
    } catch (e) {
        alert(`Could not open: ${e}`);
    }
}

document.getElementById('skillsList').addEventListener('click', (e) => {
    const viewBtn = e.target.closest('[data-skill-view]');
    if (viewBtn) { viewSkill(viewBtn.dataset.skillView, viewBtn.dataset.skillName, viewBtn.dataset.skillSource); return; }
    const openBtn = e.target.closest('[data-skill-open]');
    if (openBtn) openSkillInEditor(openBtn.dataset.skillOpen);
});

document.getElementById('refreshSkillsBtn').addEventListener('click', loadSkills);
document.getElementById('skillsExpandAllBtn').addEventListener('click', () => _setAllSkillTreeOpen(true));
document.getElementById('skillsCollapseAllBtn').addEventListener('click', () => _setAllSkillTreeOpen(false));
document.getElementById('skillsFilter').addEventListener('input', (e) => {
    _skillsFilter = e.target.value;
    renderSkills();
});
document.getElementById('skillModalClose').addEventListener('click', () => document.getElementById('skillModal').classList.add('hidden'));
document.getElementById('skillModal').addEventListener('click', (e) => { if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden'); });

// ── Initial load ────────────────────────────────────────────
loadPrefs();                       // Restore saved state
refreshSourceDropdown();           // Populate source dropdown
if (currentTab === 'settings') loadSettingsData();
else if (currentTab === 'skills') loadSkills();
else loadActiveTab();
