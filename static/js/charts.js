const COLORS = {
    accent: '#58a6ff',
    accent2: '#3fb950',
    accent3: '#d29922',
    accent4: '#f78166',
    accent5: '#bc8cff',
    muted: '#8b949e',
    grid: '#21262d',
    bg: '#161b22',
};

const MODEL_COLORS = [COLORS.accent, COLORS.accent5, COLORS.accent3, COLORS.accent4, COLORS.accent2];

const CHART_DEFAULTS = {
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
        legend: {
            labels: { color: COLORS.muted, font: { size: 12 } }
        }
    },
    scales: {
        x: {
            ticks: { color: COLORS.muted, font: { size: 11 } },
            grid: { color: COLORS.grid },
        },
        y: {
            ticks: { color: COLORS.muted, font: { size: 11 } },
            grid: { color: COLORS.grid },
        }
    }
};

let charts = {};

function destroyChart(id) {
    if (charts[id]) {
        charts[id].destroy();
        delete charts[id];
    }
}

function createActivityChart(canvasId, data) {
    destroyChart(canvasId);
    const ctx = document.getElementById(canvasId).getContext('2d');
    charts[canvasId] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: data.map(d => formatDateShort(d.date)),
            datasets: [
                {
                    label: 'Messages',
                    data: data.map(d => d.messageCount),
                    backgroundColor: COLORS.accent + '80',
                    borderColor: COLORS.accent,
                    borderWidth: 1,
                    borderRadius: 4,
                    order: 2,
                },
                {
                    label: 'Sessions',
                    data: data.map(d => d.sessionCount),
                    type: 'line',
                    borderColor: COLORS.accent2,
                    backgroundColor: COLORS.accent2 + '20',
                    pointRadius: 3,
                    pointBackgroundColor: COLORS.accent2,
                    tension: 0.3,
                    yAxisID: 'y1',
                    order: 1,
                }
            ]
        },
        options: {
            ...CHART_DEFAULTS,
            scales: {
                x: {
                    ticks: { color: COLORS.muted, font: { size: 10 }, maxRotation: 45 },
                    grid: { color: COLORS.grid },
                },
                y: {
                    position: 'left',
                    ticks: { color: COLORS.accent, font: { size: 11 } },
                    grid: { color: COLORS.grid },
                    title: { display: true, text: 'Messages', color: COLORS.accent }
                },
                y1: {
                    position: 'right',
                    ticks: { color: COLORS.accent2, font: { size: 11 } },
                    grid: { display: false },
                    title: { display: true, text: 'Sessions', color: COLORS.accent2 }
                }
            },
            plugins: {
                legend: { labels: { color: COLORS.muted, usePointStyle: true } },
                tooltip: {
                    backgroundColor: COLORS.bg,
                    borderColor: COLORS.grid,
                    borderWidth: 1,
                    titleColor: '#fff',
                    bodyColor: COLORS.muted,
                }
            }
        }
    });
}

function createProjectChart(canvasId, data) {
    destroyChart(canvasId);
    const top10 = data.slice(0, 10);
    const ctx = document.getElementById(canvasId).getContext('2d');
    charts[canvasId] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: top10.map(d => d.name),
            datasets: [{
                label: 'Messages',
                data: top10.map(d => d.messageCount),
                backgroundColor: top10.map((_, i) =>
                    [COLORS.accent, COLORS.accent5, COLORS.accent3, COLORS.accent4, COLORS.accent2][i % 5] + 'B0'
                ),
                borderRadius: 6,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: COLORS.bg,
                    borderColor: COLORS.grid,
                    borderWidth: 1,
                }
            },
            scales: {
                x: {
                    ticks: { color: COLORS.muted },
                    grid: { color: COLORS.grid },
                },
                y: {
                    ticks: { color: COLORS.muted, font: { size: 11 } },
                    grid: { display: false },
                }
            }
        }
    });
}

function createModelDoughnut(canvasId, data) {
    destroyChart(canvasId);
    if (!data.length) return;
    const ctx = document.getElementById(canvasId).getContext('2d');
    charts[canvasId] = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: data.map(d => d.displayName),
            datasets: [{
                data: data.map(d => d.totalTokens),
                backgroundColor: data.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length]),
                borderColor: COLORS.bg,
                borderWidth: 3,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: '65%',
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: COLORS.muted, padding: 16, usePointStyle: true }
                },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const d = data[ctx.dataIndex];
                            return `${d.displayName}: ${formatTokens(d.totalTokens)} tokens (${formatCost(d.estimatedCostUSD)})`;
                        }
                    }
                }
            }
        }
    });
}

function createCostChart(canvasId, data) {
    destroyChart(canvasId);
    const ctx = document.getElementById(canvasId).getContext('2d');
    charts[canvasId] = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.map(d => formatDateShort(d.date)),
            datasets: [{
                label: 'Est. Cost (USD)',
                data: data.map(d => d.estimatedCostUSD),
                borderColor: COLORS.accent3,
                backgroundColor: COLORS.accent3 + '15',
                fill: true,
                tension: 0.3,
                pointRadius: 3,
                pointBackgroundColor: COLORS.accent3,
            }]
        },
        options: {
            ...CHART_DEFAULTS,
            scales: {
                x: {
                    ticks: { color: COLORS.muted, font: { size: 10 }, maxRotation: 45 },
                    grid: { color: COLORS.grid },
                },
                y: {
                    ticks: {
                        color: COLORS.accent3,
                        callback: v => '$' + v.toFixed(2)
                    },
                    grid: { color: COLORS.grid },
                }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => formatCost(ctx.parsed.y)
                    }
                }
            }
        }
    });
}

