const container = document.getElementById('incidents-container');
const detailPanel = document.getElementById('detail-view');
const actionHistory = document.getElementById('action-history');
const closeBtn = document.getElementById('close-detail');
const refreshBtn = document.getElementById('refresh-btn');
const histogramContainer = document.getElementById('histogram-container');
const bucketSelect = document.getElementById('bucket-select');

const clearBtn = document.getElementById('clear-btn');

async function fetchIncidents() {
    try {
        const response = await fetch(`/api/incidents?t=${Date.now()}`);
        const data = await response.json();
        renderIncidents(data);
    } catch (error) {
        console.error('Failed to fetch incidents:', error);
        container.innerHTML = '<div class="error">Failed to connect to cluster API</div>';
    }
}

async function clearIncidents() {
    if (!confirm('Are you sure you want to clear ALL incident history? This cannot be undone.')) return;
    try {
        await fetch('/api/incidents', { method: 'DELETE' });
        fetchIncidents();
        fetchStats();
    } catch (error) {
        console.error('Failed to clear incidents:', error);
    }
}

async function fetchStats() {
    try {
        const bucket = bucketSelect.value;
        const response = await fetch(`/api/stats/frequency?minutes=${bucket}&t=${Date.now()}`);
        const data = await response.json();
        renderHistogram(data);
    } catch (error) {
        console.error('Failed to fetch stats:', error);
    }
}

function renderHistogram(data) {
    if (!data.length) return;
    const max = Math.max(...data.map(d => d.count));
    histogramContainer.innerHTML = '';
    
    // Show last 24 buckets
    data.reverse().forEach(d => {
        const bar = document.createElement('div');
        bar.className = 'hist-bar';
        const height = (d.count / max) * 100;
        bar.style.height = `${Math.max(height, 5)}%`;
        bar.setAttribute('data-count', d.count);
        bar.title = `${d.bucket}: ${d.count} incidents`;
        histogramContainer.appendChild(bar);
    });
}

function renderIncidents(incidents) {
    container.innerHTML = '';
    if (incidents.length === 0) {
        container.innerHTML = '<div class="empty-state">No incidents found. Cluster is healthy! 🦅✅</div>';
        return;
    }
    incidents.forEach(inc => {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
            <div class="card-header">
                <span class="status-pill ${inc.status}">${inc.status}</span>
                <span class="source-tag">${inc.source.toUpperCase()}</span>
            </div>
            <div class="card-body">
                <h3>${inc.reason}</h3>
                <p class="meta">${inc.object_kind}/${inc.object_name}</p>
            </div>
            <div class="card-footer">
                <span class="token-count">🎟️ ${inc.total_tokens.toLocaleString()} tokens</span>
                <span class="meta">${new Date(inc.timestamp).toLocaleTimeString()}</span>
            </div>
        `;
        card.onclick = () => showDetail(inc.incident_id);
        container.appendChild(card);
    });
}

async function showDetail(incidentId) {
    detailPanel.classList.remove('hidden');
    actionHistory.innerHTML = '<div class="loading">Fetching audit trail...</div>';
    
    try {
        const response = await fetch(`/api/incidents/${incidentId}/actions`);
        const actions = await response.json();
        renderActions(actions);
    } catch (error) {
        actionHistory.innerHTML = '<div class="error">Audit trail unavailable</div>';
    }
}

function renderActions(actions) {
    if (actions.length === 0) {
        actionHistory.innerHTML = '<div class="meta">No autonomous actions recorded yet.</div>';
        return;
    }
    
    actionHistory.innerHTML = '';
    actions.forEach(action => {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.innerHTML = `
            <div class="timeline-time">${new Date(action.timestamp).toLocaleTimeString()}</div>
            <div class="timeline-tool">Called <code>${action.tool}</code></div>
            <div class="timeline-reason">${action.reasoning}</div>
            <pre>${JSON.stringify(JSON.parse(action.args), null, 2)}</pre>
            <div class="token-detail">
                <span>In: ${action.input_tokens}</span>
                <span>Out: ${action.output_tokens}</span>
            </div>
            ${action.result ? `<div class="meta">Result: ${action.result}</div>` : ''}
        `;
        actionHistory.appendChild(item);
    });
}

closeBtn.onclick = () => detailPanel.classList.add('hidden');
refreshBtn.onclick = () => { fetchIncidents(); fetchStats(); };
clearBtn.onclick = clearIncidents;
bucketSelect.onchange = fetchStats;

// Initial load
fetchIncidents();
fetchStats();
// Polling
setInterval(() => { fetchIncidents(); fetchStats(); }, 10000);
