/**
 * Algo Strategies panel module.
 * Fetches classical strategy signals from /api/monitor/algo_signals
 * and renders them in the Algo Strategies tab.
 *
 * Also manages the Fast Trading Mode toggle (POST /api/settings/fast-trading).
 */

export async function initAlgoPanel() {
    await updateAlgoData();
    // Listen for real-time WebSocket pushes for algo signals
    document.addEventListener('algo-signals-update', (e) => {
        updateAlgoData(e.detail);
    });
    // Listen for Fast Trading Mode state changes from WebSocket
    document.addEventListener('fast-trading-update', (e) => {
        _applyFastTradingState(e.detail.enabled);
    });
    // Listen for safety-guard state updates
    document.addEventListener('fast-guard-update', (e) => {
        _renderFastGuard(e.detail);
    });
    // Fetch initial Fast Trading Mode state
    _initFastTradingToggle();
    _initFastBannerToggle();
    // Fetch initial guard state
    _fetchFastGuard();
}

function _initFastBannerToggle() {
    const btn = document.getElementById('fast-banner-toggle');
    const clearBtn = document.getElementById('fast-banner-clear');
    const panel = document.getElementById('fast-banner-inspector');
    if (!btn || !panel) return;

    btn.addEventListener('click', () => {
        const isOpen = panel.classList.toggle('is-open');
        panel.style.display = isOpen ? 'block' : 'none';
        btn.setAttribute('aria-expanded', String(isOpen));
        btn.textContent = isOpen ? 'Hide Live Logic' : 'Inspect Live Logic';
        if (clearBtn) clearBtn.style.display = isOpen ? 'inline-flex' : 'none';
    });

    if (clearBtn) {
        clearBtn.addEventListener('click', _onClearInspectorHistory);
    }
}

async function _onClearInspectorHistory() {
    const btn = document.getElementById('fast-banner-clear');
    if (btn) { btn.disabled = true; btn.textContent = 'Clearing…'; }
    try {
        const resp = await fetch('/api/settings/fast-guard/clear-history', { method: 'POST' });
        if (resp.ok) {
            const data = await resp.json();
            if (data && data.snapshot) _renderFastGuard(data.snapshot);
        } else {
            console.warn('[AlgoPanel] clear-history failed:', resp.status);
        }
    } catch (e) {
        console.error('[AlgoPanel] clear-history error:', e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '&#x1F5D1; Clear';
        }
    }
}

async function _fetchFastGuard() {
    try {
        const resp = await fetch('/api/monitor/fast_guard');
        if (resp.ok) {
            const data = await resp.json();
            _renderFastGuard(data);
        }
    } catch (e) {
        console.warn('[AlgoPanel] fast_guard fetch failed:', e);
    }
}

