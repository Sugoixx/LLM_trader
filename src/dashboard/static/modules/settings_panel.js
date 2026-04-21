/**
 * Settings panel — runtime trading parameter management.
 * Controls: timeframe, style presets, trailing, debate, risk caps, model params.
 */

let currentSettings = {};

// ── Preset fingerprints (mirror of server PRESETS) ────────────────
// We detect active preset by comparing key settings to known preset values.
const PRESET_FINGERPRINTS = {
    aggressive: {
        'execution_engine.trailing_atr_multiplier': 1.5,
        'execution_engine.trailing_breakeven_on_tp1': false,
        'execution_engine.partial_enabled': false,
        'debate.enabled': false,
    },
    moderate: {
        'execution_engine.trailing_atr_multiplier': 2.0,
        'execution_engine.trailing_breakeven_on_tp1': true,
        'execution_engine.partial_enabled': false,
        'debate.enabled': true,
        'debate.use_quick_model': true,
    },
    conservative: {
        'execution_engine.trailing_atr_multiplier': 2.5,
        'execution_engine.trailing_breakeven_on_tp1': true,
        'execution_engine.partial_enabled': true,
        'debate.enabled': true,
        'debate.use_quick_model': false,
        'live_trading.max_order_usd': 250,
    },
};

// ── Helpers ──────────────────────────────────────────────────────
function el(id) { return document.getElementById(id); }

function showFeedback(msg, isError = false) {
    const fb = el('settings-feedback');
    if (!fb) return;
    fb.textContent = msg;
    fb.className = 'settings-feedback ' + (isError ? 'settings-feedback-error' : 'settings-feedback-success');
    fb.style.display = 'block';
    setTimeout(() => { fb.style.display = 'none'; }, 4000);
}

// ── API ──────────────────────────────────────────────────────────
async function fetchSettings() {
    try {
        const res = await fetch('/api/settings');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentSettings = data.settings || {};
        renderSettings(data);
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function sendUpdate(settings) {
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings }),
        });
        const data = await res.json();
        if (data.errors && data.errors.length > 0) {
            showFeedback(data.errors.map(e => e.error).join('; '), true);
        } else {
            showFeedback(`Updated: ${data.applied.join(', ')}`);
        }
        currentSettings = data.settings || currentSettings;
        renderValues();
    } catch (e) {
        showFeedback('Failed to save: ' + e.message, true);
    }
}

async function applyPreset(presetName) {
    try {
        const res = await fetch('/api/settings/preset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset: presetName }),
        });
        const data = await res.json();
        if (data.error) {
            showFeedback(data.error, true);
            return;
        }
        showFeedback(`Preset "${data.label}" applied`);
        currentSettings = data.settings || currentSettings;
        renderValues();
        highlightActivePreset(presetName);
    } catch (e) {
        showFeedback('Preset failed: ' + e.message, true);
    }
}

// ── Rendering ────────────────────────────────────────────────────
function renderSettings(data) {
    renderValues();

    // Bind timeframe select
    const tfSel = el('set-timeframe');
    if (tfSel && data.valid_timeframes) {
        tfSel.innerHTML = '';
        data.valid_timeframes.forEach(tf => {
            const opt = document.createElement('option');
            opt.value = tf;
            opt.textContent = tf;
            tfSel.appendChild(opt);
        });
        tfSel.value = currentSettings['general.timeframe'] || '30m';
    }
}

function renderValues() {
    const s = currentSettings;

    setVal('set-timeframe', s['general.timeframe']);
    setVal('set-cooldown', s['general.analysis_cooldown_minutes']);
    setVal('set-candles', s['general.candle_limit']);
    setVal('set-chart-candles', s['general.ai_chart_candle_limit']);

    setChecked('set-trailing', s['execution_engine.trailing_enabled']);
    setVal('set-atr-mult', s['execution_engine.trailing_atr_multiplier']);
    setChecked('set-breakeven', s['execution_engine.trailing_breakeven_on_tp1']);
    setChecked('set-partial', s['execution_engine.partial_enabled']);

    setChecked('set-debate', s['debate.enabled']);
    setChecked('set-debate-quick', s['debate.use_quick_model']);
    setChecked('set-debate-skip-hold', s['debate.skip_for_hold']);

    setVal('set-max-order', s['live_trading.max_order_usd']);
    setChecked('set-confirm', s['live_trading.confirm_orders']);

    setVal('set-temperature', s['model_config.temperature']);
    setVal('set-max-tokens', s['model_config.max_tokens']);

    // Update range display
    const tempDisplay = el('set-temperature-display');
    if (tempDisplay) tempDisplay.textContent = s['model_config.temperature'];
    const atrDisplay = el('set-atr-mult-display');
    if (atrDisplay) atrDisplay.textContent = s['execution_engine.trailing_atr_multiplier'];

    // Auto-detect and highlight active preset
    const detected = detectActivePreset(s);
    if (detected) highlightActivePreset(detected);
}

