/**
 * Auto-Trade toggle module — controls live order execution from the dashboard.
 */

export function initAutoTrade() {
    const checkbox = document.getElementById('auto-trade-checkbox');
    const label = document.getElementById('auto-trade-label');
    if (!checkbox || !label) return;

    // Fetch initial state
    fetchAutoTradeState(checkbox, label);

    // Toggle on click
    checkbox.addEventListener('change', async () => {
        try {
            const resp = await fetch('/api/execution/auto-trade', { method: 'POST' });
            const data = await resp.json();
            applyState(checkbox, label, data.enabled);
        } catch (e) {
            console.error('Failed to toggle auto-trade:', e);
            // Revert checkbox on error
            checkbox.checked = !checkbox.checked;
        }
    });

    // Listen for WebSocket updates (server-side toggle)
    document.addEventListener('auto-trade-update', (e) => {
        applyState(checkbox, label, e.detail.enabled);
    });
}

async function fetchAutoTradeState(checkbox, label) {
    try {
        const resp = await fetch('/api/execution/auto-trade');
        const data = await resp.json();
        applyState(checkbox, label, data.enabled);
    } catch (e) {
        console.error('Failed to fetch auto-trade state:', e);
    }
}

function applyState(checkbox, label, enabled) {
    checkbox.checked = enabled;
    label.textContent = enabled ? 'Auto-Trade ON' : 'Auto-Trade OFF';
    label.className = 'toggle-label ' + (enabled ? 'active' : 'inactive');
}
