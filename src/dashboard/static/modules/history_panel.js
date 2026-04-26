/**
 * Binance History Panel — reconstruct positions from raw fills,
 * display realized PNL, latent PNL, win-rate and drawdown.
 */

let _currentSymbol = 'BTC/USDT';
let _currentMethod = 'fifo';
let _currentDays   = 30;
let _equityChart   = null;

export function initHistoryPanel() {
    _bindControls();
}

export async function updateHistoryData() {
    // Only auto-refresh when the tab is visible (slow-lane polling)
    const tab = document.getElementById('tab-history');
    if (!tab || !tab.classList.contains('active')) return;
    await _load();
}

// ── Internal ───────────────────────────────────────────────────────────────

function _bindControls() {
    const symInput    = document.getElementById('history-symbol');
    const methodSel   = document.getElementById('history-method');
    const daysSel     = document.getElementById('history-days');
    const refreshBtn  = document.getElementById('history-refresh');
    const navBtn      = document.getElementById('nav-history');

    if (symInput)   symInput.addEventListener('change',  () => { _currentSymbol = symInput.value.trim() || 'BTC/USDT'; });
    if (methodSel)  methodSel.addEventListener('change', () => { _currentMethod = methodSel.value; });
    if (daysSel)    daysSel.addEventListener('change',   () => { _currentDays   = daysSel.value === 'all' ? null : Number(daysSel.value); });
    if (refreshBtn) refreshBtn.addEventListener('click',  () => _load());

    // Load data the first time the tab is opened
    let _loaded = false;
    if (navBtn) {
        navBtn.addEventListener('click', () => {
            if (!_loaded) { _loaded = true; _load(); }
        });
    }
}

async function _load() {
    _setLoading(true);
    try {
        const params = new URLSearchParams({ symbol: _currentSymbol, method: _currentMethod });
        if (_currentDays) params.set('days', _currentDays);
        const res  = await fetch(`/api/history/analyze?${params}`);
        const data = await res.json();
        if (data.error) { _showError(data.error); return; }
        _render(data);
    } catch (e) {
        _showError(e.message);
    } finally {
        _setLoading(false);
    }
}

function _setLoading(on) {
    const spinner = document.getElementById('history-spinner');
    const table   = document.getElementById('history-table-wrap');
    if (spinner) spinner.style.display = on ? 'flex' : 'none';
    if (table)   table.style.opacity   = on ? '0.4' : '1';
}

function _showError(msg) {
    const el = document.getElementById('history-error');
    if (el) { el.textContent = msg; el.style.display = 'block'; }
}

// ── Render ─────────────────────────────────────────────────────────────────

function _render(data) {
    const errEl = document.getElementById('history-error');
    if (errEl) errEl.style.display = 'none';

    _renderMetrics(data.metrics, data.symbol, data.method);
    _renderOpenLegs(data.open_legs || []);
    _renderEquityChart(data.closed || []);
    _renderTradesTable(data.closed || []);
}

function _fmt(n, dec = 2) {
    return (n === null || n === undefined) ? '—' : n.toFixed(dec);
}
function _sign(n) { return n >= 0 ? '+' : ''; }
function _cls(n)  { return n >= 0 ? 'stat-positive' : 'stat-negative'; }

function _renderMetrics(m, symbol, method) {
    const el = id => document.getElementById(id);

    _setText('hist-symbol-badge', symbol);
    _setText('hist-method-badge', method.toUpperCase());

    // KPI cards
    _setKpi('hist-kpi-trades',    `${m.total_trades}`,                       `${m.winning_trades}W / ${m.losing_trades}L`,  '');
    _setKpi('hist-kpi-winrate',   `${_fmt(m.win_rate, 1)}%`,                 '',                                            m.win_rate >= 50 ? 'stat-positive' : 'stat-negative');
    _setKpi('hist-kpi-pnl',       `${_sign(m.total_pnl_pct)}${_fmt(m.total_pnl_pct)}%`, `$${_sign(m.total_pnl_quote)}${_fmt(m.total_pnl_quote, 4)}`, _cls(m.total_pnl_quote));
    _setKpi('hist-kpi-fees',      `$${_fmt(m.total_fees_quote, 4)}`,         '',                                            'stat-negative');
    _setKpi('hist-kpi-maxdd',     `${_fmt(m.max_drawdown_pct)}%`,            `avg ${_fmt(m.avg_drawdown_pct)}%`,            'stat-negative');
    _setKpi('hist-kpi-best',      `${_sign(m.best_trade_pct)}${_fmt(m.best_trade_pct)}%`, `worst ${_sign(m.worst_trade_pct)}${_fmt(m.worst_trade_pct)}%`, 'stat-positive');
    _setKpi('hist-kpi-latent',    `$${_sign(m.unrealized_pnl_quote)}${_fmt(m.unrealized_pnl_quote, 4)}`, 'Unrealized', _cls(m.unrealized_pnl_quote));
}

