const container = document.getElementById('incidents-container');
const detailPanel = document.getElementById('detail-view');
const actionHistory = document.getElementById('action-history');
const closeBtn = document.getElementById('close-detail');
const refreshBtn = document.getElementById('refresh-btn');

async function fetchIncidents() {
    try {
        const response = await fetch('/api/incidents');
        const data = await response.json();
        renderIncidents(data);
    } catch (error) {
        console.error('Failed to fetch incidents:', error);
        container.innerHTML = '<div class="error">Failed to connect to cluster API</div>';
    }
}

function renderIncidents(incidents) {
    container.innerHTML = '';
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
                <p class="meta">Namespace: ${inc.namespace}</p>
            </div>
            <div class="card-footer">
                <span class="meta">${new Date(inc.timestamp).toLocaleTimeString()}</span>
            </div>
        `;
        card.onclick = () => showDetail(inc.incident_id);
        container.appendChild(card);
    });

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
            ${action.result ? `<div class="meta">Result: ${action.result}</div>` : ''}
        `;
        actionHistory.appendChild(item);
    });

closeBtn.onclick = () => detailPanel.classList.add('hidden');
refreshBtn.onclick = fetchIncidents;

// Initial load
fetchIncidents();
// Polling
setInterval(fetchIncidents, 10000);
