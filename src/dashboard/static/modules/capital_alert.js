/**
 * Capital Alert Banner
 *
 * Surfaces sizing hypotheses when the risk manager refuses a trade because
 * the computed position is below the broker minimum. Updated live via
 * WebSocket ('capital_alert' event) and rehydrated on page load via REST.
 */

const FMT_USD = (v, currency) => {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const sign = v < 0 ? '-' : '';
    const abs = Math.abs(v);
    const sym = (currency || 'USD') === 'EUR' ? '€' : '$';
    if (abs >= 1000) return `${sign}${sym}${abs.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
    if (abs >= 100)  return `${sign}${sym}${abs.toFixed(0)}`;
    return `${sign}${sym}${abs.toFixed(2)}`;
};

function render(alert) {
    const banner = document.getElementById('capital-alert-banner');
    if (!banner) return;

    if (!alert || typeof alert !== 'object') {
        banner.style.display = 'none';
        return;
    }

    const currency = alert.account_currency || 'USD';
    const setTxt = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };

    setTxt('capital-alert-symbol', `${alert.symbol || '—'} · ${alert.side || '—'}`);

    const headlineBits = [];
    if (alert.sizing_warning) {
        headlineBits.push(alert.sizing_warning);
    } else {
        headlineBits.push('Position sous le minimum broker — rechargez le compte pour activer ce trade.');
    }
    const headline = headlineBits.join(' ');
    const headlineEl = document.getElementById('capital-alert-headline');
    if (headlineEl) headlineEl.textContent = headline;

    setTxt('capital-alert-topup',
        `+${FMT_USD(alert.capital_top_up, currency)}  (→ ${FMT_USD(alert.capital_needed_total, currency)})`);
    setTxt('capital-alert-lots',
        (alert.required_lots !== undefined && alert.required_lots !== null)
            ? Number(alert.required_lots).toFixed(4)
            : '—');
    setTxt('capital-alert-notional', FMT_USD(alert.required_notional, currency));
    setTxt('capital-alert-leverage',
        alert.leverage ? `${Number(alert.leverage).toFixed(0)}x` : '1x');
    setTxt('capital-alert-tp', `+${FMT_USD(alert.expected_gain_at_tp, currency)}`);
    setTxt('capital-alert-sl', `-${FMT_USD(alert.expected_loss_at_sl, currency)}`);

    banner.style.display = 'flex';

    // Also mirror to console for quick debugging
    console.warn(
        `[CapitalAlert] ${alert.symbol} ${alert.side} — top-up ${FMT_USD(alert.capital_top_up, currency)} ` +
        `→ min lot ${alert.required_lots} (${FMT_USD(alert.required_notional, currency)} notional, ` +
        `leverage ${alert.leverage}x) ⇒ TP +${FMT_USD(alert.expected_gain_at_tp, currency)} / ` +
        `SL -${FMT_USD(alert.expected_loss_at_sl, currency)}`
    );
}

async function dismiss() {
    const banner = document.getElementById('capital-alert-banner');
    if (banner) banner.style.display = 'none';
    try {
        await fetch('/api/execution/capital-alert/dismiss', { method: 'POST' });
    } catch (e) {
        console.error('[CapitalAlert] dismiss failed', e);
    }
}

async function hydrate() {
    try {
        const res = await fetch('/api/execution/capital-alert');
        if (!res.ok) return;
        const data = await res.json();
        if (data && data.alert) render(data.alert);
    } catch (e) {
        // Silent — endpoint may not be registered on older backends
    }
}

export function initCapitalAlert() {
    document.addEventListener('capital-alert-update', (ev) => {
        render(ev.detail);
    });

    const btn = document.getElementById('capital-alert-dismiss');
    if (btn) btn.addEventListener('click', dismiss);

    hydrate();
}