function renderHeatmap(containerId, dailyData) {
    // GitHub-style contribution calendar using CSS grid — auto-fits to container width
    const container = document.getElementById(containerId);
    container.innerHTML = '';

    const LEVELS = [
        { min: 0,   max: 0,        bg: '#161b22', border: '#21262d' },
        { min: 1,   max: 10,       bg: '#0e4429', border: '#1a5c38' },
        { min: 11,  max: 30,       bg: '#006d32', border: '#008c41' },
        { min: 31,  max: 80,       bg: '#26a641', border: '#2fbd4f' },
        { min: 81,  max: 200,      bg: '#39d353', border: '#4ae565' },
        { min: 201, max: Infinity,  bg: '#73e66d', border: '#8ff085' },
    ];

    function cellColor(count) {
        for (const l of LEVELS) if (count >= l.min && count <= l.max) return l.bg;
        return LEVELS[LEVELS.length - 1].bg;
    }

    // Build date→count map
    const countMap = {};
    for (const d of dailyData) countMap[d.date] = d.messageCount;

    // Last 52 weeks ending today
    const today = new Date();
    const endDate = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const startDate = new Date(endDate);
    startDate.setDate(startDate.getDate() - (52 * 7) - endDate.getDay());

    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    // Build weeks array
    const weeks = [];
    const cursor = new Date(startDate);
    let currentWeek = [];
    while (cursor <= endDate) {
        const key = cursor.toISOString().slice(0, 10);
        currentWeek.push({ date: key, count: countMap[key] || 0, day: cursor.getDay() });
        if (cursor.getDay() === 6 || cursor.getTime() === endDate.getTime()) {
            weeks.push(currentWeek);
            currentWeek = [];
        }
        cursor.setDate(cursor.getDate() + 1);
    }
    if (currentWeek.length) weeks.push(currentWeek);

    const numWeeks = weeks.length;

    // CSS Grid: columns = day-labels + N weeks, rows = month-labels + 7 days
    const grid = document.createElement('div');
    grid.className = 'cal-grid';
    grid.style.gridTemplateColumns = `24px repeat(${numWeeks}, 1fr)`;
    grid.style.gridTemplateRows = `16px repeat(7, 1fr)`;

    // Month labels (row 1)
    grid.appendChild(Object.assign(document.createElement('div'), { className: 'cal-spacer' }));
    let lastMonth = -1;
    for (let w = 0; w < numWeeks; w++) {
        const span = document.createElement('div');
        span.className = 'cal-month';
        const firstDay = weeks[w][0];
        if (firstDay) {
            const m = new Date(firstDay.date + 'T00:00:00').getMonth();
            if (m !== lastMonth) {
                span.textContent = new Date(firstDay.date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short' });
                lastMonth = m;
            }
        }
        grid.appendChild(span);
    }

    // Day rows (rows 2-8)
    for (let dayIdx = 0; dayIdx < 7; dayIdx++) {
        // Day label
        const label = document.createElement('div');
        label.className = 'cal-day-label';
        label.textContent = dayIdx % 2 === 1 ? DAYS[dayIdx] : '';
        grid.appendChild(label);

        // Week cells
        for (let w = 0; w < numWeeks; w++) {
            const cell = document.createElement('div');
            cell.className = 'cal-cell';
            const entry = weeks[w].find(e => e.day === dayIdx);
            if (entry) {
                cell.style.background = cellColor(entry.count);
                cell.setAttribute('data-count', entry.count);
                const d = new Date(entry.date + 'T00:00:00');
                const dateLabel = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
                cell.title = `${dateLabel}: ${entry.count} messages`;
                // Tooltip
                const tooltip = document.createElement('span');
                tooltip.className = 'cal-tooltip';
                tooltip.textContent = `${dateLabel} — ${entry.count} msgs`;
                cell.appendChild(tooltip);
            } else {
                cell.style.background = 'transparent';
            }
            grid.appendChild(cell);
        }
    }
    container.appendChild(grid);

    // Legend
    const legend = document.createElement('div');
    legend.className = 'cal-legend';
    legend.innerHTML = '<span>Less</span>' +
        LEVELS.map(l => `<div class="cal-legend-cell" style="background:${l.bg}" title="${l.min === 0 ? '0' : l.min + (l.max === Infinity ? '+' : '-' + l.max)} msgs/day"></div>`).join('') +
        '<span>More</span>';
    container.appendChild(legend);
}

function renderModelTable(containerId, data) {
    const container = document.getElementById(containerId);
    if (!data.length) {
        container.innerHTML = '<p style="color: var(--text-muted)">No token data available</p>';
        return;
    }
    let html = `<table>
        <thead><tr>
            <th>Model</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Cost</th>
        </tr></thead><tbody>`;
    for (const d of data) {
        html += `<tr>
            <td>${d.displayName}</td>
            <td>${formatTokens(d.inputTokens)}</td>
            <td>${formatTokens(d.outputTokens)}</td>
            <td>${formatTokens(d.cacheReadTokens)}</td>
            <td>${formatCost(d.estimatedCostUSD)}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
}

function resizeAllCharts() {
    Object.values(charts).forEach(c => { try { c.resize(); } catch {} });
}