function _renderFastGuard(g) {
    const el = document.getElementById('fast-guard-content');
    if (!el) return;
    _renderFastBannerDebug(g);
    if (!g) {
        el.innerHTML = '<div class="empty-state"><p>No guard data.</p></div>';
        return;
    }

    const cfg = g.config || {};
    const pnl = typeof g.daily_pnl_pct === 'number' ? g.daily_pnl_pct : 0;
    const streak = g.consecutive_losses || 0;
    const blocked = g.blocked_reason;

    const pnlClass = pnl < -1 ? 'guard-bad' : pnl > 0 ? 'guard-good' : 'guard-neutral';
    const pnlLimit = cfg.daily_loss_pct_limit != null ? cfg.daily_loss_pct_limit : -3;
    const streakLimit = cfg.consecutive_loss_threshold != null ? cfg.consecutive_loss_threshold : 3;
    const streakClass = streak >= streakLimit ? 'guard-bad' : streak > 0 ? 'guard-warn' : 'guard-neutral';

    let cooldownHtml = '';
    if (g.cooldown_until_utc) {
        const until = new Date(g.cooldown_until_utc);
        cooldownHtml = `<div class="guard-row guard-bad">
            <span class="guard-label">Cooldown until</span>
            <span class="guard-value">${until.toLocaleTimeString()}</span>
            <button id="fast-guard-reset-btn" class="btn-small btn-warn"
                    type="button" title="Manually clear the consecutive-loss cooldown">
                Reset Cooldown
            </button>
        </div>`;
    }

    const blockedHtml = blocked
        ? `<div class="guard-blocked">🛑 ${_esc(blocked)}</div>`
        : `<div class="guard-ok">✅ All guards passed — fast trading enabled</div>`;

    let lastTradeHtml = '--';
    if (g.last_trade_utc) {
        const dt = new Date(g.last_trade_utc);
        const mins = Math.floor((Date.now() - dt.getTime()) / 60000);
        lastTradeHtml = `${dt.toLocaleTimeString()} (${mins}m ago)`;
    }

    // Last consensus snapshot (for diagnostics — why did Fast not trade?)
    let consensusHtml = '';
    if (g.last_consensus) {
        let agoStr = '';
        if (g.last_consensus_at_utc) {
            const dt = new Date(g.last_consensus_at_utc);
            const secs = Math.floor((Date.now() - dt.getTime()) / 1000);
            agoStr = secs < 120 ? `${secs}s ago` : `${Math.floor(secs / 60)}m ago`;
        }
        consensusHtml = `<div class="guard-row guard-neutral guard-consensus">
            <span class="guard-label">Last consensus</span>
            <span class="guard-value" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;">
                ${_esc(g.last_consensus)}
            </span>
            <span class="guard-limit">${_esc(agoStr)}</span>
        </div>`;
    }

    // Manual reset info line (shows when a reset has been applied)
    let resetInfoHtml = '';
    if (g.cooldown_reset_at_utc && !g.cooldown_until_utc) {
        const dt = new Date(g.cooldown_reset_at_utc);
        resetInfoHtml = `<div class="guard-row guard-good">
            <span class="guard-label">Cooldown manually reset</span>
            <span class="guard-value">${dt.toLocaleTimeString()}</span>
        </div>`;
    }

    const decisionsHtml = _renderFastDecisionItems(g.recent_decisions || []);

    el.innerHTML = `
        ${blockedHtml}
        <div class="guard-grid">
            <div class="guard-row ${pnlClass}">
                <span class="guard-label">Daily PnL</span>
                <span class="guard-value">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%</span>
                <span class="guard-limit">limit ${pnlLimit}%</span>
            </div>
            <div class="guard-row ${streakClass}">
                <span class="guard-label">Loss streak</span>
                <span class="guard-value">${streak}</span>
                <span class="guard-limit">threshold ${streakLimit}</span>
            </div>
            <div class="guard-row guard-neutral">
                <span class="guard-label">Last trade</span>
                <span class="guard-value">${_esc(lastTradeHtml)}</span>
                <span class="guard-limit">min ${Math.round((cfg.min_interval_seconds || 900) / 60)}m</span>
            </div>
            ${cooldownHtml}
            ${resetInfoHtml}
            ${consensusHtml}
        </div>
        <div class="fast-decision-stream">
            <div class="fast-decision-stream-header">Recent fast decisions</div>
            <div class="fast-decision-stream-list">${decisionsHtml}</div>
        </div>
    `;

    // Wire up the reset button (delegated each render)
    const resetBtn = document.getElementById('fast-guard-reset-btn');
    if (resetBtn) {
        resetBtn.addEventListener('click', _onResetCooldown);
    }
}

function _renderFastBannerDebug(g) {
    const summaryEl = document.getElementById('fast-banner-inspector-summary');
    const logEl = document.getElementById('fast-banner-inspector-log');
    if (!summaryEl || !logEl) return;
    if (!g) {
        summaryEl.textContent = 'Waiting for fast cycle…';
        logEl.innerHTML = '<div class="empty-state"><p>No fast diagnostics yet.</p></div>';
        return;
    }
    summaryEl.textContent = g.last_consensus || g.blocked_reason || 'No fast consensus yet.';
    logEl.innerHTML = _renderFastDecisionItems(g.recent_decisions || []);
}

const _FAST_DECISION_PAGE_SIZE = 5;

