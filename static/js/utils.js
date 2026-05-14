// ── Numeric formatting ─────────────────────────────────────
// Two flavors per type: compact (cards/tooltips/badges) and full (tables).
//   formatNumber(1234567)  → "1.23M"      formatNumberFull(1234567)  → "1,234,567"
//   formatCost(1234.56)    → "$1.23K"     formatCostFull(1234.56)    → "$1,234.56"
//   formatCost(0.0042)     → "$0.0042"    formatCostFull(0.0042)     → "$0.0042"

// toFixed but trim trailing zeros: (1.20, 2) → "1.2", (1.00, 2) → "1".
function _trim(n, decimals) {
    return parseFloat(n.toFixed(decimals)).toString();
}

function formatNumber(n) {
    if (n == null || Number.isNaN(n)) return '—';
    const abs = Math.abs(n);
    const sign = n < 0 ? '-' : '';
    if (abs >= 1e9) return sign + _trim(abs / 1e9, 2) + 'B';
    if (abs >= 1e6) return sign + _trim(abs / 1e6, 2) + 'M';
    if (abs >= 1e3) return sign + _trim(abs / 1e3, 1) + 'K';
    if (Number.isInteger(n)) return n.toLocaleString('en-US');
    return _trim(n, 2);
}

function formatNumberFull(n) {
    if (n == null || Number.isNaN(n)) return '—';
    return Number.isInteger(n)
        ? n.toLocaleString('en-US')
        : n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

function formatTokens(n) {
    return formatNumber(n);
}

function formatTokensFull(n) {
    return formatNumberFull(n);
}

function formatCost(n) {
    if (n == null || Number.isNaN(n)) return '—';
    const abs = Math.abs(n);
    const sign = n < 0 ? '-' : '';
    if (abs === 0) return '$0';
    if (abs >= 1e6) return sign + '$' + _trim(abs / 1e6, 2) + 'M';
    if (abs >= 1e4) return sign + '$' + _trim(abs / 1e3, 1) + 'K';
    if (abs >= 1) {
        return sign + '$' + abs.toLocaleString('en-US', {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    }
    if (abs >= 0.01) return sign + '$' + abs.toFixed(2);
    // Sub-cent: 4 sig digits, trim trailing zeros so 0.005 → "$0.005" not "$0.0050".
    return sign + '$' + _trim(abs, 4);
}

function formatCostFull(n) {
    if (n == null || Number.isNaN(n)) return '—';
    const abs = Math.abs(n);
    const sign = n < 0 ? '-' : '';
    if (abs === 0) return '$0.00';
    if (abs >= 0.01) {
        return sign + '$' + abs.toLocaleString('en-US', {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    }
    return sign + '$' + _trim(abs, 4);
}

function formatDuration(ms) {
    if (ms < 1000) return '< 1s';
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `${days}d ${hours % 24}h`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
}

function timeAgo(isoString) {
    if (!isoString) return '--';
    const diff = Date.now() - new Date(isoString).getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'just now';
}

function truncateId(id) {
    return id ? id.substring(0, 8) : '--';
}

function formatDate(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatDateShort(dateStr) {
    if (!dateStr) return '--';
    const d = new Date(dateStr + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function getModelShortName(model) {
    if (!model) return '--';
    const m = model.match(/^claude-(opus|sonnet|haiku)-(\d+)-(\d+)/);
    if (m) return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}`;
    return model;
}

function getModelClass(model) {
    if (!model) return '';
    if (model.includes('opus')) return 'opus';
    if (model.includes('sonnet')) return 'sonnet';
    if (model.includes('haiku')) return 'haiku';
    return '';
}
