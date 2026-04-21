/**
 * Manual Trade module — BUY / SELL / CLOSE buttons on the dashboard.
 * Positions opened here are monitored by the AI just like its own trades.
 * Auto-fills SL/TP from the last AI analysis + inline banner.
 */

let isBusy = false;
let lastSuggestion = null;

export function initManualTrade() {
    const buyBtn = document.getElementById('btn-manual-buy');
    const sellBtn = document.getElementById('btn-manual-sell');
    const closeBtn = document.getElementById('btn-manual-close');
    const closeAllBtn = document.getElementById('btn-manual-close-all');
    if (!buyBtn || !sellBtn || !closeBtn) {
        console.warn('[ManualTrade] Buttons not found in DOM — skipping init');
        return;
    }

    buyBtn.addEventListener('click', (e) => {
        e.preventDefault();
        console.log('[ManualTrade] BUY clicked');
        executeTrade('BUY');
    });
    sellBtn.addEventListener('click', (e) => {
        e.preventDefault();
        console.log('[ManualTrade] SELL clicked');
        executeTrade('SELL');
    });
    closeBtn.addEventListener('click', (e) => {
        e.preventDefault();
        console.log('[ManualTrade] CLOSE clicked');
        executeClose();
    });
    if (closeAllBtn) closeAllBtn.addEventListener('click', (e) => {
        e.preventDefault();
        console.log('[ManualTrade] CLOSE ALL clicked');
        executeCloseAll();
    });

    // Refresh position state and AI suggestion on load
    refreshPositionState();
    fetchSuggestion();

    // Listen for WS position updates to sync button state
    document.addEventListener('position-update', () => refreshPositionState());
    // Refresh suggestion after each new analysis
    document.addEventListener('analysis-complete', () => fetchSuggestion());
    console.log('[ManualTrade] Initialized');
}

// ── AI Analysis Banner ───────────────────────────────────────────

/**
 * Fetch AI-recommended SL/TP from last analysis, pre-fill inputs, render banner.
 */
async function fetchSuggestion() {
    try {
        const resp = await fetch('/api/trade/suggestion');
        const data = await resp.json();
        if (!data.has_suggestion) {
            hideBanner();
            return;
        }

        lastSuggestion = data;
        const slInput = document.getElementById('manual-sl');
        const tpInput = document.getElementById('manual-tp');

        // Only pre-fill if inputs are empty (don't overwrite user edits)
        if (slInput && !slInput.value) slInput.value = data.stop_loss;
        if (tpInput && !tpInput.value) tpInput.value = data.take_profit;

        renderBanner(data);
    } catch {
        // Silent — suggestion is optional
    }
}

function renderBanner(d) {
    const banner = document.getElementById('ai-analysis-banner');
    if (!banner) return;

    // Signal badge
    const sigEl = document.getElementById('ai-banner-signal');
    if (sigEl) {
        const cls = d.signal === 'BUY' ? 'signal-buy'
            : d.signal === 'SELL' ? 'signal-sell' : 'signal-hold';
        sigEl.className = `ai-banner-signal ${cls}`;
        sigEl.textContent = d.signal;
    }

    // Confidence
    setText('ai-banner-confidence', d.confidence ? `${d.confidence}%` : '');

    // Rating
    const ratingEl = document.getElementById('ai-banner-rating');
    if (ratingEl && d.rating) {
        ratingEl.textContent = d.rating;
        ratingEl.className = 'ai-banner-rating ' + ratingClass(d.rating);
    }

    // Detail pills
    setText('ai-banner-entry', d.entry_price ? `Entry ${fmt(d.entry_price)}` : '');
    setText('ai-banner-sl', `SL ${fmt(d.stop_loss)}`);
    setText('ai-banner-tp', `TP ${fmt(d.take_profit)}`);
    setText('ai-banner-rr', d.risk_reward_ratio ? `R/R ${d.risk_reward_ratio.toFixed(1)}:1` : '');
    setText('ai-banner-size', d.position_size != null ? `Size ${(d.position_size * 100).toFixed(0)}%` : '');
    setText('ai-banner-setup', d.setup_type || '');

    // Reasoning (truncated)
    const reasonEl = document.getElementById('ai-banner-reasoning');
    if (reasonEl && d.reasoning) {
        const short = d.reasoning.length > 180 ? d.reasoning.slice(0, 180) + '…' : d.reasoning;
        reasonEl.textContent = short;
        reasonEl.title = d.reasoning;
        reasonEl.style.display = 'block';
    } else if (reasonEl) {
        reasonEl.style.display = 'none';
    }

    banner.style.display = 'block';
}

function hideBanner() {
    const banner = document.getElementById('ai-analysis-banner');
    if (banner) banner.style.display = 'none';
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.style.display = text ? 'inline-block' : 'none';
}

function fmt(n) {
    if (n == null) return '—';
    // Use up to 5 significant digits for price display
    return n >= 100 ? n.toFixed(2) : n.toFixed(5);
}

function ratingClass(r) {
    const s = (r || '').toUpperCase();
    if (s.includes('STRONG_BUY') || s.includes('OVERWEIGHT')) return 'rating-strong-buy';
    if (s.includes('BUY')) return 'rating-buy';
    if (s.includes('STRONG_SELL') || s.includes('UNDERWEIGHT')) return 'rating-strong-sell';
    if (s.includes('SELL')) return 'rating-sell';
    return 'rating-neutral';
}