// Exposed globally so the inline onclick works across module scope
window._showAllFastDecisions = function(btn) {
    const list = btn.closest('.fast-banner-inspector-log, .fast-decision-stream-list');
    if (!list) return;
    const hiddenItems = list.querySelectorAll('.fast-decision-item.hidden-entry');
    hiddenItems.forEach(el => el.classList.remove('hidden-entry'));
    btn.remove();
};

function _renderFastDecisionItems(decisions) {
    if (!Array.isArray(decisions) || !decisions.length) {
        return '<div class="empty-state"><p>No fast diagnostics yet.</p></div>';
    }
    const hiddenCount = Math.max(0, decisions.length - _FAST_DECISION_PAGE_SIZE);
    const showMoreBtn = hiddenCount > 0
        ? `<button class="fast-decision-showmore" onclick="window._showAllFastDecisions && window._showAllFastDecisions(this)">Show ${hiddenCount} more…</button>`
        : '';
    return decisions.map((entry, idx) => {
        const outcome = _esc(entry.outcome || 'UNKNOWN');
        const signal = _esc(entry.signal || '--');
        const confidence = _esc(entry.confidence || '--');
        const when = _formatAge(entry.timestamp_utc);
        const regimeBits = [entry.market_regime, entry.volatility_regime, entry.adx != null ? `ADX ${entry.adx}` : null]
            .filter(Boolean)
            .map(v => _esc(v))
            .join(' · ');
        const strategies = Array.isArray(entry.signals) ? entry.signals.map((s) => `
            <div class="fast-decision-strategy-row">
                <span class="fast-decision-strategy-name">${_esc(s.strategy_name || '?')}</span>
                <span class="fast-decision-strategy-signal signal-badge ${_signalBadgeClass(s.signal)}">${_esc(s.signal || '--')}</span>
                <span class="fast-decision-strategy-reason">${_esc(s.explanation || '')}</span>
            </div>
        `).join('') : '';
        const hiddenClass = idx >= _FAST_DECISION_PAGE_SIZE ? ' hidden-entry' : '';
        return `
            <div class="fast-decision-item${hiddenClass}">
                <div class="fast-decision-topline">
                    <span class="fast-decision-outcome">${outcome}</span>
                    <span class="fast-decision-main">${signal} · ${confidence}</span>
                    <span class="fast-decision-age">${when}</span>
                </div>
                <div class="fast-decision-reason">${_esc(entry.reasoning || entry.detail || '')}</div>
                ${regimeBits ? `<div class="fast-decision-meta">${regimeBits}</div>` : ''}
                ${entry.detail ? `<div class="fast-decision-detail">${_esc(entry.detail)}</div>` : ''}
                <div class="fast-decision-strategies">${strategies}</div>
            </div>
        `;
    }).join('') + showMoreBtn;
}