function setVal(id, val) {
    const e = el(id);
    if (e && val !== undefined) e.value = val;
}
function setChecked(id, val) {
    const e = el(id);
    if (e) e.checked = !!val;
}

function highlightActivePreset(name) {
    document.querySelectorAll('.preset-btn').forEach(btn => {
        const isActive = btn.dataset.preset === name;
        btn.classList.toggle('preset-active', isActive);
        // Show/hide checkmark badge
        let badge = btn.querySelector('.preset-active-check');
        if (isActive && !badge) {
            badge = document.createElement('span');
            badge.className = 'preset-active-check';
            badge.textContent = '✓';
            btn.prepend(badge);
        } else if (!isActive && badge) {
            badge.remove();
        }
    });
}

/**
 * Detect which preset best matches the current settings.
 * Returns the preset name with the most matched fingerprint keys, or null.
 */
function detectActivePreset(settings) {
    let bestMatch = null;
    let bestScore = 0;

    for (const [preset, fingerprint] of Object.entries(PRESET_FINGERPRINTS)) {
        const keys = Object.keys(fingerprint);
        let matches = 0;
        for (const key of keys) {
            const sv = settings[key];
            const fv = fingerprint[key];
            // Compare with type coercion for floats
            if (typeof fv === 'number') {
                if (Math.abs(parseFloat(sv) - fv) < 0.001) matches++;
            } else if (typeof fv === 'boolean') {
                if (Boolean(sv) === fv) matches++;
            } else {
                if (String(sv) === String(fv)) matches++;
            }
        }
        const score = matches / keys.length;
        if (score > bestScore && score >= 0.75) {   // ≥75% match = consider it active
            bestScore = score;
            bestMatch = preset;
        }
    }
    return bestMatch;
}

// ── Collect & send changes ───────────────────────────────────────
function collectSetting(key, el, type) {
    let value;
    if (type === 'bool') value = el.checked;
    else if (type === 'int') value = parseInt(el.value, 10);
    else if (type === 'float') value = parseFloat(el.value);
    else value = el.value;
    return { key, value };
}

// ── Init ─────────────────────────────────────────────────────────
export function initSettingsPanel() {
    const container = el('settings-panel-content');
    if (!container) return;

    // Preset buttons
    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', () => applyPreset(btn.dataset.preset));
    });

    // Timeframe — instant apply
    bindChange('set-timeframe', 'general.timeframe', 'str');

    // Analysis
    bindChange('set-cooldown', 'general.analysis_cooldown_minutes', 'int');
    bindChange('set-candles', 'general.candle_limit', 'int');
    bindChange('set-chart-candles', 'general.ai_chart_candle_limit', 'int');

    // Trailing
    bindChange('set-trailing', 'execution_engine.trailing_enabled', 'bool');
    bindChange('set-atr-mult', 'execution_engine.trailing_atr_multiplier', 'float');
    bindChange('set-breakeven', 'execution_engine.trailing_breakeven_on_tp1', 'bool');
    bindChange('set-partial', 'execution_engine.partial_enabled', 'bool');

    // Debate
    bindChange('set-debate', 'debate.enabled', 'bool');
    bindChange('set-debate-quick', 'debate.use_quick_model', 'bool');
    bindChange('set-debate-skip-hold', 'debate.skip_for_hold', 'bool');

    // Risk
    bindChange('set-max-order', 'live_trading.max_order_usd', 'float');
    bindChange('set-confirm', 'live_trading.confirm_orders', 'bool');

    // Model
    bindChange('set-temperature', 'model_config.temperature', 'float');
    bindChange('set-max-tokens', 'model_config.max_tokens', 'int');

    // Range display updates
    const tempSlider = el('set-temperature');
    if (tempSlider) {
        tempSlider.addEventListener('input', () => {
            const d = el('set-temperature-display');
            if (d) d.textContent = tempSlider.value;
        });
    }
    const atrSlider = el('set-atr-mult');
    if (atrSlider) {
        atrSlider.addEventListener('input', () => {
            const d = el('set-atr-mult-display');
            if (d) d.textContent = atrSlider.value;
        });
    }

    fetchSettings();
}

function bindChange(elementId, settingKey, type) {
    const element = el(elementId);
    if (!element) return;
    const event = (element.type === 'checkbox') ? 'change' : 'change';
    element.addEventListener(event, () => {
        const update = collectSetting(settingKey, element, type);
        sendUpdate([update]);
    });
}

export async function refreshSettings() {
    await fetchSettings();
}


