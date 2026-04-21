/**
 * Symbol picker — categorised dropdown to change the active trading pair.
 *
 * Renders:
 *  - A pill button in the sidebar header showing the current symbol.
 *  - A full-screen modal with the SYMBOL_CATALOG fetched from the backend,
 *    grouped by category (Forex / Métaux / Énergie / Indices / Crypto),
 *    each with volatility + liquidity + hours badges.
 *
 * Calls:
 *  - GET  /api/settings/symbols
 *  - GET  /api/settings/symbol/status
 *  - POST /api/settings/symbol  {symbol, close_position}
 */

const VOL_LABEL = {
    low: 'Vol. faible',
    medium: 'Vol. moyenne',
    high: 'Vol. forte',
    very_high: 'Vol. très forte',
};
const LIQ_LABEL = {
    low: 'Liquidité faible',
    medium: 'Liquidité moyenne',
    high: 'Liquidité forte',
    very_high: 'Liquidité très forte',
};

let catalog = null;
let currentSymbol = null;

function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === 'className') el.className = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v !== null && v !== undefined) el.setAttribute(k, v);
    }
    for (const c of children.flat()) {
        if (c == null || c === false) continue;
        el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
}

async function fetchCatalog() {
    const resp = await fetch('/api/settings/symbols');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function fetchStatus() {
    const resp = await fetch('/api/settings/symbol/status');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function requestSwitch(symbol, closePosition = false) {
    const resp = await fetch('/api/settings/symbol', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, close_position: closePosition }),
    });
    const data = await resp.json().catch(() => ({}));
    return { status: resp.status, data };
}

function updatePillLabel(symbol, entry) {
    const pill = document.getElementById('symbol-pill');
    if (!pill) return;
    const icon = pill.querySelector('.symbol-pill-icon');
    const label = pill.querySelector('.symbol-pill-label');
    if (icon) icon.textContent = (entry && entry.icon) || '⇄';
    if (label) label.textContent = symbol || '—';
}

function buildSymbolCard(entry, { active, onPick }) {
    const card = h('button', {
        type: 'button',
        className: `symbol-card ${active ? 'is-active' : ''} vol-${entry.volatility || 'medium'}`,
        dataset: { symbol: entry.symbol },
        onclick: () => onPick(entry),
        title: entry.description || '',
    },
        h('div', { className: 'symbol-card-top' },
            h('span', { className: 'symbol-card-icon' }, entry.icon || '•'),
            h('span', { className: 'symbol-card-label' }, entry.label || entry.symbol),
            active ? h('span', { className: 'symbol-card-active' }, '✓') : null,
        ),
        h('div', { className: 'symbol-card-sym' }, entry.symbol),
        h('div', { className: 'symbol-card-desc' }, entry.description || ''),
        h('div', { className: 'symbol-card-badges' },
            h('span', { className: `badge vol-badge vol-${entry.volatility}` },
                VOL_LABEL[entry.volatility] || entry.volatility || '—'),
            h('span', { className: `badge liq-badge liq-${entry.liquidity}` },
                LIQ_LABEL[entry.liquidity] || entry.liquidity || '—'),
            h('span', { className: 'badge hours-badge' }, entry.hours || ''),
        ),
    );
    return card;
}

function buildModal() {
    const existing = document.getElementById('symbol-picker-modal');
    if (existing) return existing;

    const modal = h('div', {
        id: 'symbol-picker-modal',
        className: 'symbol-picker-modal',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': 'symbol-picker-title',
    },
        h('div', { className: 'symbol-picker-backdrop', onclick: closeModal }),
        h('div', { className: 'symbol-picker-dialog' },
            h('header', { className: 'symbol-picker-header' },
                h('div', {},
                    h('h2', { id: 'symbol-picker-title' }, 'Changer de marché'),
                    h('p', { className: 'symbol-picker-subtitle' },
                        'Sélectionne le symbole à trader. News, IA et stratégies algo s\'adaptent automatiquement.'),
                ),
                h('button', {
                    type: 'button',
                    className: 'symbol-picker-close',
                    'aria-label': 'Fermer',
                    onclick: closeModal,
                }, '✕'),
            ),
            h('div', { className: 'symbol-picker-search' },
                h('input', {
                    type: 'text',
                    id: 'symbol-picker-search-input',
                    placeholder: 'Rechercher… (EURUSD, BTC, Gold…)',
                    oninput: (e) => filterCards(e.target.value),
                }),
                h('div', { id: 'symbol-picker-current', className: 'symbol-picker-current' }),
            ),
            h('div', { id: 'symbol-picker-body', className: 'symbol-picker-body' }),
            h('footer', { className: 'symbol-picker-footer' },
                h('small', {}, 'Pour lancer plusieurs bots en parallèle : ',
                    h('code', {}, 'python start.py --profile forex'),
                    ' — voir config/profiles/.'),
            ),
        ),
    );
    document.body.appendChild(modal);
    document.addEventListener('keydown', escHandler);
    return modal;
}

function escHandler(e) {
    if (e.key === 'Escape') closeModal();
}

function closeModal() {
    const modal = document.getElementById('symbol-picker-modal');
    if (modal) modal.classList.remove('is-open');
}