function _formatAge(ts) {
    if (!ts) return '--';
    const dt = new Date(ts);
    const delta = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
    if (delta < 60) return `${delta}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    return dt.toLocaleTimeString();
}

function _signalBadgeClass(signal) {
    if (signal === 'BUY') return 'signal-buy';
    if (signal === 'SELL') return 'signal-sell';
    return 'signal-hold';
}

async function _onResetCooldown(ev) {
    const btn = ev.currentTarget;
    if (!btn) return;
    if (!confirm('Clear the consecutive-loss cooldown and allow Fast Mode to trade immediately?')) {
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Resetting…';
    try {
        const resp = await fetch('/api/settings/fast-guard/reset', { method: 'POST' });
        if (!resp.ok) {
            const txt = await resp.text().catch(() => '');
            console.error('[AlgoPanel] reset-cooldown failed:', resp.status, txt);
            btn.disabled = false;
            btn.textContent = 'Reset Cooldown';
            return;
        }
        const data = await resp.json();
        if (data && data.snapshot) {
            _renderFastGuard(data.snapshot);
        } else {
            _fetchFastGuard();
        }
    } catch (e) {
        console.error('[AlgoPanel] reset-cooldown error:', e);
        btn.disabled = false;
        btn.textContent = 'Reset Cooldown';
    }
}

// ── Fast Trading Mode toggle ──────────────────────────────────────────────────

async function _initFastTradingToggle() {
    const checkbox = document.getElementById('fast-trade-checkbox');
    const label    = document.getElementById('fast-trade-label');
    if (!checkbox) {
        console.warn('[AlgoPanel] fast-trade-checkbox not found in DOM');
        return;
    }
    // Always attach the listener FIRST, even if initial fetch fails
    checkbox.addEventListener('change', _onFastTradingToggle);

    // Also bind a click on the slider/label so the label text is clearly clickable
    try {
        const resp = await fetch('/api/settings/fast-trading', {
            headers: { 'Cache-Control': 'no-cache' },
        });
        if (resp.ok) {
            const data = await resp.json();
            console.log('[AlgoPanel] initial fast-trading state:', data);
            _applyFastTradingState(Boolean(data.enabled));
        } else {
            console.error('[AlgoPanel] GET /api/settings/fast-trading failed:', resp.status);
        }
    } catch (e) {
        console.error('[AlgoPanel] Failed to fetch fast-trading state:', e);
    }
    if (label) {
        label.style.cursor = 'pointer';
    }
}

async function _onFastTradingToggle(e) {
    const checkbox = e.target;
    const desired  = checkbox.checked;
    console.log('[AlgoPanel] toggle change → requesting', desired);
    // Optimistic: leave the checkbox reflecting the user's intent;
    // we'll overwrite with the server-truth after the response.
    try {
        const resp = await fetch('/api/settings/fast-trading', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: desired }),
        });
        if (!resp.ok) {
            const txt = await resp.text().catch(() => '');
            console.error('[AlgoPanel] POST /fast-trading failed:', resp.status, txt);
            // Revert UI on failure
            _applyFastTradingState(!desired);
            return;
        }
        const data = await resp.json();
        console.log('[AlgoPanel] toggle response:', data);
        _applyFastTradingState(Boolean(data.enabled));
    } catch (err) {
        console.error('[AlgoPanel] fast-trading toggle error:', err);
        _applyFastTradingState(!desired);
    }
}

function _applyFastTradingState(enabled) {
    const checkbox = document.getElementById('fast-trade-checkbox');
    const label    = document.getElementById('fast-trade-label');
    const wrapper  = checkbox ? checkbox.closest('label') : null;

    if (checkbox) checkbox.checked = !!enabled;
    if (label)    label.textContent = enabled ? 'Fast Trade ON' : 'Fast Trade OFF';
    if (wrapper)  wrapper.classList.toggle('is-on', !!enabled);

    // Tab-level indicator (⚡ FAST badge next to heading)
    const indicator = document.getElementById('fast-trade-indicator');
    if (indicator) indicator.style.display = enabled ? 'inline-block' : 'none';

    // Global sidebar badge — visible on ALL tabs
    const sidebarBadge = document.getElementById('fast-global-badge');
    if (sidebarBadge) sidebarBadge.style.display = enabled ? 'flex' : 'none';

    // Sticky banner at top of content area — visible on ALL tabs
    const banner = document.getElementById('fast-global-banner');
    if (banner) banner.style.display = enabled ? 'flex' : 'none';
}

export async function updateAlgoData(wsData = null) {
    // Accept either a WebSocket push payload or fetch from API
    let data = wsData;
    if (!data) {
        try {
            const resp = await fetch('/api/monitor/algo_signals');
            if (!resp.ok) {
                console.warn('[AlgoPanel] /api/monitor/algo_signals returned', resp.status);
                // Clear spinner — endpoint exists but errored
                _renderRegime(null);
                _renderSignalsTable([]);
                _renderConsensus([]);
                return;
            }
            data = await resp.json();
        } catch (e) {
            console.error('[AlgoPanel] Fetch failed:', e);
            _renderRegime(null);
            _renderSignalsTable([]);
            _renderConsensus([]);
            return;
        }
    }

    _renderRegime(data.market_condition);
    _renderSignalsTable(data.signals || []);
    _renderConsensus(data.signals || []);

    const tsEl = document.getElementById('algo-last-updated');
    if (tsEl && data.timestamp) {
        const d = new Date(data.timestamp);
        tsEl.textContent = `Updated: ${new Intl.DateTimeFormat(navigator.language, { timeStyle: 'medium' }).format(d)}`;
    }
}

// ── Regime bar ────────────────────────────────────────────────────────────────

function _renderRegime(mc) {
    const el = document.getElementById('algo-regime-content');
    if (!el) return;
    if (!mc) {
        el.innerHTML = '<div class="empty-state"><p>No market condition data.</p></div>';
        return;
    }

    const cond = (mc.market_condition || 'unknown').toUpperCase();
    const vol  = (mc.volatility_regime || 'normal').toUpperCase();
    const adx  = typeof mc.adx === 'number' ? mc.adx.toFixed(1) : '--';
    const conf = typeof mc.confidence === 'number' ? `${(mc.confidence * 100).toFixed(0)}%` : '--';
    const inst = mc.instrument_type || 'CRYPTO';

    const condClass = cond === 'TRENDING' ? 'regime-trending'
                    : cond === 'RANGING'  ? 'regime-ranging'
                    : 'regime-unknown';

    const volClass = vol === 'HIGH'   ? 'vol-high'
                   : vol === 'LOW'    ? 'vol-low'
                   : 'vol-normal';

    el.innerHTML = `
        <div class="algo-regime-row">
            <span class="regime-badge ${condClass}">${cond}</span>
            <span class="algo-regime-detail">Instrument: <strong>${_esc(inst)}</strong></span>
            <span class="algo-regime-detail">ADX: <strong>${_esc(adx)}</strong></span>
            <span class="algo-regime-detail">Confidence: <strong>${_esc(conf)}</strong></span>
            <span class="vol-badge ${volClass}">Volatility: ${_esc(vol)}</span>
        </div>
    `;
}

// ── Signals table ─────────────────────────────────────────────────────────────

function _renderSignalsTable(signals) {
    const el = document.getElementById('algo-signals-table');
    if (!el) return;
    if (!signals.length) {
        el.innerHTML = '<div class="empty-state"><p>No signals yet – run analysis cycle first.</p></div>';
        return;
    }

    const rows = signals.map(s => {
        const sigClass = s.signal === 'BUY'  ? 'signal-buy'
                       : s.signal === 'SELL' ? 'signal-sell'
                       : 'signal-hold';
        const conf = typeof s.confidence === 'number' ? `${(s.confidence * 100).toFixed(0)}%` : '--';
        return `
            <tr>
                <td class="algo-strat-name">${_esc(s.strategy_name)}</td>
                <td><span class="signal-badge ${sigClass}">${_esc(s.signal)}</span></td>
                <td class="algo-conf">${_esc(conf)}</td>
                <td class="algo-explanation">${_esc(s.explanation)}</td>
            </tr>
        `;
    }).join('');

    el.innerHTML = `
        <table class="algo-table">
            <thead>
                <tr>
                    <th>Strategy</th>
                    <th>Signal</th>
                    <th>Confidence</th>
                    <th>Explanation</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

// ── Consensus ─────────────────────────────────────────────────────────────────

function _renderConsensus(signals) {
    const el = document.getElementById('algo-consensus-content');
    if (!el) return;
    if (!signals.length) {
        el.innerHTML = '<div class="empty-state"><p>--</p></div>';
        return;
    }

    const actionable = signals.filter(s => s.signal !== 'HOLD');
    let consensus, cssClass;

    if (!actionable.length) {
        consensus = 'NEUTRAL — all strategies HOLD';
        cssClass  = 'consensus-neutral';
    } else {
        const buyCount  = actionable.filter(s => s.signal === 'BUY').length;
        const sellCount = actionable.filter(s => s.signal === 'SELL').length;
        const total = actionable.length;
        if (buyCount > sellCount) {
            consensus = `BULLISH — ${buyCount}/${total} strategies signal BUY`;
            cssClass  = 'consensus-bullish';
        } else if (sellCount > buyCount) {
            consensus = `BEARISH — ${sellCount}/${total} strategies signal SELL`;
            cssClass  = 'consensus-bearish';
        } else {
            consensus = `MIXED — ${buyCount} BUY / ${sellCount} SELL`;
            cssClass  = 'consensus-mixed';
        }
    }

    el.innerHTML = `<div class="consensus-pill ${cssClass}">${_esc(consensus)}</div>`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