function _setKpi(id, value, sub, colorClass) {
    const card = document.getElementById(id);
    if (!card) return;
    const v = card.querySelector('.hist-kpi-value');
    const s = card.querySelector('.hist-kpi-sub');
    if (v) { v.textContent = value; v.className = `hist-kpi-value ${colorClass}`; }
    if (s) s.textContent = sub;
}

function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function _renderOpenLegs(legs) {
    const wrap = document.getElementById('hist-open-legs');
    if (!wrap) return;
    if (!legs.length) { wrap.innerHTML = '<p class="hist-no-open">No open positions</p>'; return; }
    wrap.innerHTML = legs.map(leg => `
        <div class="hist-open-card">
            <span class="hist-open-sym">${leg.symbol}</span>
            <span class="hist-open-dir hist-dir-${leg.direction.toLowerCase()}">${leg.direction}</span>
            <span class="hist-open-detail">qty <b>${leg.qty.toFixed(6)}</b></span>
            <span class="hist-open-detail">avg entry <b>$${leg.avg_price.toLocaleString(undefined, {minimumFractionDigits:2})}</b></span>
            <span class="hist-open-detail">current <b>$${leg.current_price.toLocaleString(undefined, {minimumFractionDigits:2})}</b></span>
            <span class="hist-open-detail ${_cls(leg.unrealized_pnl_quote)}">
                latent <b>${_sign(leg.unrealized_pnl_quote)}$${_fmt(leg.unrealized_pnl_quote, 4)}</b>
                (${_sign(leg.unrealized_pnl_pct)}${_fmt(leg.unrealized_pnl_pct)}%)
            </span>
        </div>
    `).join('');
}

function _renderEquityChart(closed) {
    const container = document.getElementById('hist-equity-chart');
    if (!container || typeof ApexCharts === 'undefined' || !closed.length) return;

    // Build equity curve from cumulative net PNL
    let equity = 0;
    const series = closed.map((leg, i) => {
        equity += leg.pnl_net_quote;
        return { x: new Date(leg.exit_time).getTime(), y: parseFloat(equity.toFixed(4)) };
    });

    if (_equityChart) { _equityChart.destroy(); _equityChart = null; }

    _equityChart = new ApexCharts(container, {
        series: [{ name: 'Cumul. Net PNL ($)', data: series }],
        chart: { type: 'area', height: 200, background: 'transparent', toolbar: { show: false },
                 animations: { enabled: false } },
        stroke: { curve: 'stepline', width: 2 },
        fill:   { type: 'gradient', gradient: { shadeIntensity: 0.3, opacityFrom: 0.4, opacityTo: 0 } },
        colors: [equity >= 0 ? '#00d084' : '#ff4560'],
        xaxis:  { type: 'datetime', labels: { style: { colors: '#9ca3af', fontSize: '11px' } } },
        yaxis:  { labels: { style: { colors: '#9ca3af', fontSize: '11px' },
                             formatter: v => `$${v.toFixed(2)}` } },
        grid:   { borderColor: '#2a2a2a' },
        tooltip: { theme: 'dark', x: { format: 'dd MMM HH:mm' },
                   y: { formatter: v => `$${v.toFixed(4)}` } },
        theme:  { mode: 'dark' },
    });
    _equityChart.render();
}

function _renderTradesTable(closed) {
    const wrap = document.getElementById('hist-table-wrap');
    if (!wrap) return;
    if (!closed.length) { wrap.innerHTML = '<p class="hist-no-trades">No closed trades yet</p>'; return; }

    const rows = [...closed].reverse().map((leg, i) => {
        const wl  = leg.is_win ? '<span class="hist-win">WIN</span>' : '<span class="hist-loss">LOSS</span>';
        const dir = `<span class="hist-dir-${leg.direction.toLowerCase()}">${leg.direction}</span>`;
        const dt  = new Date(leg.exit_time).toLocaleString(navigator.language, { dateStyle: 'short', timeStyle: 'short' });
        return `<tr>
            <td>${closed.length - i}</td>
            <td>${leg.symbol}</td>
            <td>${dir}</td>
            <td>$${leg.entry_price.toLocaleString(undefined, {minimumFractionDigits:2})}</td>
            <td>$${leg.exit_price.toLocaleString(undefined, {minimumFractionDigits:2})}</td>
            <td>${leg.qty.toFixed(6)}</td>
            <td class="${_cls(leg.pnl_net_quote)}">${_sign(leg.pnl_net_quote)}$${_fmt(leg.pnl_net_quote,4)}</td>
            <td class="${_cls(leg.pnl_pct)}">${_sign(leg.pnl_pct)}${_fmt(leg.pnl_pct)}%</td>
            <td>$${_fmt(leg.fee,4)}</td>
            <td>${dt}</td>
            <td>${wl}</td>
        </tr>`;
    }).join('');

    wrap.innerHTML = `
        <div class="hist-table-scroll">
        <table class="hist-table">
            <thead><tr>
                <th>#</th><th>Symbol</th><th>Dir</th>
                <th>Entry</th><th>Exit</th><th>Qty</th>
                <th>Net PNL</th><th>PNL%</th><th>Fee</th><th>Exit Time</th><th>W/L</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        </div>
    `;
}