// ── Trade Execution ──────────────────────────────────────────────

async function executeTrade(signal) {
    if (isBusy) return;

    const slInput = document.getElementById('manual-sl');
    const tpInput = document.getElementById('manual-tp');
    const volInput = document.getElementById('manual-volume');
    const stopLoss = parseFloat(slInput?.value);
    const takeProfit = parseFloat(tpInput?.value);
    const volume = volInput?.value ? parseFloat(volInput.value) : null;

    if (!stopLoss || !takeProfit || stopLoss <= 0 || takeProfit <= 0) {
        showFeedback('⚠ Enter valid SL and TP values', 'error');
        shakeElement(signal === 'BUY' ? 'btn-manual-buy' : 'btn-manual-sell');
        return;
    }

    if (volume !== null && volume <= 0) {
        showFeedback('⚠ Volume must be positive or empty (auto)', 'error');
        shakeElement('manual-volume');
        return;
    }

    // Sanity: for BUY, SL < TP; for SELL, SL > TP
    if (signal === 'BUY' && stopLoss >= takeProfit) {
        showFeedback('⚠ BUY: Stop Loss must be below Take Profit', 'error');
        return;
    }
    if (signal === 'SELL' && stopLoss <= takeProfit) {
        showFeedback('⚠ SELL: Stop Loss must be above Take Profit', 'error');
        return;
    }

    const volLabel = volume ? ` | Vol: ${volume}` : ' | Vol: auto';
    if (!confirm(`Confirm MANUAL ${signal}?\nSL: ${stopLoss}\nTP: ${takeProfit}${volLabel}`)) return;

    isBusy = true;
    setBusy(true);

    try {
        const endpoint = signal === 'BUY' ? '/api/trade/buy' : '/api/trade/sell';
        const body = { stop_loss: stopLoss, take_profit: takeProfit };
        if (volume !== null) body.volume = volume;

        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok && data.success) {
            showFeedback(`✓ ${signal} executed @ ${data.price?.toFixed(2) ?? '—'}`, 'success');
            refreshPositionState();
        } else {
            showFeedback(`✗ ${data.error || 'Trade failed'}`, 'error');
        }
    } catch (e) {
        showFeedback('✗ Network error: ' + e.message, 'error');
    } finally {
        isBusy = false;
        setBusy(false);
    }
}

async function executeClose() {
    if (isBusy) return;
    if (!confirm('Confirm MANUAL CLOSE position?')) return;

    isBusy = true;
    setBusy(true);

    try {
        const resp = await fetch('/api/trade/close', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.success) {
            showFeedback('✓ Position closed', 'success');
            refreshPositionState();
        } else {
            showFeedback(`✗ ${data.error || 'Close failed'}`, 'error');
        }
    } catch (e) {
        showFeedback('✗ Network error: ' + e.message, 'error');
    } finally {
        isBusy = false;
        setBusy(false);
    }
}

async function executeCloseAll() {
    if (isBusy) return;
    if (!confirm('⚠ CLOSE ALL open broker positions?\nThis will close every position on the current symbol.')) return;

    isBusy = true;
    setBusy(true);

    try {
        const resp = await fetch('/api/trade/close-all', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.success) {
            const msg = `✓ Closed ${data.closed}/${data.total} positions`;
            showFeedback(data.errors?.length ? msg + ` (${data.errors.length} errors)` : msg, 'success');
            refreshPositionState();
        } else {
            showFeedback(`✗ ${data.error || 'Close all failed'}`, 'error');
        }
    } catch (e) {
        showFeedback('✗ Network error: ' + e.message, 'error');
    } finally {
        isBusy = false;
        setBusy(false);
    }
}

export async function refreshPositionState() {
    try {
        const resp = await fetch('/api/trade/position');
        const data = await resp.json();
        const openGroup = document.getElementById('manual-trade-open');
        const closeGroup = document.getElementById('manual-trade-close');
        const posInfo = document.getElementById('manual-pos-info');

        if (data.has_position) {
            if (openGroup) openGroup.style.display = 'none';
            if (closeGroup) closeGroup.style.display = 'flex';
            if (posInfo) {
                posInfo.textContent = `${data.direction} ${data.symbol} @ ${data.entry_price?.toFixed(2) ?? '—'}`;
            }
        } else {
            if (openGroup) openGroup.style.display = 'flex';
            if (closeGroup) closeGroup.style.display = 'none';
            if (posInfo) posInfo.textContent = '';
        }
    } catch {
        // Silent fail — position state will refresh on next WS update
    }
}

function showFeedback(msg, type) {
    const el = document.getElementById('manual-trade-feedback');
    if (!el) return;
    el.textContent = msg;
    el.className = `manual-feedback manual-feedback-${type}`;
    el.style.display = 'block';
    if (el._hideTimer) clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => { el.style.display = 'none'; }, 6000);
}

function shakeElement(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add('shake');
    setTimeout(() => el.classList.remove('shake'), 500);
}

function setBusy(busy) {
    document.querySelectorAll('.btn-manual-trade').forEach(btn => {
        btn.disabled = busy;
        if (busy) btn.classList.add('btn-loading');
        else btn.classList.remove('btn-loading');
    });
}
