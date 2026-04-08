function formatNumber(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toString();
}

function formatTokens(n) {
    return formatNumber(n);
}

function formatCost(n) {
    if (n >= 1) return '$' + n.toFixed(2);
    if (n >= 0.01) return '$' + n.toFixed(2);
    return '$' + n.toFixed(4);
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
    if (model.includes('opus') && model.includes('4-6')) return 'Opus 4.6';
    if (model.includes('opus') && model.includes('4-5')) return 'Opus 4.5';
    if (model.includes('sonnet') && model.includes('4-6')) return 'Sonnet 4.6';
    if (model.includes('sonnet') && model.includes('4-5')) return 'Sonnet 4.5';
    if (model.includes('haiku') && model.includes('4-5')) return 'Haiku 4.5';
    if (model.includes('haiku')) return 'Haiku';
    return model;
}

function getModelClass(model) {
    if (!model) return '';
    if (model.includes('opus')) return 'opus';
    if (model.includes('sonnet')) return 'sonnet';
    if (model.includes('haiku')) return 'haiku';
    return '';
}