function filterCards(query) {
    const q = (query || '').trim().toLowerCase();
    document.querySelectorAll('#symbol-picker-body .symbol-card').forEach(card => {
        const sym = (card.dataset.symbol || '').toLowerCase();
        const label = (card.querySelector('.symbol-card-label')?.textContent || '').toLowerCase();
        const desc = (card.querySelector('.symbol-card-desc')?.textContent || '').toLowerCase();
        const match = !q || sym.includes(q) || label.includes(q) || desc.includes(q);
        card.style.display = match ? '' : 'none';
    });
    document.querySelectorAll('#symbol-picker-body .symbol-category').forEach(cat => {
        const anyVisible = Array.from(cat.querySelectorAll('.symbol-card'))
            .some(c => c.style.display !== 'none');
        cat.style.display = anyVisible ? '' : 'none';
    });
}

function renderBody(status) {
    const body = document.getElementById('symbol-picker-body');
    if (!body || !catalog) return;
    body.innerHTML = '';

    const current = document.getElementById('symbol-picker-current');
    if (current) {
        const posText = status && status.position
            ? ` — position ouverte ${status.position.direction} @ ${status.position.entry_price}`
            : '';
        current.textContent = `Actif : ${status.current_symbol}${posText}`;
        current.className = 'symbol-picker-current' + (status.position ? ' has-position' : '');
    }

    for (const cat of catalog) {
        const catEl = h('section', { className: `symbol-category cat-${cat.id}` },
            h('header', { className: 'symbol-category-header' },
                h('span', { className: 'symbol-category-icon' }, cat.icon || '•'),
                h('span', { className: 'symbol-category-label' }, cat.label),
                h('small', { className: 'symbol-category-desc' }, cat.description || ''),
            ),
            h('div', { className: 'symbol-category-grid' },
                ...cat.symbols.map(entry => buildSymbolCard(entry, {
                    active: entry.symbol.toUpperCase() === (status.current_symbol || '').toUpperCase(),
                    onPick: (e) => handlePick(e, status),
                })),
            ),
        );
        body.appendChild(catEl);
    }
}

async function handlePick(entry, status) {
    const target = entry.symbol;
    const hasPosition = !!(status && status.position);
    let closePosition = false;

    if (hasPosition) {
        const pos = status.position;
        const msg =
            `Une position est ouverte :\n\n` +
            `  ${pos.direction} ${pos.symbol}  @ ${pos.entry_price}\n\n` +
            `Fermer cette position au marché et basculer sur ${target} ?\n` +
            `(Annuler = ne rien faire)`;
        if (!confirm(msg)) return;
        closePosition = true;
    } else {
        if (!confirm(`Basculer le bot sur ${target} ?\n\nLe bot va redémarrer en interne (quelques secondes).`)) return;
    }

    const btn = document.querySelector(`.symbol-card[data-symbol="${CSS.escape(target)}"]`);
    if (btn) btn.classList.add('is-loading');

    try {
        const { status: httpStatus, data } = await requestSwitch(target, closePosition);
        if (httpStatus >= 200 && httpStatus < 300) {
            alert(`✓ Switch demandé : ${target}\nLe bot redémarre sur le nouveau marché.`);
            closeModal();
            // refresh pill label optimistically
            updatePillLabel(target, entry);
            // actual confirmation comes from WebSocket or next status poll
            setTimeout(refreshStatus, 1500);
        } else if (httpStatus === 409) {
            alert(`Impossible : ${data.reason || 'position_open'}\n\nFerme la position d'abord.`);
        } else {
            alert(`Erreur ${httpStatus} : ${data.error || data.reason || 'unknown'}`);
        }
    } catch (err) {
        alert(`Erreur réseau : ${err.message}`);
    } finally {
        if (btn) btn.classList.remove('is-loading');
    }
}

async function openModal() {
    try {
        if (!catalog) {
            const data = await fetchCatalog();
            catalog = data.catalog || [];
            currentSymbol = data.current_symbol;
        }
        const status = await fetchStatus();
        currentSymbol = status.current_symbol;
        updatePillLabel(currentSymbol, status.current_entry);
        const modal = buildModal();
        renderBody(status);
        modal.classList.add('is-open');
        setTimeout(() => {
            document.getElementById('symbol-picker-search-input')?.focus();
        }, 50);
    } catch (err) {
        console.error('[SymbolPicker] openModal failed', err);
        alert(`Impossible de charger le catalogue : ${err.message}`);
    }
}

async function refreshStatus() {
    try {
        const status = await fetchStatus();
        currentSymbol = status.current_symbol;
        updatePillLabel(currentSymbol, status.current_entry);
    } catch (err) {
        console.warn('[SymbolPicker] refreshStatus failed', err);
    }
}

export function initSymbolPicker() {
    const pill = document.getElementById('symbol-pill');
    if (!pill) {
        console.warn('[SymbolPicker] #symbol-pill not found in DOM');
        return;
    }
    pill.addEventListener('click', openModal);
    refreshStatus();
    // Periodic refresh so pill stays in sync after a successful switch
    setInterval(refreshStatus, 30000);
}
