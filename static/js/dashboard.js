// Values from the server (JSON in templates/index.html, #dashboard-config)
window.DASHBOARD_CONFIG = JSON.parse(document.getElementById('dashboard-config').textContent);

// ===== I18N =====
// UI strings live in locales/<lang>.json (embedded by app.py). Static markup
// uses data-i18n (text) and data-i18n-attr (attr:key;...) attributes; code uses t(key, params).
var I18N_MESSAGES = window.DASHBOARD_CONFIG.locales;
var I18N_DEFAULT = window.DASHBOARD_CONFIG.defaultLanguage;
var I18N_STORAGE_KEY = 'dashboard-language';
var i18nLang = (function() {
    try {
        var saved = localStorage.getItem(I18N_STORAGE_KEY);
        if (saved && I18N_MESSAGES[saved]) return saved;
    } catch (e) {}
    return I18N_DEFAULT;
})();

function i18nLookup(messages, key) {
    var value = messages;
    var parts = key.split('.');
    for (var i = 0; i < parts.length; i++) {
        if (value == null || typeof value !== 'object') return null;
        value = value[parts[i]];
    }
    return typeof value === 'string' ? value : null;
}

// Translate a key, falling back to the default language, then the key itself.
// {name} placeholders are filled from params.
function t(key, params) {
    var msg = i18nLookup(I18N_MESSAGES[i18nLang], key);
    if (msg == null) msg = i18nLookup(I18N_MESSAGES[I18N_DEFAULT], key);
    if (msg == null) return key;
    if (params) {
        msg = msg.replace(/\{(\w+)\}/g, function(m, name) {
            return params[name] != null ? params[name] : m;
        });
    }
    return msg;
}

// Locale for dates/times, e.g. "uk-UA"
function i18nLocale() {
    return t('meta.locale');
}

// Set translated text on an element and remember the key, so a language
// switch re-translates it without waiting for the next data refresh.
function setI18nText(el, key, params) {
    if (!el) return;
    el.setAttribute('data-i18n', key);
    if (params) el.setAttribute('data-i18n-params', JSON.stringify(params));
    else el.removeAttribute('data-i18n-params');
    el.textContent = t(key, params);
}

// Set untranslated text (numbers, values) on an element that may carry a key.
function setPlainText(el, text) {
    if (!el) return;
    el.removeAttribute('data-i18n');
    el.removeAttribute('data-i18n-params');
    el.textContent = text;
}

function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
        var params = el.getAttribute('data-i18n-params');
        el.textContent = t(el.getAttribute('data-i18n'), params ? JSON.parse(params) : null);
    });
    document.querySelectorAll('[data-i18n-attr]').forEach(function(el) {
        el.getAttribute('data-i18n-attr').split(';').forEach(function(pair) {
            var idx = pair.indexOf(':');
            if (idx < 0) return;
            var attr = pair.slice(0, idx).trim();
            var key = pair.slice(idx + 1).trim();
            el.setAttribute(attr, t(key, { name: t('meta.name') }));
        });
    });
    document.documentElement.lang = i18nLang;
    // A name set in the settings (DASHBOARD_TITLE) stays as typed; else the translated default
    document.title = window.DASHBOARD_CONFIG.title || t('app.title');
    renderLanguageOptions();
}

function setLanguage(lang) {
    if (!I18N_MESSAGES[lang]) return;
    i18nLang = lang;
    try { localStorage.setItem(I18N_STORAGE_KEY, lang); } catch (e) {}
    applyTranslations();
    refreshTranslatedContent();
}

// Language select in the settings panel. Each language is listed by its own
// name ("English", "Українська").
function renderLanguageOptions() {
    var select = document.getElementById('language-select');
    if (!select) return;
    if (!select.options.length) {
        select.innerHTML = Object.keys(I18N_MESSAGES).map(function(lang) {
            var meta = I18N_MESSAGES[lang].meta || {};
            return '<option value="' + lang + '" lang="' + lang + '">' + escapeHtml(meta.name || lang) + '</option>';
        }).join('');
    }
    select.value = i18nLang;
}

// Re-render content that code builds from translated strings
function refreshTranslatedContent() {
    var refreshers = [
        function() { if (typeof cachedWeatherData !== 'undefined' && cachedWeatherData) updateWeatherForecastBanner(cachedWeatherData); },
        function() { renderPlannedOutages(); },
        function() { updateOutageInfo(); },
        function() { renderPhaseStats(phaseStatsData); },
        function() { if (typeof generatorData !== 'undefined' && generatorData) { updateGeneratorBanner(generatorData); updateGeneratorCard(generatorData); } },
        function() { updateOnboardingUI(); },
        function() { renderAlerts(); },
        function() { renderSmartMeter(); },
        function() { renderOutageReadiness(); },
        function() { renderSparklines(); renderHistoryDetails(); },
        function() { updateClock(); },
        function() { renderOutageLog(); },
        function() { renderCardSettings(); },
        function() { updateDashboard(); },
    ];
    refreshers.forEach(function(fn) {
        try { fn(); } catch (e) { console.error('i18n refresh failed:', e); }
    });
}

applyTranslations();

// Inverter config (populated from /api/data response)
var inverterConfig = null;
var outageScheduleIntervalId = null;
var outageInfoIntervalId = null;
var gridProgressBarIntervalId = null;

function applyInverterConfig(config) {
    if (!config) return;
    if (inverterConfig) {
        // Applied once; if the server's config changed (e.g. background
        // auto-detect finished after the page loaded), reload to rebuild the UI.
        if (JSON.stringify(config) !== JSON.stringify(inverterConfig)) location.reload();
        return;
    }
    inverterConfig = config;

    // String / micro inverters use other register maps: say so instead of
    // letting wrong numbers pass for real ones
    var warning = document.getElementById('inverter-warning');
    if (warning && config.supported === false) {
        setI18nText(warning, 'inverter.unsupported', {
            type: t('inverter.family_' + config.family),
            code: '0x' + ('000' + (config.device_type || 0).toString(16).toUpperCase()).slice(-4),
        });
        warning.hidden = false;
    }

    // Single-phase: hide per-phase load and phase analytics
    if (config.phases === 1) {
        var consumptionPhases = document.getElementById('consumption-phases');
        if (consumptionPhases) consumptionPhases.style.display = 'none';
        var phaseAnalytics = document.getElementById('phase-analytics-card');
        if (phaseAnalytics) phaseAnalytics.style.display = 'none';
    }

    // No battery: hide battery card and banner
    if (config.has_battery === false) {
        var batteryCard = document.getElementById('battery-card');
        if (batteryCard) batteryCard.style.display = 'none';
        var batteryBanner = document.getElementById('battery-banner');
        if (batteryBanner) batteryBanner.style.display = 'none';
    }

    // Single PV string: hide PV2 stat
    if (config.pv_strings === 1) {
        var pv2Stat = document.getElementById('pv2-stat');
        if (pv2Stat) pv2Stat.style.display = 'none';
    }

    // No outage schedule: hide outage UI and stop polling
    if (config.has_outage_schedule === false) {
        var outageInfo = document.getElementById('grid-outage-info');
        if (outageInfo) outageInfo.style.display = 'none';
        var plannedOutages = document.getElementById('planned-outages-card');
        if (plannedOutages) plannedOutages.style.display = 'none';
        document.body.classList.add('outages-disabled');
        var plannedOutagesBanner = document.getElementById('planned-outages-banner');
        if (plannedOutagesBanner) plannedOutagesBanner.style.display = 'none';
        // Stop outage polling intervals
        if (outageScheduleIntervalId) {
            clearInterval(outageScheduleIntervalId);
            outageScheduleIntervalId = null;
        }
        if (outageInfoIntervalId) {
            clearInterval(outageInfoIntervalId);
            outageInfoIntervalId = null;
        }
        // Hide grid progress bar and stop its interval
        var gridBannerBar = document.getElementById('grid-banner-bar');
        if (gridBannerBar) gridBannerBar.style.display = 'none';
        if (gridProgressBarIntervalId) {
            clearInterval(gridProgressBarIntervalId);
            gridProgressBarIntervalId = null;
        }
    }

    // Generator support
    if (config.has_generator === true) {
        document.body.classList.add('generator-visible', 'has-generator');
        fetchGeneratorData();
        generatorIntervalId = setInterval(fetchGeneratorData, 30000);
    } else {
        document.body.classList.remove('generator-visible', 'has-generator');
        if (generatorIntervalId) {
            clearInterval(generatorIntervalId);
            generatorIntervalId = null;
        }
    }
}

// Fullscreen mode
function toggleFullscreen() {
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        // Enter fullscreen
        const elem = document.documentElement;
        if (elem.requestFullscreen) {
            elem.requestFullscreen();
        } else if (elem.webkitRequestFullscreen) {
            elem.webkitRequestFullscreen(); // Safari/iOS
        }
    } else {
        // Exit fullscreen
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen(); // Safari/iOS
        }
    }
}

// Update fullscreen icon on change
document.addEventListener('fullscreenchange', updateFullscreenIcon);
document.addEventListener('webkitfullscreenchange', updateFullscreenIcon);

function updateFullscreenIcon() {
    const enterIcon = document.getElementById('fullscreen-icon-enter');
    const exitIcon = document.getElementById('fullscreen-icon-exit');
    if (document.fullscreenElement || document.webkitFullscreenElement) {
        enterIcon.style.display = 'none';
        exitIcon.style.display = 'block';
        document.body.classList.add('fullscreen-mode');
        // Its button is hidden in fullscreen, so don't leave it open
        closeSettingsPanel();
    } else {
        enterIcon.style.display = 'block';
        exitIcon.style.display = 'none';
        document.body.classList.remove('fullscreen-mode');
    }
    // Update grid progress bar sizing on fullscreen change
    if (typeof updateGridProgressBar === 'function') {
        updateGridProgressBar();
    }
    updatePageScrolled();
    scheduleReturnToTop();
}

// The fullscreen scroll hint is only for the first screen: hide it once the
// page is scrolled, show it again back at the top.
var SCROLL_HINT_THRESHOLD = 40;  // px; ignores tiny touch/rubber-band movement

function updatePageScrolled() {
    document.body.classList.toggle('page-scrolled', window.scrollY > SCROLL_HINT_THRESHOLD);
}

window.addEventListener('scroll', updatePageScrolled, { passive: true });
updatePageScrolled();

// Settings panel (opening it requires the settings PIN)
var settingsPin = null;  // last verified PIN, sent with settings API calls

// The panel itself opens without a PIN: local (per-device) settings like low
// power mode are always available. Server-side settings (updates, config) are
// locked until the PIN is entered, and lock again when the panel closes.
function toggleSettingsPanel() {
    var panel = document.getElementById('settings-panel');
    if (panel.classList.contains('open')) {
        closeSettingsPanel();
    } else {
        lockServerSettings();
        renderCardSettings();
        panel.classList.add('open');
    }
}

function closeSettingsPanel() {
    document.getElementById('settings-panel').classList.remove('open');
    lockServerSettings();
}

// A click anywhere outside the open panel closes it, and only closes it (the
// dashboard underneath doesn't react). Not for the settings button (it toggles
// the panel itself) or the dialogs the panel opens (PIN, configuration editor).
document.addEventListener('click', function(e) {
    var panel = document.getElementById('settings-panel');
    if (!panel || !panel.classList.contains('open')) return;
    if (e.target.closest('#settings-panel, .settings-toggle-btn, .pin-overlay, .onboarding-overlay, .restart-overlay, .details-overlay')) return;
    e.preventDefault();
    e.stopPropagation();
    closeSettingsPanel();
}, true);

function lockServerSettings() {
    document.getElementById('settings-server').classList.add('locked');
}

function unlockServerSettings() {
    showPinPrompt(function() {
        document.getElementById('settings-server').classList.remove('locked');
    });
}

// Low power mode: no animations, transitions or blur (CSS: html.low-power).
// Stored per device; the inline script in <head> applies it before first paint.
var LOW_POWER_STORAGE_KEY = 'dashboard-low-power';

function isLowPower() {
    return document.documentElement.classList.contains('low-power');
}

function toggleLowPower() {
    var on = !isLowPower();
    document.documentElement.classList.toggle('low-power', on);
    try { localStorage.setItem(LOW_POWER_STORAGE_KEY, on ? '1' : '0'); } catch (e) {}
    updateLowPowerToggle();
}

function updateLowPowerToggle() {
    var toggle = document.getElementById('low-power-toggle');
    if (!toggle) return;
    toggle.classList.toggle('on', isLowPower());
    toggle.setAttribute('aria-checked', String(isLowPower()));
}

updateLowPowerToggle();

// Fullscreen: back to the first screen RETURN_TO_TOP_MS after the last
// interaction, so a kiosk isn't left scrolled down on the details.
// Stored per device; on unless switched off.
var RETURN_TO_TOP_STORAGE_KEY = 'dashboard-return-to-top';
var RETURN_TO_TOP_MS = 30000;
var returnToTopOn = (function() {
    try { return localStorage.getItem(RETURN_TO_TOP_STORAGE_KEY) !== '0'; } catch (e) { return true; }
})();
var returnToTopTimer = null;
var returningToTop = false;  // our own scrolling doesn't count as an interaction

function toggleReturnToTop() {
    returnToTopOn = !returnToTopOn;
    try { localStorage.setItem(RETURN_TO_TOP_STORAGE_KEY, returnToTopOn ? '1' : '0'); } catch (e) {}
    updateReturnToTopToggle();
    scheduleReturnToTop();
}

function updateReturnToTopToggle() {
    var toggle = document.getElementById('return-to-top-toggle');
    if (!toggle) return;
    toggle.classList.toggle('on', returnToTopOn);
    toggle.setAttribute('aria-checked', String(returnToTopOn));
}

// (Re)start the countdown; nothing to do outside fullscreen or at the top
function scheduleReturnToTop() {
    clearTimeout(returnToTopTimer);
    returnToTopTimer = null;
    if (!returnToTopOn || !document.body.classList.contains('fullscreen-mode')) return;
    if (window.scrollY <= SCROLL_HINT_THRESHOLD) return;
    returnToTopTimer = setTimeout(returnToTop, RETURN_TO_TOP_MS);
}

function returnToTop() {
    returnToTopTimer = null;
    if (!returnToTopOn || !document.body.classList.contains('fullscreen-mode')) return;
    // A card is still being dragged: wait for the next quiet period
    if (document.body.classList.contains('cards-dragging')) { scheduleReturnToTop(); return; }
    returningToTop = true;
    closeHistoryDetails();
    closeOutageLog();
    window.scrollTo({ top: 0, behavior: isLowPower() ? 'auto' : 'smooth' });
    setTimeout(function() { returningToTop = false; }, 1500);
}

['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(function(type) {
    window.addEventListener(type, function() {
        returningToTop = false;
        scheduleReturnToTop();
    }, { passive: true });
});
window.addEventListener('scroll', function() {
    if (!returningToTop) scheduleReturnToTop();
}, { passive: true });

updateReturnToTopToggle();

// fetch() for endpoints behind the settings PIN
// Requests to the dashboard server give up after this long. Without a timeout a
// request that hangs on flaky Wi-Fi never settles; a few of those use up the
// browser's 6 connections to the server and every later request just queues,
// so the cards stop updating until the page is reloaded.
var API_TIMEOUT_MS = 15000;

function apiFetch(url, options) {
    options = options || {};
    if (typeof AbortController === 'undefined' || options.signal) return fetch(url, options);
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, API_TIMEOUT_MS);
    return fetch(url, Object.assign({}, options, { signal: controller.signal }))
        .finally(function() { clearTimeout(timer); });
}

function settingsFetch(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers, { 'X-Settings-Pin': settingsPin || '' });
    return apiFetch(url, options);
}

// What to do after a correct PIN (unlock server settings, clear stats, ...)
var pinSuccessAction = null;

function showPinPrompt(onSuccess) {
    pinSuccessAction = onSuccess || null;
    var input = document.getElementById('pin-input');
    input.value = '';
    document.getElementById('pin-error').textContent = '';
    document.getElementById('pin-submit').disabled = false;
    document.getElementById('pin-overlay').classList.remove('hidden');
    input.focus();
}

function closePinPrompt() {
    document.getElementById('pin-overlay').classList.add('hidden');
}

function submitSettingsPin() {
    var input = document.getElementById('pin-input');
    var errorEl = document.getElementById('pin-error');
    var submitBtn = document.getElementById('pin-submit');
    var pin = input.value;
    if (!pin) return;
    submitBtn.disabled = true;

    apiFetch('/api/settings/verify-pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pin }),
    })
        .then(function(r) { return r.json().then(function(data) { return { status: r.status, data: data }; }); })
        .then(function(res) {
            submitBtn.disabled = false;
            if (res.status === 200) {
                settingsPin = pin;
                closePinPrompt();
                var action = pinSuccessAction;
                pinSuccessAction = null;
                if (action) action();
                return;
            }
            errorEl.textContent = res.status === 429
                ? t('pin.too_many', { seconds: res.data.retry_after })
                : t('pin.wrong');
            input.value = '';
            input.focus();
            var dialog = document.getElementById('pin-dialog');
            dialog.classList.remove('shake');
            void dialog.offsetWidth;  // restart the animation
            dialog.classList.add('shake');
        })
        .catch(function() {
            submitBtn.disabled = false;
            errorEl.textContent = t('pin.check_failed');
        });
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !document.getElementById('pin-overlay').classList.contains('hidden')) {
        closePinPrompt();
    }
});

// Track grid state
let lastGridOnline = null;
let notificationsEnabled = false;


// Check notification permission on load
if ('Notification' in window) {
    if (Notification.permission === 'granted') {
        notificationsEnabled = true;
        document.getElementById('notification-setup').classList.add('hidden');
    } else if (Notification.permission === 'denied') {
        document.getElementById('notification-setup').classList.add('hidden');
    } else if (sessionStorage.getItem('notificationSetupDismissed') === 'true') {
        document.getElementById('notification-setup').classList.add('hidden');
    }
} else {
    document.getElementById('notification-setup').classList.add('hidden');
}

function requestNotificationPermission() {
    if ('Notification' in window) {
        Notification.requestPermission().then(permission => {
            if (permission === 'granted') {
                notificationsEnabled = true;
                document.getElementById('notification-setup').classList.add('hidden');
                showNotification(t('notifications.enabled_title'), t('notifications.enabled_body'));
            }
        });
    }
}

function closeNotificationSetup() {
    document.getElementById('notification-setup').classList.add('hidden');
    // Remember the dismissal for this session
    sessionStorage.setItem('notificationSetupDismissed', 'true');
}


function showNotification(title, body) {
    if (notificationsEnabled && 'Notification' in window && Notification.permission === 'granted') {
        new Notification(title, {
            body: body,
            icon: '⚡',
            requireInteraction: true
        });
    }
}

var BATTERY_STATUS_KEYS = { 'Charging': 'charging', 'Discharging': 'discharging', 'Idle': 'idle', 'N/A': 'na' };

function batteryStatusText(status) {
    var key = BATTERY_STATUS_KEYS[status];
    return key ? t('battery.status.' + key) : status;
}

function formatGeneratorDuration(decimalHours) {
    if (decimalHours === null || decimalHours === undefined || isNaN(decimalHours)) return '--';
    var totalMinutes = Math.round(decimalHours * 60);
    var h = Math.floor(totalMinutes / 60);
    var m = totalMinutes % 60;
    if (h > 0) {
        return t('duration.hm', { h: h, m: m });
    } else {
        return t('duration.m', { m: m });
    }
}
// Phase statistics
async function loadPhaseStats() {
    try {
        const response = await apiFetch('/api/phase-stats');
        const stats = await response.json();
        renderPhaseStats(stats);
    } catch (e) {
        console.error('Failed to load phase stats:', e);
    }
}

// Phase balance card: today's kWh per phase, then one bar per day — length =
// the day's total (relative to the biggest day), coloured parts = each phase's share
var PHASE_DAYS_SHOWN = 7;
var phaseStatsData = [];

// kWh with 2 decimals below 1 kWh (low loads: 0.03, not 0.0), else 1 decimal
function fmtKwh(value) {
    return fmt(value, (value || 0) < 1 ? 2 : 1);
}

function localDateString(date) {
    return date.getFullYear() + '-' + ('0' + (date.getMonth() + 1)).slice(-2) + '-' + ('0' + date.getDate()).slice(-2);
}

function renderPhaseStats(stats) {
    phaseStatsData = stats || [];
    var days = phaseStatsData.slice(0, PHASE_DAYS_SHOWN);   // newest first
    var todayKey = localDateString(new Date());
    var today = days[0] && days[0].date === todayKey ? days[0] : null;
    ['l1', 'l2', 'l3'].forEach(function(p) {
        document.getElementById('phase-today-' + p).textContent = today ? fmtKwh(today[p + '_kwh']) : '--';
    });

    var list = document.getElementById('phase-days');
    if (!days.length) {
        list.innerHTML = '<div class="phase-days-empty">' + escapeHtml(t('phase_stats.no_data')) + '</div>';
        return;
    }
    var maxTotal = Math.max.apply(null, days.map(function(d) { return d.total_kwh || 0; })) || 1;
    list.innerHTML = days.map(function(d) {
        var label = d.date === todayKey ? t('phase_stats.today')
            : new Date(d.date + 'T00:00:00').toLocaleDateString(i18nLocale(), { weekday: 'short', day: 'numeric' });
        var width = Math.max(2, (d.total_kwh || 0) / maxTotal * 100);
        var parts = ['l1', 'l2', 'l3'].map(function(p) {
            return '<span class="phase-day-part ' + p + '" style="width:' + (d[p + '_pct'] || 0) + '%"></span>';
        }).join('');
        var details = ['l1', 'l2', 'l3'].map(function(p, i) {
            return '<span><span class="phase-label ' + p + '">L' + (i + 1) + '</span> ' +
                escapeHtml(t('phase_stats.detail', { kwh: fmt(d[p + '_kwh'], 2), pct: fmt(d[p + '_pct'], 0), peak: d[p + '_max'] || 0 })) +
                '</span>';
        }).join('');
        return '<div class="phase-day' + (d.date === todayKey ? ' today' : '') + '" onclick="this.classList.toggle(\'open\')">' +
            '<span class="phase-day-label">' + escapeHtml(label) + '</span>' +
            '<span class="phase-day-track"><span class="phase-day-bar" style="width:' + width.toFixed(1) + '%">' + parts + '</span></span>' +
            '<span class="phase-day-total">' + fmtKwh(d.total_kwh) + '</span>' +
            '<span class="phase-day-details">' + details + '</span></div>';
    }).join('');
}

// Clearing stats needs the settings PIN (same as the settings panel)
function clearPhaseStats() {
    if (!confirm(t('phase_stats.confirm_clear'))) return;
    showPinPrompt(function() {
        settingsFetch('/api/phase-stats/clear', { method: 'POST' })
            .then(function(r) {
                if (!r.ok) return r.json().then(function(d) { alert(t('common.error', { message: d.error })); });
                loadPhaseStats();
            })
            .catch(function(e) { alert(t('common.error', { message: e })); });
    });
}

function updatePhaseDisplay(data) {
    if (!data.load_l1) return;

    const l1 = data.load_l1 || 0;
    const l2 = data.load_l2 || 0;
    const l3 = data.load_l3 || 0;
    const total = l1 + l2 + l3;

    // Calculate percentages
    const l1Pct = total > 0 ? (l1 / total * 100).toFixed(0) : 0;
    const l2Pct = total > 0 ? (l2 / total * 100).toFixed(0) : 0;
    const l3Pct = total > 0 ? (l3 / total * 100).toFixed(0) : 0;

    // Update bars (width based on percentage of total)
    document.getElementById('phase-bar-l1').style.width = l1Pct + '%';
    document.getElementById('phase-bar-l2').style.width = l2Pct + '%';
    document.getElementById('phase-bar-l3').style.width = l3Pct + '%';

    // Update percentages
    document.getElementById('phase-pct-l1').textContent = l1Pct + '%';
    document.getElementById('phase-pct-l2').textContent = l2Pct + '%';
    document.getElementById('phase-pct-l3').textContent = l3Pct + '%';

    // Update values
    document.getElementById('phase-l1').textContent = l1 + ' ' + t('units.w');
    document.getElementById('phase-l2').textContent = l2 + ' ' + t('units.w');
    document.getElementById('phase-l3').textContent = l3 + ' ' + t('units.w');

    // Update voltages
    document.getElementById('voltage-l1').textContent = t('units.voltage_v', { value: (data.voltage_l1 || 0).toFixed(0) });
    document.getElementById('voltage-l2').textContent = t('units.voltage_v', { value: (data.voltage_l2 || 0).toFixed(0) });
    document.getElementById('voltage-l3').textContent = t('units.voltage_v', { value: (data.voltage_l3 || 0).toFixed(0) });
}

// Power as a number and unit: 950 -> "950" W, 3135 -> "3.1" kW (locale decimal separator)
function powerParts(watts) {
    var kw = watts >= 999.5;
    return {
        number: kw
            ? (watts / 1000).toLocaleString(i18nLocale(), { minimumFractionDigits: 1, maximumFractionDigits: 1 })
            : String(Math.round(watts)),
        unit: t(kw ? 'units.kw' : 'units.w')
    };
}

// Battery power for the banner. The number gets its own span so fullscreen mode
// can make it stand out.
function batteryPowerHtml(watts) {
    var p = powerParts(watts);
    return '<span class="battery-flow-power">' + escapeHtml(p.number) + '</span>\u00A0' + escapeHtml(p.unit);
}

// "Right now" strip: where the power comes from and where it goes (a bar and a
// list each), by the current readings. Smaller flows are noise (a few watts
// either way) and left out.
var FLOW_MIN_W = 20;

function renderPowerFlow(data) {
    var strip = document.getElementById('flow-strip');
    var battery = (!inverterConfig || inverterConfig.has_battery !== false) ? (data.battery_power || 0) : 0;
    var grid = data.grid_power || 0;
    var generator = (inverterConfig && inverterConfig.has_generator) ? (data.generator_power || 0) : 0;
    function flows(list) {
        return list.filter(function(f) { return f.watts >= FLOW_MIN_W; });
    }
    var sources = flows([
        { key: 'solar', watts: data.pv_total_power || 0 },
        { key: 'grid', watts: grid },            // importing
        { key: 'battery', watts: -battery },     // discharging
        { key: 'generator', watts: generator }
    ]);
    var uses = flows([
        { key: 'home', watts: data.load_power || 0 },
        { key: 'battery', watts: battery },      // charging
        { key: 'grid', watts: -grid }            // exporting
    ]);
    strip.hidden = !uses.length;
    if (!uses.length) return;

    // One row: label, bar (each flow's share), list with the values
    function row(direction, items) {
        if (!items.length) return '';
        var bar = items.map(function(f) {
            return '<span class="flow-segment flow-' + direction + '-' + f.key + '" style="flex-grow: ' + Math.round(f.watts) + '"></span>';
        }).join('');
        var list = items.map(function(f) {
            var p = powerParts(f.watts);
            return '<span class="flow-item"><span class="flow-dot flow-' + direction + '-' + f.key + '"></span>' +
                escapeHtml(t('flow.' + direction + '_' + f.key)) +
                ' <strong>' + escapeHtml(p.number) + '\u00A0' + escapeHtml(p.unit) + '</strong></span>';
        }).join('');
        return '<span class="flow-label">' + escapeHtml(t('flow.' + direction)) + '</span>' +
            '<div class="flow-bar">' + bar + '</div><div class="flow-list">' + list + '</div>';
    }
    document.getElementById('flow-rows').innerHTML = row('from', sources) + row('to', uses);
}

// Grid power actually in use: the server decides from the inverter's grid relay
// (falls back to voltage for older data)
function gridIsOnline(data) {
    if (typeof data.grid_online === 'boolean') return data.grid_online;
    return (data.grid_voltage || 0) > 100;
}

function updateBatteryBanner(data) {
    const banner = document.getElementById('battery-banner');
    const valueEl = document.getElementById('battery-banner-value');
    const fillEl = document.getElementById('battery-banner-fill');
    const statusEl = document.getElementById('battery-banner-status');

    const soc = data.battery_soc || 0;
    const current = data.battery_current || 0;
    const power = Math.abs(data.battery_power || 0);

    // Update value and fill
    valueEl.textContent = soc + '%';
    fillEl.style.width = soc + '%';

    // Update status text
    var flow = current > 0.5 ? 'charging' : current < -0.5 ? 'discharging' : null;
    statusEl.innerHTML = flow
        ? t('battery_banner.' + flow, { power: batteryPowerHtml(power) })
        : escapeHtml(t('battery_banner.idle'));

    // Time estimate in the header (server-side, from the recent average power):
    // until full while charging; until the shutdown SOC while discharging in an outage
    var minutes = null, labelKey = null;
    if (flow === 'charging' && typeof data.battery_charge_minutes === 'number') {
        minutes = data.battery_charge_minutes;
        labelKey = 'battery_banner.charge_label';
    } else if (flow === 'discharging' && !gridIsOnline(data) &&
               typeof data.battery_runtime_minutes === 'number') {
        minutes = data.battery_runtime_minutes;
        labelKey = 'battery_banner.runtime_label';
    }
    var estimateEl = document.getElementById('battery-banner-runtime');
    estimateEl.hidden = minutes === null;
    if (minutes !== null) {
        setI18nText(estimateEl.querySelector('.battery-banner-label'), labelKey);
        document.getElementById('battery-runtime-value').textContent =
            t('battery_banner.runtime', { time: formatPlannedDuration(minutes) });
    }
    statusEl.classList.toggle('battery-flow-charging', flow === 'charging');
    statusEl.classList.toggle('battery-flow-discharging', flow === 'discharging');

    // Update banner color class
    banner.classList.remove('battery-high', 'battery-medium', 'battery-low');
    if (soc >= 50) {
        banner.classList.add('battery-high');
    } else if (soc >= 20) {
        banner.classList.add('battery-medium');
    } else {
        banner.classList.add('battery-low');
    }
}

function handleGridStateChange(isOnline, voltage) {
    if (lastGridOnline === null) {
        // First load, just set the state
        lastGridOnline = isOnline;
        return;
    }

    if (lastGridOnline && !isOnline) {
        // Grid just went offline
        showNotification(t('notifications.outage_title'), t('notifications.outage_body'));
    } else if (!lastGridOnline && isOnline) {
        // Grid just came back online
        showNotification(t('notifications.restored_title'), t('notifications.restored_body'));
    }

    lastGridOnline = isOnline;
}

// Number for display; '--' when the inverter didn't report the value, so one
// missing field can't abort the rest of the dashboard update
function fmt(value, digits) {
    return (typeof value === 'number' && isFinite(value)) ? value.toFixed(digits) : '--';
}

// One /api/data request at a time: with a slow server, overlapping requests could
// resolve out of order and replay a stale grid state (false outage/restore alerts)
var dashboardRequestInFlight = false;

// Requests to the dashboard server fail now and then (tablet Wi-Fi, a busy NAS):
// keep the last data on screen and only report a problem once nothing has
// arrived for this long
var STALE_ERROR_MS = 60000;
// Battery power (W) within ±this counts as idle (card border; server: time estimates)
var BATTERY_IDLE_W = 20;
function noDataFor(lastOkAt) { return !lastOkAt || Date.now() - lastOkAt > STALE_ERROR_MS; }

var dashboardOkAt = 0;   // last answer with readings
var DASHBOARD_REQUEST_TIMEOUT_MS = 15000;

// Header status pill: how old the readings are ("Live · 12 s ago", ticking),
// amber "Updated 3 min ago" once they're older than a few polls (the server
// keeps the last good readings for up to 5 minutes). The exact time is in the
// pill's tooltip. statusMode "other": loading / an error text is shown instead.
var LIVE_MAX_AGE_S = 90;
var dataTakenAt = null;
var statusMode = 'other';

function formatAge(seconds) {
    if (seconds < 10) return t('status.age_now');
    if (seconds < 60) return t('status.age_s', { n: Math.floor(seconds) });
    if (seconds < 3600) return t('status.age_min', { n: Math.floor(seconds / 60) });
    return t('status.age_h', { n: Math.floor(seconds / 3600) });
}

// The status in the app bar's pill, and as the dot by the fullscreen date (its
// colour; the text goes to its tooltip and screen readers). title: details.
var STATUS_STATES = ['online', 'stale', 'loading', 'error'];

function showStatus(key, params, state, title) {
    var pill = document.getElementById('status');
    var dot = document.getElementById('fullscreen-status');
    if (pill) {
        setI18nText(pill, key, params);
        pill.title = title || '';
    }
    if (dot) {
        var text = t(key, params);
        dot.removeAttribute('data-i18n-attr');   // the text below replaces the static one
        dot.setAttribute('aria-label', text);
        dot.title = title ? text + ' · ' + title : text;
    }
    [pill, dot].forEach(function(el) {
        if (el) STATUS_STATES.forEach(function(s) { el.classList.toggle(s, s === state); });
    });
}

function renderLiveStatus() {
    if (statusMode !== 'live' || dataTakenAt === null) return;
    var age = Math.max(0, (Date.now() - dataTakenAt) / 1000);
    var live = age <= LIVE_MAX_AGE_S;
    showStatus(live ? 'status.live' : 'status.stale', { age: formatAge(age) }, live ? 'online' : 'stale',
               t('status.updated_at', { time: new Date(dataTakenAt).toLocaleTimeString(i18nLocale()) }));
}

setInterval(renderLiveStatus, 1000);

function updateDashboard() {
    if (dashboardRequestInFlight) return;
    dashboardRequestInFlight = true;
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timeoutId = controller ? setTimeout(function() { controller.abort(); }, DASHBOARD_REQUEST_TIMEOUT_MS) : null;
    fetch('/api/data', controller ? { signal: controller.signal } : {})
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                statusMode = 'other';
                if (data.error === 'not yet available') {
                    showStatus('status.pulling', null, 'loading', '');
                } else {
                    showStatus('common.error', { message: data.error }, 'error', '');
                }
                // Apply config even on error so UI elements stay visible
                if (data.config) {
                    applyInverterConfig(data.config);
                }
                return;
            }

            // "Updated" = when the readings were taken (the server keeps the last
            // good ones through a failed inverter poll)
            dashboardOkAt = Date.now();
            dataTakenAt = Date.now() - (data.data_age_seconds || 0) * 1000;
            statusMode = 'live';
            renderLiveStatus();

            // Apply config once when first received
            if (data.config) {
                applyInverterConfig(data.config);
            }

            latestInverterData = data;

            // Solar
            document.getElementById('pv-total').innerHTML = data.pv_total_power + '<span class="unit">' + t('units.w') + '</span>';
            document.getElementById('pv1-power').textContent = data.pv1_power;
            if (!inverterConfig || inverterConfig.pv_strings >= 2) {
                document.getElementById('pv2-power').textContent = data.pv2_power;
            }
            document.getElementById('daily-pv').textContent = fmt(data.daily_pv, 1);

            // Battery
            if (!inverterConfig || inverterConfig.has_battery !== false) {
                document.getElementById('battery-soc').textContent = data.battery_soc + '%';
                document.getElementById('battery-fill').style.width = data.battery_soc + '%';
                document.getElementById('battery-voltage').textContent = fmt(data.battery_voltage, 1);
                document.getElementById('battery-current').textContent = fmt(data.battery_current, 1);
                // LiFePO4: below 5 °C charging slows (below 0 °C it harms the cells), above 45 °C too hot
                var tempEl = document.getElementById('battery-temperature');
                var temp = data.battery_temperature;
                tempEl.textContent = fmt(temp, 1);
                tempEl.classList.toggle('battery-temp-cold', typeof temp === 'number' && temp < 5);
                tempEl.classList.toggle('battery-temp-hot', typeof temp === 'number' && temp > 45);
                document.getElementById('battery-power').textContent = Math.abs(data.battery_power);
                setI18nText(document.getElementById('battery-status'), 'battery.status_power', { status: batteryStatusText(data.battery_status) });
            }

            // Grid
            const gridPowerEl = document.getElementById('grid-power');
            gridPowerEl.innerHTML = Math.abs(data.grid_power) + '<span class="unit">' + t('units.w') + '</span>';
            gridPowerEl.className = 'metric-value ' + (data.grid_power > 0 ? 'negative' : data.grid_power < 0 ? 'positive' : 'neutral');
            // The number has no sign: the label says which way the power flows
            setI18nText(document.getElementById('grid-power-label'),
                data.grid_power > 0 ? 'grid.import_now' : data.grid_power < 0 ? 'grid.export_now' : 'common.power_now');
            document.getElementById('grid-voltage').textContent = fmt(data.grid_voltage, 1);
            // 3-phase: one value per phase instead of L1 only
            var gridPhases = data.grid_voltage_l2 != null;
            document.getElementById('grid-voltage-single').hidden = gridPhases;
            document.getElementById('grid-voltage-phases').hidden = !gridPhases;
            if (gridPhases) {
                ['l1', 'l2', 'l3'].forEach(function(p) {
                    var el = document.getElementById('grid-voltage-' + p);
                    el.textContent = fmt(data['grid_voltage_' + p], 0);
                    el.classList.toggle('grid-phase-down', (data['grid_voltage_' + p] || 0) < 100);
                });
            }
            document.getElementById('daily-import').textContent = fmt(data.daily_grid_import, 1);
            document.getElementById('daily-export').textContent = fmt(data.daily_grid_export, 1);

            // Load
            document.getElementById('load-power').innerHTML = data.load_power + '<span class="unit">' + t('units.w') + '</span>';
            document.getElementById('daily-load').textContent = fmt(data.daily_load, 1);

            renderPowerFlow(data);

            // ===== CARD STATES (border + glow, see .card.* in the CSS) =====

            // Solar: producing
            const solarCard = document.getElementById('solar-card');
            if (data.pv_total_power > 10) {
                solarCard.classList.add('solar-active');
            } else {
                solarCard.classList.remove('solar-active');
            }

            // Battery: charging / discharging, by power like the card's text (a
            // few watts either way count as idle, as for the time estimates)
            if (!inverterConfig || inverterConfig.has_battery !== false) {
                const batteryCard = document.getElementById('battery-card');
                const batteryPower = data.battery_power || 0;
                batteryCard.classList.toggle('battery-charging', batteryPower > BATTERY_IDLE_W);
                batteryCard.classList.toggle('battery-discharging', batteryPower < -BATTERY_IDLE_W);
            }

            // Grid: importing / exporting
            const gridCard = document.getElementById('grid-card');
            if (Math.abs(data.grid_power) > 50) {
                gridCard.classList.add('grid-active');
            } else {
                gridCard.classList.remove('grid-active');
            }

            // Consumption: using power (neutral) / high load (red)
            const loadCard = document.getElementById('consumption-card');
            loadCard.classList.remove('load-active', 'load-high');
            if (data.load_power > 3000) {
                loadCard.classList.add('load-high');
            } else if (data.load_power > 100) {
                loadCard.classList.add('load-active');
            }

            renderHealth(data);

            // Grid Status Banner
            const gridBanner = document.getElementById('grid-banner');
            const gridStatusValue = document.getElementById('grid-status-value');
            const gridStatusLabel = document.getElementById('grid-status-label');
            const isOnline = gridIsOnline(data);

            // On battery either way, but say why: some phases missing, grid out of
            // range (all phases there) or no grid at all
            var gridState = isOnline ? 'online' : (data.grid_state || 'outage');
            gridBanner.classList.toggle('online', isOnline);
            gridBanner.classList.toggle('offline', !isOnline);
            gridBanner.classList.toggle('partial', gridState === 'partial' || gridState === 'out_of_range');
            if (isOnline) {
                setI18nText(gridStatusValue, 'grid_banner.online');
                setI18nText(gridStatusLabel, 'grid_banner.label');
            } else if (gridState === 'partial') {
                var missing = (data.grid_phases_missing || []).map(function(n) { return 'L' + n; });
                setI18nText(gridStatusValue, missing.length > 1 ? 'grid_banner.phases_down' : 'grid_banner.phase_down',
                    { phases: missing.join(', ') });
                setI18nText(gridStatusLabel, 'grid_banner.partial_label');
            } else if (gridState === 'out_of_range') {
                setI18nText(gridStatusValue, 'grid_banner.on_battery');
                setI18nText(gridStatusLabel, 'grid_banner.out_of_range_label');
            } else {
                setI18nText(gridStatusValue, 'grid_banner.outage');
                setI18nText(gridStatusLabel, 'grid_banner.no_power');
            }

            // Handle grid state changes (notification)
            handleGridStateChange(isOnline, data.grid_voltage);

            // Update phase display (3-phase only)
            if (!inverterConfig || inverterConfig.phases === 3) {
                updatePhaseDisplay(data);
            }

            // Update battery banner (prominent display)
            if (!inverterConfig || inverterConfig.has_battery !== false) {
                updateBatteryBanner(data);
            }

            // Update generator power from real-time inverter data
            if (inverterConfig && inverterConfig.has_generator) {
                var genPower = data.generator_power || 0;
                var genPowerEl = document.getElementById('generator-power');
                if (genPowerEl) genPowerEl.innerHTML = genPower + '<span class="unit">' + t('units.w') + '</span>';
                // Update banner with real-time power
                if (generatorData) {
                    generatorData.power = genPower;
                    generatorData.running = genPower > 0;
                }
                updateGeneratorBanner(generatorData || {power: genPower, today_runtime_hours: 0});
            }

            // Red tint when the grid is down and the battery is low
            updateBatteryOverlay(data);

        })
        .catch(error => {
            console.error('Error:', error);
            // A failed request: the last readings and their time stay until
            // nothing has arrived for a while
            if (!noDataFor(dashboardOkAt)) return;
            statusMode = 'other';
            showStatus('status.connection_failed', null, 'error', '');
        })
        .finally(function() {
            if (timeoutId) clearTimeout(timeoutId);
            dashboardRequestInFlight = false;
        });
}

// Initial load
loadPhaseStats();
updateDashboard();

// Weather forecast banner (independent of inverter data; polled from its section below)
updateClock();

// Update every 5 seconds
setInterval(updateDashboard, 5000);

// Refresh phase stats every 30 seconds
setInterval(loadPhaseStats, 30000);

// ===== SPARKLINES =====
// Battery charge, solar and load over the last 24 hours: 5-minute averages
// from /api/history (history.py), drawn from the first reading to the latest
// (with less than a day of history the caption says since when). A gap in the
// data (dashboard off, no reading) breaks the line.
var HISTORY_POLL_MS = 5 * 60 * 1000;
var SPARK_W = 300, SPARK_H = 48;   // the SVG's viewBox
var historyData = null;

function loadHistory() {
    fetch('/api/history')
        .then(function(response) { return response.json(); })
        .then(function(data) {
            historyData = data;
            renderSparklines();
            renderHistoryDetails();
        })
        .catch(function(error) { console.error('History fetch error:', error); });
}

function renderSparklines() {
    document.querySelectorAll('.sparkline[data-series]').forEach(function(el) {
        renderSparkline(el, historyData);
    });
}

// The series' readings as points (bucket middle, but not past "now" for the
// open bucket; value); buckets without one are skipped
function historyPoints(h, name) {
    var points = [];
    ((h && h[name]) || []).forEach(function(v, i) {
        if (typeof v === 'number') points.push({ t: Math.min(h.t[i] + h.bucket_seconds / 2, h.now), v: v });
    });
    return points;
}

// The time axis: first to latest point. Less than a (nearly) full day of
// history: the caption el says since when, else "Last 24 hours".
function historyRange(points, captionEl) {
    var start = points[0].t, end = points[points.length - 1].t;
    var span = Math.max(1, end - start);
    if (span < 23 * 3600) setI18nText(captionEl, 'history.since', { time: clockText(start) });
    else setI18nText(captionEl, 'history.last_24h');
    return { start: start, end: end, span: span };
}

// Line and area paths, one run per stretch of consecutive buckets (a missing
// bucket starts a new run). x(p), y(p) map a point; base is the area's bottom.
function historyPaths(points, bucketSeconds, x, y, base) {
    var runs = [], run = [];
    points.forEach(function(p, i) {
        if (i && p.t - points[i - 1].t > bucketSeconds * 1.5) { runs.push(run); run = []; }
        run.push(p);
    });
    runs.push(run);
    var line = '', area = '';
    runs.forEach(function(r) {
        var path = r.map(function(p, i) { return (i ? 'L' : 'M') + x(p).toFixed(1) + ' ' + y(p).toFixed(1); }).join(' ');
        line += path + ' ';
        area += path + ' L' + x(r[r.length - 1]).toFixed(1) + ' ' + base + ' L' + x(r[0]).toFixed(1) + ' ' + base + ' Z ';
    });
    return { line: line.trim(), area: area.trim() };
}

function historyValueText(name, v) {
    if (name === 'soc') return Math.round(v) + '%';
    var p = powerParts(v);
    return p.number + ' ' + p.unit;
}

function renderSparkline(el, h) {
    var name = el.getAttribute('data-series');
    var points = historyPoints(h, name);
    el.hidden = points.length < 2;
    if (el.hidden) return;

    var range = historyRange(points, el.querySelector('.spark-period'));
    var vs = points.map(function(p) { return p.v; });
    var max = name === 'soc' ? 100 : Math.max(100, Math.max.apply(null, vs));
    var top = 3, bottom = SPARK_H - 1;
    var paths = historyPaths(points, h.bucket_seconds,
        function(p) { return (p.t - range.start) / range.span * SPARK_W; },
        function(p) { return bottom - Math.max(0, p.v) / max * (bottom - top); },
        SPARK_H);
    el.querySelector('.spark-line').setAttribute('d', paths.line);
    el.querySelector('.spark-area').setAttribute('d', paths.area);

    el.querySelector('.spark-extra').textContent = name === 'soc'
        ? t('history.range', { min: Math.round(Math.min.apply(null, vs)), max: Math.round(Math.max.apply(null, vs)) })
        : t('history.peak', { value: historyValueText(name, Math.max.apply(null, vs)) });
}

// ===== 24-HOUR DETAILS =====
// Tapping a sparkline opens its 24 hours as a larger chart: a time axis, the
// value at the touched point, and now / peak / total (charge: now / lowest /
// highest). Closes with ×, Escape, a tap outside, or the fullscreen return to
// the first screen.
var DETAILS_W = 600, DETAILS_H = 220;
var DETAILS_LEFT = 52, DETAILS_RIGHT = 10, DETAILS_TOP = 12, DETAILS_BOTTOM = 26;
var detailsSpark = null;   // the sparkline whose details are open
var detailsAt = null;      // time of the touched point (null: the latest)
var detailsRange = null;   // the chart's time axis (historyRange)

function openHistoryDetails(sparkEl) {
    detailsSpark = sparkEl;
    detailsAt = null;
    var card = sparkEl.closest('.card');
    var chip = card && card.querySelector('.card-header .icon-chip');
    var icon = document.getElementById('details-icon');
    icon.className = chip ? chip.className.replace(/\bicon\b/, '').trim() : 'icon-chip';
    icon.innerHTML = chip ? chip.innerHTML : '';
    document.getElementById('details-overlay').hidden = false;
    renderHistoryDetails();
    document.getElementById('details-dialog').focus();
}

function closeHistoryDetails() {
    if (!detailsSpark) return;
    var spark = detailsSpark;
    detailsSpark = null;
    document.getElementById('details-overlay').hidden = true;
    if (document.activeElement && document.activeElement.closest('#details-overlay')) spark.focus();
}

// A round axis top: 3300 -> 4000, 870 -> 1000, 2100 -> 2500
function niceCeil(value) {
    var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
        if (value <= steps[i] * magnitude) return steps[i] * magnitude;
    }
    return 10 * magnitude;
}

function clockText(seconds) {
    var d = new Date(seconds * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function renderHistoryDetails() {
    if (!detailsSpark) return;
    var name = detailsSpark.getAttribute('data-series');
    var h = historyData;
    var points = historyPoints(h, name);
    var card = detailsSpark.closest('.card');
    document.getElementById('details-title').textContent = card ? cardTitle(card) : '';
    var chart = document.getElementById('details-chart');
    chart.setAttribute('class', 'details-chart ' + Array.prototype.filter.call(detailsSpark.classList, function(c) {
        return c.indexOf('spark-') === 0;
    }).join(' '));
    if (!points.length) {
        chart.innerHTML = '';
        document.getElementById('details-readout').textContent = '';
        document.getElementById('details-stats').innerHTML = '';
        return;
    }

    var range = detailsRange = historyRange(points, document.getElementById('details-period'));
    var vs = points.map(function(p) { return p.v; });
    var max = name === 'soc' ? 100 : niceCeil(Math.max(100, Math.max.apply(null, vs)));
    var left = DETAILS_LEFT, right = DETAILS_W - DETAILS_RIGHT, top = DETAILS_TOP, bottom = DETAILS_H - DETAILS_BOTTOM;
    function x(p) { return left + (p.t - range.start) / range.span * (right - left); }
    function y(p) { return bottom - Math.max(0, p.v) / max * (bottom - top); }

    var svg = '';
    // Value grid: 0, half, top
    [0, 0.5, 1].forEach(function(f) {
        var gy = (bottom - f * (bottom - top)).toFixed(1);
        svg += '<line class="details-grid" x1="' + left + '" x2="' + right + '" y1="' + gy + '" y2="' + gy + '"/>' +
            '<text class="details-label" x="' + (left - 8) + '" y="' + gy + '" text-anchor="end" dominant-baseline="middle">' +
            escapeHtml(historyValueText(name, f * max)) + '</text>';
    });
    // Time axis on whole hours of the clock: every 6 (00, 06, 12, 18), or every
    // 3 / every hour for a shorter history
    var tickHours = range.span <= 4 * 3600 ? 1 : range.span <= 12 * 3600 ? 3 : 6;
    for (var tick = Math.ceil(range.start / 3600) * 3600; tick <= range.end; tick += 3600) {
        if (new Date(tick * 1000).getHours() % tickHours) continue;
        var tx = x({ t: tick }).toFixed(1);
        svg += '<line class="details-grid details-grid-time" x1="' + tx + '" x2="' + tx + '" y1="' + top + '" y2="' + bottom + '"/>' +
            '<text class="details-label" x="' + tx + '" y="' + (DETAILS_H - 6) + '" text-anchor="middle">' + clockText(tick) + '</text>';
    }
    var paths = historyPaths(points, h.bucket_seconds, x, y, bottom);
    svg += '<path class="spark-area" d="' + paths.area + '"/><path class="spark-line" d="' + paths.line + '"/>';

    // The touched point (or the latest)
    var at = points[points.length - 1];
    if (detailsAt !== null) {
        points.forEach(function(p) { if (Math.abs(p.t - detailsAt) < Math.abs(at.t - detailsAt)) at = p; });
    }
    var ax = x(at).toFixed(1), ay = y(at).toFixed(1);
    svg += '<line class="details-marker" x1="' + ax + '" x2="' + ax + '" y1="' + top + '" y2="' + bottom + '"/>' +
        '<circle class="details-dot" cx="' + ax + '" cy="' + ay + '" r="4"/>';
    chart.innerHTML = svg;
    document.getElementById('details-readout').innerHTML =
        '<span>' + escapeHtml(detailsAt === null ? t('history.now') : clockText(at.t - h.bucket_seconds / 2)) + '</span>' +
        '<strong>' + escapeHtml(historyValueText(name, at.v)) + '</strong>';

    var latest = points[points.length - 1].v;
    var stats;
    if (name === 'soc') {
        stats = [['history.now', historyValueText(name, latest)],
                 ['history.lowest', historyValueText(name, Math.min.apply(null, vs))],
                 ['history.highest', historyValueText(name, Math.max.apply(null, vs))]];
    } else {
        var kwh = vs.reduce(function(sum, v) { return sum + v; }, 0) * h.bucket_seconds / 3600 / 1000;
        stats = [['history.now', historyValueText(name, latest)],
                 ['history.peak_label', historyValueText(name, Math.max.apply(null, vs))],
                 ['history.total', fmt(kwh, 1) + ' ' + t('units.kwh')]];
    }
    document.getElementById('details-stats').innerHTML = stats.map(function(s) {
        return '<div class="details-stat"><span>' + escapeHtml(t(s[0])) + '</span><strong>' + escapeHtml(s[1]) + '</strong></div>';
    }).join('');
}

(function() {
    var overlay = document.getElementById('details-overlay');
    var chart = document.getElementById('details-chart');
    if (!overlay || !chart) return;
    document.addEventListener('click', function(e) {
        var spark = e.target.closest('.sparkline[data-series]');
        if (spark && !spark.hidden) openHistoryDetails(spark);
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && detailsSpark) { closeHistoryDetails(); return; }
        var spark = e.target.closest && e.target.closest('.sparkline[data-series]');
        if (spark && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openHistoryDetails(spark); }
    });
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeHistoryDetails();
    });
    // Touch or hover the chart: the value at that time
    function touched(e) {
        if (!detailsSpark || !detailsRange) return;
        var rect = chart.getBoundingClientRect();
        var vx = (e.clientX - rect.left) / rect.width * DETAILS_W;
        var f = (vx - DETAILS_LEFT) / (DETAILS_W - DETAILS_RIGHT - DETAILS_LEFT);
        detailsAt = detailsRange.start + Math.min(1, Math.max(0, f)) * detailsRange.span;
        renderHistoryDetails();
    }
    chart.addEventListener('pointerdown', touched);
    chart.addEventListener('pointermove', touched);
    chart.addEventListener('pointerleave', function() {
        detailsAt = null;
        renderHistoryDetails();
    });
})();

loadHistory();
setInterval(loadHistory, HISTORY_POLL_MS);

// ===== OUTAGE SCHEDULE =====
var latestInverterData = null;
var outageScheduleData = null;

// Generator data
var generatorData = null;
var generatorIntervalId = null;

function fetchOutageSchedule() {
    apiFetch('/api/outage_schedule')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            // "unknown" = the server has no current schedule yet (e.g. just after
            // midnight, before its next fetch): keep the one on screen meanwhile
            if (data.status === 'unknown' && outageScheduleData && outageScheduleData.days) return;
            outageScheduleData = data;
            updateOutageInfo();
            renderPlannedOutages();
        })
        // A failed request (Wi-Fi blip, busy server) keeps the last schedule on
        // screen; "Loading" only shows before the first answer
        .catch(function() { if (!outageScheduleData) renderPlannedOutages(); });
}

// ===== PLANNED OUTAGES CARD =====

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Window times are local ISO strings ("2026-10-17T08:00:00"); an end at
// next-day midnight is shown as 24:00 on the window's own day.
function plannedMinutes(iso, dayIso) {
    var mins = parseInt(iso.slice(11, 13), 10) * 60 + parseInt(iso.slice(14, 16), 10);
    return iso.slice(0, 10) === dayIso ? mins : 24 * 60;
}

function formatPlannedMinutes(mins) {
    var h = Math.floor(mins / 60), m = mins % 60;
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}

function formatPlannedDuration(mins) {
    var h = Math.floor(mins / 60), m = mins % 60;
    if (h && m) return t('duration.hm', { h: h, m: m });
    return h ? t('duration.h', { h: h }) : t('duration.m', { m: m });
}

function renderPlannedDay(label, day, now) {
    var dateObj = new Date(day.date + 'T00:00:00');
    var dateText = dateObj.toLocaleDateString(i18nLocale(), { weekday: 'short', day: 'numeric', month: 'short' });
    var isToday = dateObj.toDateString() === now.toDateString();
    var nowMins = now.getHours() * 60 + now.getMinutes();

    var windows = (day.windows || []).map(function(w) {
        var start = plannedMinutes(w.start, day.date);
        var end = plannedMinutes(w.end, day.date);
        var state = 'upcoming';
        if (isToday && end <= nowMins) state = 'past';
        else if (isToday && start <= nowMins) state = 'active';
        return { start: start, end: end, state: state, endDate: new Date(w.end), startDate: new Date(w.start) };
    });
    var totalMins = windows.reduce(function(sum, w) { return sum + (w.end - w.start); }, 0);

    var html = '<div class="planned-day">';
    html += '<div class="planned-day-header"><span class="planned-day-name">' + label + '</span>';
    html += '<span class="planned-day-date">' + escapeHtml(dateText) + '</span>';
    if (totalMins > 0) html += '<span class="planned-day-total">' + t('planned.without_power', { time: formatPlannedDuration(totalMins) }) + '</span>';
    html += '</div>';

    html += '<div class="planned-timeline">';
    windows.forEach(function(w) {
        html += '<div class="planned-timeline-segment' + (w.state === 'past' ? ' past' : '') + '" style="left:' +
            (w.start / 14.4) + '%;width:' + ((w.end - w.start) / 14.4) + '%"></div>';
    });
    if (isToday) html += '<div class="planned-timeline-now" style="left:' + (nowMins / 14.4) + '%"></div>';
    html += '</div>';
    html += '<div class="planned-timeline-scale"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>';

    if (windows.length === 0) {
        html += '<div class="planned-none clear">' + t('planned.none') + '</div>';
    } else {
        html += '<ul class="planned-windows">';
        windows.forEach(function(w) {
            var tag = '';
            if (w.state === 'active') tag = t('planned.now_ends_in', { time: formatCountdown(Math.max(0, w.endDate - now)) });
            else if (w.state === 'past') tag = t('planned.done');
            else if (isToday) tag = t('planned.starts_in', { time: formatCountdown(Math.max(0, w.startDate - now)) });
            html += '<li class="planned-window ' + w.state + '">' +
                '<span class="planned-window-time">' + formatPlannedMinutes(w.start) + '–' + formatPlannedMinutes(w.end) + '</span>' +
                '<span class="planned-window-duration">' + formatPlannedDuration(w.end - w.start) + '</span>' +
                '<span class="planned-window-tag">' + tag + '</span></li>';
        });
        html += '</ul>';
    }
    return html + '</div>';
}

function renderPlannedOutages() {
    var d = outageScheduleData;
    // Same content in the dashboard card and the fullscreen banner
    var targets = [
        { container: document.getElementById('planned-outages-card'),
          body: document.getElementById('planned-outages-body'),
          source: document.getElementById('planned-outages-source') },
        { container: document.getElementById('planned-outages-banner'),
          body: document.getElementById('planned-outages-banner-body'),
          source: document.getElementById('planned-outages-banner-source') },
    ].filter(function(t) { return t.container && t.body; });

    if (d && d.status === 'disabled') {
        targets.forEach(function(t) { t.container.style.display = 'none'; });
        document.body.classList.add('outages-disabled');
        return;
    }

    var html;
    if (!d || d.status === 'unknown') {
        html = '<div class="planned-none">' + t('planned.loading') + '</div>';
    } else if (d.status === 'error') {
        html = '<div class="planned-none">' + t('planned.unavailable') + '</div>' +
            '<div class="planned-error-detail">' + escapeHtml(d.error || '') + '</div>';
    } else {
        var now = new Date();
        var days = d.days || [];
        html = '';
        if (days[0]) html += renderPlannedDay(t('planned.today'), days[0], now);
        if (days[1]) {
            html += renderPlannedDay(t('planned.tomorrow'), days[1], now);
        } else {
            html += '<div class="planned-day"><div class="planned-day-header"><span class="planned-day-name">' + t('planned.tomorrow') + '</span></div>' +
                '<div class="planned-none">' + t('planned.not_published') + '</div></div>';
        }
    }

    // Provider name highlighted like the NEPTUN link in the alerts card (escaped: it comes from the server)
    var sourceHtml = (d && d.provider)
        ? t('planned.source', { provider: '<span class="source-name">' + escapeHtml(d.provider) + '</span>' })
        : '';
    targets.forEach(function(t) {
        t.body.innerHTML = html;
        if (t.source) t.source.innerHTML = sourceHtml;
    });
}

// Countdown time in the grid banner, highlighted (escaped: it's built from translations)
function outageTimeHtml(ms) {
    return '<span class="outage-time">' + escapeHtml(formatCountdown(ms)) + '</span>';
}

function updateOutageInfo() {
    var el = document.getElementById('grid-outage-info');
    var d = outageScheduleData;
    if (!el || !d) { if (el) el.textContent = ''; return; }
    el.title = d.status === 'error' ? (d.error || '') : '';
    var now = new Date();
    var gridBanner = document.getElementById('grid-banner');
    var gridOnline = gridBanner.classList.contains('online');
    var gridOffline = gridBanner.classList.contains('offline');

    if (!gridOnline && !gridOffline) {
        // Grid state unknown (no inverter data yet): report the schedule
        // itself rather than assuming an outage.
        if (d.status === 'active' && d.end_time) {
            var activeLeft = new Date(d.end_time) - now;
            el.innerHTML = activeLeft > 0 ? t('outage_info.scheduled_now', { time: outageTimeHtml(activeLeft) }) : '';
        } else if (d.status === 'upcoming' && d.upcoming_windows && d.upcoming_windows.length > 0) {
            var untilStart = new Date(d.upcoming_windows[0].start) - now;
            el.innerHTML = untilStart > 0 ? t('outage_info.outage_in', { time: outageTimeHtml(untilStart) }) : '';
        } else if (d.status === 'clear') {
            el.textContent = t('outage_info.no_outages_today');
        } else if (d.status === 'error') {
            el.textContent = t('outage_info.unavailable');
        } else {
            el.textContent = '';
        }
    } else if (gridOffline) {
        // Grid is off — show when it comes back
        if (d.status === 'active' && d.end_time) {
            var end = new Date(d.end_time);
            var diff = end - now;
            if (diff > 0) {
                el.innerHTML = t('outage_info.back_in', { time: outageTimeHtml(diff) });
            } else {
                el.textContent = t('outage_info.back_soon');
            }
        } else {
            // Unscheduled outage (battery level and runtime left are in the battery banner)
            el.textContent = d.status === 'error' ? t('outage_info.outage_unavailable') : t('outage_info.unscheduled');
        }
    } else {
        // Grid is on — show next outage countdown
        if (d.status === 'upcoming' && d.upcoming_windows && d.upcoming_windows.length > 0) {
            var start = new Date(d.upcoming_windows[0].start);
            var diff = start - now;
            if (diff > 0) {
                el.innerHTML = t('outage_info.outage_in', { time: outageTimeHtml(diff) });
            } else {
                el.textContent = '';
            }
        } else if (d.status === 'clear') {
            el.textContent = t('outage_info.no_outages_today');
        } else if (d.status === 'error') {
            el.textContent = t('outage_info.unavailable');
        } else {
            el.textContent = '';
        }
    }
}

function formatCountdown(ms) {
    var totalSec = Math.floor(ms / 1000);
    var h = Math.floor(totalSec / 3600);
    var m = Math.floor((totalSec % 3600) / 60);
    var s = totalSec % 60;
    if (h > 0) return t('duration.hm', { h: h, m: m });
    if (m > 0) return t('duration.ms', { m: m, s: s });
    return t('duration.s', { s: s });
}

fetchOutageSchedule();
outageScheduleIntervalId = setInterval(fetchOutageSchedule, 60000);
outageInfoIntervalId = setInterval(updateOutageInfo, 1000);
// Keep the card's "now" marker and countdowns current between fetches
setInterval(renderPlannedOutages, 30000);

// ===== GRID SCHEDULE PROGRESS BAR =====

function updateGridProgressBar() {
    var bar = document.getElementById('grid-banner-bar');
    var fill = document.getElementById('grid-banner-bar-fill');
    var d = outageScheduleData;
    var gridBanner = document.getElementById('grid-banner');
    var gridOffline = gridBanner && gridBanner.classList.contains('offline');
    var gridUnknown = gridBanner && !gridOffline && !gridBanner.classList.contains('online');

    if (!d || (d.status !== 'active' && d.status !== 'upcoming')) {
        bar.style.display = 'none';
        return;
    }

    var now = new Date();
    var progress = 0;
    var showBar = false;

    if (gridOffline || (gridUnknown && d.status === 'active')) {
        // Red bar: outage progress (grid offline, or grid state unknown during a scheduled outage)
        showBar = true;
        fill.className = 'grid-banner-bar-fill red';
        if (d.status === 'active' && d.start_time && d.end_time) {
            var start = new Date(d.start_time);
            var end = new Date(d.end_time);
            var total = end - start;
            var elapsed = now - start;
            progress = Math.min(100, Math.max(0, (elapsed / total) * 100));
            if (end - now <= 0) progress = 100;
        }
    } else if (d.status === 'upcoming' && d.upcoming_windows && d.upcoming_windows.length > 0) {
        // Green bar: electricity time consumed
        showBar = true;
        fill.className = 'grid-banner-bar-fill green';
        var outageStart = new Date(d.upcoming_windows[0].start);
        if (d.electricity_start) {
            var elecStart = new Date(d.electricity_start);
            var total = outageStart - elecStart;
            var elapsed = now - elecStart;
            if (total > 0) {
                progress = Math.min(100, Math.max(0, (elapsed / total) * 100));
            }
        }
    }

    if (!showBar) {
        bar.style.display = 'none';
        return;
    }
    bar.style.display = 'block';
    var remaining = Math.max(0, 100 - progress);
    fill.style.width = remaining + '%';
}

gridProgressBarIntervalId = setInterval(updateGridProgressBar, 1000);

// ===== WEATHER FORECAST BANNER =====

var cachedWeatherData = null;

// WMO weather code -> icon; descriptions are in locales (weather.codes.<code>).
// Only emoji from the original (Unicode 6) set: older tablets used as kiosks
// (Android 5.x) show newer ones such as 🌧️ 🌦️ 🌫️ as empty boxes.
var WMO_ICONS = {
    0: '☀️',
    1: '☀️',
    2: '⛅',
    3: '☁️',
    45: '🌁',
    48: '🌁',
    51: '💧',
    53: '💧',
    55: '💧',
    56: '💧',
    57: '💧',
    61: '☔',
    63: '☔',
    65: '☔',
    66: '☔',
    67: '☔',
    71: '❄️',
    73: '❄️',
    75: '❄️',
    77: '❄️',
    80: '☔',
    81: '☔',
    82: '☔',
    85: '❄️',
    86: '❄️',
    95: '⚡',
    96: '⚡',
    99: '⚡'
};

function getWeatherDescription(code) {
    if (WMO_ICONS[code]) return { text: t('weather.codes.' + code), icon: WMO_ICONS[code] };
    return { text: t('weather.unknown'), icon: '❔' };
}

function formatTimeFromISO(isoStr) {
    if (!isoStr) return '--';
    var timePart = isoStr.indexOf('T') >= 0 ? isoStr.split('T')[1] : isoStr;
    return timePart.substring(0, 5);
}

function updateWeatherForecastBanner(data) {
    var desc = getWeatherDescription(data.weather_code);
    document.getElementById('weather-forecast-icon').textContent = desc.icon;
    document.getElementById('weather-forecast-temp').textContent =
        data.temperature != null ? Math.round(data.temperature) + '°C' : '--';
    document.getElementById('weather-forecast-desc').textContent = desc.text;
    document.getElementById('weather-forecast-high').textContent =
        data.temp_max != null ? Math.round(data.temp_max) + '°' : '--';
    document.getElementById('weather-forecast-low').textContent =
        data.temp_min != null ? Math.round(data.temp_min) + '°' : '--';
    document.getElementById('weather-forecast-sunrise').textContent =
        formatTimeFromISO(data.sunrise);
    document.getElementById('weather-forecast-sunset').textContent =
        formatTimeFromISO(data.sunset);

    var tomorrow = data.tomorrow;
    document.getElementById('weather-forecast-tomorrow').hidden = !tomorrow;
    if (tomorrow) {
        var tDesc = getWeatherDescription(tomorrow.weather_code);
        var deg = function(v) { return v != null ? Math.round(v) + '°' : '--'; };
        document.getElementById('weather-tomorrow-icon').textContent = tDesc.icon;
        document.getElementById('weather-tomorrow-temps').textContent =
            deg(tomorrow.temp_max) + ' / ' + deg(tomorrow.temp_min);
        var text = tDesc.text;
        if (tomorrow.precipitation >= 0.1) {
            text += ' · ' + t('weather.precipitation', { value: tomorrow.precipitation.toFixed(1) });
        }
        document.getElementById('weather-tomorrow-desc').textContent = text;
    }
}

function updateBatteryOverlay(data) {
    var overlay = document.getElementById('battery-overlay');

    // Skip battery overlay when no battery is configured
    if (inverterConfig && inverterConfig.has_battery === false) {
        overlay.style.background = 'rgba(var(--alarm-rgb), 0)';
        return;
    }

    var soc = data.battery_soc || 0;

    if (!gridIsOnline(data) && soc < 50) {
        // More aggressive: 0.1 at SOC 45%, up to 0.6 at SOC 0%
        var opacity = Math.min(((50 - soc) / 50) * 0.5 + 0.1, 0.6);
        overlay.style.background = 'rgba(var(--alarm-rgb), ' + opacity.toFixed(3) + ')';
    } else {
        overlay.style.background = 'rgba(var(--alarm-rgb), 0)';
    }
}

// Weather for the forecast banner (fullscreen); the server refreshes it every 15 min
var WEATHER_POLL_MS = 60000;

function fetchWeather() {
    apiFetch('/api/weather')
        .then(function(response) { return response.json(); })
        .then(function(weatherData) {
            if (weatherData.error) return;
            cachedWeatherData = weatherData;
            updateWeatherForecastBanner(weatherData);
        })
        .catch(function(err) {
            console.error('Weather fetch error:', err);
        });
}

// Started here, after WEATHER_POLL_MS is set: called earlier, the delay was
// still undefined and the browser polled as fast as it could (~250 requests/s)
fetchWeather();
setInterval(fetchWeather, WEATHER_POLL_MS);

// Clock in the forecast banner: the device's local time, 24-hour (HH:MM)
var clockTimer;  // no "= null": updateClock() already runs at startup, before this line

function updateClock() {
    var el = document.getElementById('weather-forecast-clock');
    if (!el) return;
    var now = new Date();
    el.textContent = ('0' + now.getHours()).slice(-2) + ':' + ('0' + now.getMinutes()).slice(-2);
    // "Mon 26 Oct", "Пн, 26 жовт." in the dashboard's language
    var date = now.toLocaleDateString(i18nLocale(), { weekday: 'short', day: 'numeric', month: 'short' });
    document.getElementById('weather-forecast-date').textContent = date.charAt(0).toUpperCase() + date.slice(1);
    // Next update just after the minute changes
    clearTimeout(clockTimer);
    clockTimer = setTimeout(updateClock, 60000 - now.getSeconds() * 1000 - now.getMilliseconds() + 50);
}

// ===== OUTAGE READINESS =====
// Will the battery last through the next planned outage? The server compares
// the energy the outage needs (recent average load x its length) with what the
// battery has left (/api/outage-readiness, outage_readiness.py).
var READINESS_POLL_MS = 60000;
var readinessData = null;

function fetchOutageReadiness() {
    apiFetch('/api/outage-readiness')
        .then(function(r) { return r.json(); })
        .then(function(data) { readinessData = data; renderOutageReadiness(); })
        .catch(function() {});   // keep the last answer through a failed request
}

// "14:00–18:00"; an end at the next midnight shows as 24:00
function readinessWindow(d) {
    var end = formatTimeFromISO(d.end);
    if (end === '00:00' && d.end.slice(0, 10) !== d.start.slice(0, 10)) end = '24:00';
    return formatTimeFromISO(d.start) + '–' + end;
}

// Long durations rounded to hours ("~15h"), so they stay short
function readinessDuration(mins) {
    return mins >= 600 ? '~' + t('duration.h', { h: Math.round(mins / 60) }) : formatPlannedDuration(mins);
}

function renderOutageReadiness() {
    var card = document.getElementById('readiness-card');
    var body = document.getElementById('readiness-body');
    if (!card || !body) return;
    var d = readinessData;
    if (!d || d.status === 'disabled') {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    card.classList.toggle('readiness-tight', d.verdict === 'tight');
    card.classList.toggle('readiness-short', d.verdict === 'short');

    if (d.status === 'no_outage' || d.status === 'no_schedule' || !d.start) {
        var key = d.status === 'no_outage' || d.status === 'no_schedule' ? d.status : 'no_data';
        body.innerHTML = '<div class="planned-none">' + escapeHtml(t('readiness.' + key)) + '</div>';
        return;
    }
    // When: a short label, then the window on its own line
    var duration = formatPlannedDuration(d.outage_minutes);
    var when = d.during
        ? t('readiness.now', { end: readinessWindow(d).split('–')[1], time: duration })
        : t('readiness.next', {
            day: t(d.start.slice(0, 10) === localDateString(new Date()) ? 'readiness.today' : 'readiness.tomorrow'),
            window: readinessWindow(d), time: duration });
    var html = '<div class="readiness-when-label">' + escapeHtml(t(d.during ? 'readiness.now_label' : 'readiness.next_label')) + '</div>' +
        '<div class="readiness-when">' + escapeHtml(when) + '</div>';
    if (d.status !== 'ok') {
        body.innerHTML = html + '<div class="planned-none">' + escapeHtml(t('readiness.no_data')) + '</div>';
        return;
    }

    var mark = d.verdict === 'short' ? '✕ ' : d.verdict === 'tight' ? '⚠ ' : '✓ ';
    var detail = d.lasts_minutes == null ? '' : d.verdict === 'short'
        ? t('readiness.lasts_short', { time: readinessDuration(d.lasts_minutes), short: readinessDuration(d.short_minutes) })
        : t('readiness.lasts', { time: readinessDuration(d.lasts_minutes) });
    var kwh = '<span class="unit">' + escapeHtml(t('units.kwh')) + '</span>';
    // Bar: the share of the available energy the outage needs (full and red when short)
    var share = d.available_kwh > 0 ? Math.min(100, Math.round(d.needed_kwh / d.available_kwh * 100)) : 100;
    html += '<div class="readiness-verdict readiness-' + d.verdict + '">' + escapeHtml(mark + t('readiness.' + d.verdict)) + '</div>' +
        (detail ? '<div class="readiness-detail">' + escapeHtml(detail) + '</div>' : '') +
        '<div class="stats-row readiness-energy">' +
        '<div class="stat"><div class="value">' + fmtKwh(d.needed_kwh) + kwh + '</div><div class="label">' + escapeHtml(t('readiness.needed')) + '</div></div>' +
        '<div class="stat"><div class="value">' + fmtKwh(d.available_kwh) + kwh + '</div><div class="label">' + escapeHtml(t('readiness.available')) + '</div></div>' +
        '</div>' +
        '<div class="readiness-bar readiness-bar-' + d.verdict + '"><div class="readiness-bar-fill" style="width: ' + share + '%"></div></div>';
    // One short line each (the average covers the last 30 minutes)
    html += '<div class="readiness-basis">' + escapeHtml(t('readiness.basis', { power: d.average_load_w, soc: d.shutdown_soc })) + '</div>';
    if (!d.during && d.soc_at_start > d.soc) {
        html += '<div class="readiness-basis">' + escapeHtml(t('readiness.charged_by_start', { soc: d.soc_at_start })) + '</div>';
    }
    body.innerHTML = html;
}

fetchOutageReadiness();
setInterval(fetchOutageReadiness, READINESS_POLL_MS);

// ===== INVERTER & BATTERY HEALTH =====
// From the inverter readings (/api/data); hidden when the driver doesn't report it.
function renderHealth(data) {
    var card = document.getElementById('health-card');
    if (!card) return;
    if (!data || !('inverter_state' in data)) {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    var state = data.inverter_state || 'unknown';
    card.classList.toggle('health-alarm', state === 'alarm');
    card.classList.toggle('health-fault', state === 'fault');
    document.getElementById('health-state').innerHTML =
        '<span class="health-label">' + escapeHtml(t('health.inverter')) + '</span>' +
        '<span class="health-chip health-' + state + '">' + escapeHtml(t('health.state_' + state)) + '</span>';

    // Codes only come while the inverter reports a warning or fault
    var codes = [];
    if ((data.inverter_fault_codes || []).length) codes.push(t('health.fault_codes', { codes: data.inverter_fault_codes.join(', ') }));
    if ((data.inverter_warning_codes || []).length) codes.push(t('health.warning_codes', { codes: data.inverter_warning_codes.join(', ') }));
    var codesEl = document.getElementById('health-codes');
    codesEl.hidden = !codes.length;
    codesEl.innerHTML = codes.length
        ? codes.map(function(line) { return '<div>' + escapeHtml(line) + '</div>'; }).join('') +
          '<div class="health-codes-hint">' + escapeHtml(t('health.codes_hint')) + '</div>'
        : '';

    function temperature(value) { return value == null ? '--' : fmt(value, 0) + '°'; }
    document.getElementById('health-temp-dc').textContent = temperature(data.inverter_temperature_dc);
    document.getElementById('health-temp-ac').textContent = temperature(data.inverter_temperature_ac);
    document.getElementById('health-temp-battery').textContent = temperature(data.battery_temperature);

    var hasBattery = !inverterConfig || inverterConfig.has_battery !== false;
    document.getElementById('health-temp-battery-stat').hidden = !hasBattery;
    document.getElementById('health-battery').hidden = !hasBattery;
    document.getElementById('health-charged').textContent = data.battery_total_charge_kwh == null ? '--' : fmt(data.battery_total_charge_kwh, 0);
    document.getElementById('health-discharged').textContent = data.battery_total_discharge_kwh == null ? '--' : fmt(data.battery_total_discharge_kwh, 0);
    document.getElementById('health-cycles').textContent = data.battery_cycles == null ? '--' : '≈ ' + data.battery_cycles;
    var capacityEl = document.getElementById('health-capacity');
    capacityEl.textContent = hasBattery && data.battery_capacity_ah ? t('health.capacity', { ah: data.battery_capacity_ah }) : '';
}

// ===== SMART METER =====
// Read-only readings of the MQTT smart meter (/api/smart-meter); the card stays
// hidden unless the meter is enabled on the server.
var SMART_METER_POLL_MS = 5000;
var smartMeterData = null;

var smartMeterOkAt = 0;

function fetchSmartMeter() {
    apiFetch('/api/smart-meter')
        .then(function(r) { return r.json(); })
        .then(function(data) { smartMeterOkAt = Date.now(); smartMeterData = data; renderSmartMeter(); })
        .catch(function() {
            // The request failed (not the meter or its broker): keep the last
            // readings; after a while say the dashboard can't be reached, with
            // the last values dimmed
            if (!noDataFor(smartMeterOkAt) || (smartMeterData && smartMeterData.status === 'disabled')) return;
            smartMeterData = Object.assign({}, smartMeterData || {}, { status: 'unreachable' });
            renderSmartMeter();
        });
}

function renderSmartMeter() {
    var d = smartMeterData;
    var card = document.getElementById('smart-meter-card');
    if (!card) return;
    if (!d || d.status === 'disabled') {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';

    document.getElementById('meter-demo').hidden = !d.demo;
    var badge = document.getElementById('meter-status');
    var hasData = !!d.updated;
    if (d.status === 'ok') {
        badge.hidden = true;
    } else {
        badge.hidden = false;
        badge.textContent = t('meter.status_' + (d.status === 'unknown' ? 'waiting' : d.status));
    }
    // Meter or broker unavailable: red badge; readings still on screen are the last known ones, dimmed
    badge.classList.toggle('meter-status-bad', d.status === 'offline' || d.status === 'error' || d.status === 'unreachable');
    card.classList.toggle('meter-not-live', d.status !== 'ok');

    document.getElementById('meter-power').innerHTML =
        (hasData ? fmt(d.power_w, 0) : '--') + '<span class="unit">' + t('units.w') + '</span>';

    var threePhase = d.phases === 3;
    document.getElementById('meter-single-phase').style.display = threePhase ? 'none' : '';
    document.getElementById('meter-phases').hidden = !threePhase;
    if (threePhase) {
        // Per phase: cut by the meter's relay, without supply, or normal
        var state = [0, 1, 2].map(function(i) {
            if (d.phases_connected && d.phases_connected[i] === false) return 'cut';
            if (d.phases_on && d.phases_on[i] === false) return 'no_voltage';
            return 'ok';
        });
        var anyOk = state.indexOf('ok') >= 0;
        var rows = '';
        for (var i = 0; i < 3; i++) {
            var pick = function(list) { return list && list.length > i ? list[i] : null; };
            var label = '<td><span class="phase-label l' + (i + 1) + '">L' + (i + 1) + '</span></td>';
            if (state[i] === 'ok') {
                rows += '<tr>' + label + '<td>' + fmt(pick(d.voltage), 0) + '</td><td>' +
                    fmt(pick(d.current), 1) + '</td><td>' + fmt(pick(d.power_phases), 0) + '</td></tr>';
                continue;
            }
            // A lost phase stands out while the meter is live and other phases still work
            var cls = 'meter-phase-off' + (d.status === 'ok' && anyOk ? ' meter-phase-alert' : '');
            if (state[i] === 'cut') {
                // Keep the voltage: it's usually why the meter tripped (e.g. over 255 V)
                rows += '<tr class="' + cls + '">' + label + '<td class="meter-phase-v">' + fmt(pick(d.voltage), 0) +
                    '</td><td class="meter-phase-state" colspan="2">' + t('meter.disconnected') + '</td></tr>';
            } else {
                rows += '<tr class="' + cls + '">' + label + '<td class="meter-phase-state" colspan="3">' +
                    t('meter.no_voltage') + '</td></tr>';
            }
        }
        document.getElementById('meter-phases-body').innerHTML = rows;
    } else {
        document.getElementById('meter-voltage').textContent = fmt((d.voltage || [])[0], 1);
        document.getElementById('meter-current').textContent = fmt((d.current || [])[0], 2);
    }

    var freq = d.frequency_max != null ? d.frequency_max : d.frequency_min;
    document.getElementById('meter-frequency').textContent =
        typeof freq === 'number' ? t('meter.frequency', { value: freq.toFixed(1) }) : '';
    // One relay per phase: its state is in the phase rows; otherwise relay 1 here
    var relays = d.relays || [];
    document.getElementById('meter-relay').textContent = relays.length && !(threePhase && d.phases_connected)
        ? t('meter.relay', { state: t(relays[0] > 0 ? 'meter.relay_on' : 'meter.relay_off') })
        : '';
}

fetchSmartMeter();
setInterval(fetchSmartMeter, SMART_METER_POLL_MS);

// ===== OUTAGE HISTORY =====
// Grid outages the smart meter saw today and yesterday (/api/meter-outages,
// meter_outages.py): total (no grid) or partial (phases with no voltage from
// the grid, or disconnected by the meter's relay). The card lists the latest
// OUTAGE_LOG_ROWS (one line each); the rest of today and yesterday is in a
// dialog. An outage over midnight is in both days there, marked "Yesterday"
// under today.
var OUTAGE_LOG_POLL_MS = 30000;
var OUTAGE_LOG_ROWS = 5;
var outageLogData = null;
var outageLogAt = 0;   // when it arrived: running outages count on from the server's "now"

function fetchOutageLog() {
    apiFetch('/api/meter-outages')
        .then(function(r) { return r.json(); })
        .then(function(data) { outageLogData = data; outageLogAt = Date.now(); renderOutageLog(); })
        .catch(function() {});   // keep the last list through a failed request
}

function outageLogNow() {
    return new Date(new Date(outageLogData.now).getTime() + (Date.now() - outageLogAt));
}

// 45 s, 12m, 1h 20m
function outageDuration(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return t('duration.s', { s: s });
    var m = Math.round(s / 60);
    if (m < 60) return t('duration.m', { m: m });
    return m % 60 ? t('duration.hm', { h: Math.floor(m / 60), m: m % 60 }) : t('duration.h', { h: m / 60 });
}

function outageEnd(o) {
    return o.end ? new Date(o.end) : outageLogNow();
}

// What was missing: "No grid", or which phases had no voltage from the grid
// and which the meter disconnected
// long: the full wording, for the tooltip ("L2: no voltage from the grid");
// else short for the row ("L2: no grid", "L1–L3: cut by meter")
function outageKind(o, long) {
    if (o.type === 'total') return t('outage_log.total');
    var label = function(list) {
        return list.length === 3 ? 'L1–L3' : list.map(function(p) { return 'L' + p; }).join(', ');
    };
    var dead = o.phases.filter(function(p) { return o.cut.indexOf(p) === -1; });
    var parts = [];
    if (dead.length) parts.push(t(long ? 'outage_log.no_voltage' : 'outage_log.no_voltage_short', { phases: label(dead) }));
    if (o.cut.length) parts.push(t(long ? 'outage_log.cut' : 'outage_log.cut_short', { phases: label(o.cut) }));
    return parts.join(' · ');
}

function outageRow(o, showDay) {
    var start = new Date(o.start), end = outageEnd(o);
    var time = o.end ? clockText(start.getTime() / 1000) + '–' + clockText(end.getTime() / 1000)
                     : t('outage_log.since', { time: clockText(start.getTime() / 1000) });
    var day = showDay && o.start.slice(0, 10) !== outageLogData.today ? t('outage_log.yesterday') + ' · ' : '';
    return '<div class="outage-row outage-' + o.type + (o.end ? '' : ' outage-now') + '">' +
        '<span class="outage-dot"></span>' +
        '<div class="outage-main"><span class="outage-time">' + escapeHtml(time) + '</span>' +
        '<span class="outage-kind" title="' + escapeHtml(day + outageKind(o, true)) + '">' +
        escapeHtml(day + outageKind(o)) + '</span></div>' +
        '<span class="outage-duration">' + escapeHtml(outageDuration(end - start)) + '</span></div>';
}

// The outages of one day (today 0, yesterday 1) and the time without power in
// it (an outage over midnight counts in both days)
function outageDay(back) {
    var now = outageLogNow();
    var from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back);
    var to = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 1);
    var list = outageLogData.outages.filter(function(o) { return new Date(o.start) < to && outageEnd(o) > from; });
    var ms = list.reduce(function(sum, o) {
        return sum + Math.min(outageEnd(o), to) - Math.max(new Date(o.start), from);
    }, 0);
    return { list: list, ms: ms };
}

function outageDaySummary(key, day) {
    return '<strong>' + escapeHtml(t(key)) + '</strong> · ' + escapeHtml(day.list.length
        ? t('outage_log.count', { count: day.list.length }) + ' · ' + t('outage_log.without', { duration: outageDuration(day.ms) })
        : t('outage_log.none'));
}

function renderOutageLog() {
    var card = document.getElementById('outage-log-card');
    if (!card || !outageLogData) return;
    if (outageLogData.status !== 'ok') {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    document.getElementById('outage-log-demo').hidden = !outageLogData.demo;
    var outages = outageLogData.outages;
    document.getElementById('outage-log-summary').innerHTML = outageDaySummary('outage_log.today', outageDay(0));
    document.getElementById('outage-log-list').innerHTML = outages.length
        ? outages.slice(0, OUTAGE_LOG_ROWS).map(function(o) { return outageRow(o, true); }).join('')
        : '<div class="outage-log-empty">' + escapeHtml(t('outage_log.empty')) + '</div>';
    var more = document.getElementById('outage-log-more');
    more.hidden = outages.length <= OUTAGE_LOG_ROWS;
    more.textContent = t('outage_log.more', { count: outages.length });
    if (!document.getElementById('outage-log-overlay').hidden) renderOutageLogDays();
}

function renderOutageLogDays() {
    document.getElementById('outage-log-days').innerHTML = [['outage_log.today', 0], ['outage_log.yesterday', 1]].map(function(d) {
        var day = outageDay(d[1]);
        return '<div class="outage-log-day"><div class="outage-log-summary">' + outageDaySummary(d[0], day) + '</div>' +
            day.list.map(function(o) { return outageRow(o, d[1] === 0); }).join('') + '</div>';
    }).join('');
}

function openOutageLog() {
    renderOutageLogDays();
    document.getElementById('outage-log-overlay').hidden = false;
    document.getElementById('outage-log-dialog').focus();
}

function closeOutageLog() {
    var overlay = document.getElementById('outage-log-overlay');
    if (overlay) overlay.hidden = true;
}

(function() {
    var overlay = document.getElementById('outage-log-overlay');
    if (!overlay) return;
    overlay.addEventListener('click', function(e) { if (e.target === overlay) closeOutageLog(); });
    document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeOutageLog(); });
})();

fetchOutageLog();
setInterval(fetchOutageLog, OUTAGE_LOG_POLL_MS);

// ===== AIR RAID ALERTS =====
// Map of Ukraine's regions (static/data/ukraine-regions.json, ids are ISO 3166-2
// codes) coloured by /api/alerts. Drawn into every [data-alerts-map]: the
// dashboard cards and the fullscreen banner. data-alerts-regions="UA-30,UA-32"
// limits a map to those regions and zooms to them.
var ALERTS_POLL_MS = 30000;
var alertsMapData = null;
var alertsData = null;
var alertsIntervalId = null;

// Configured region (AIR_ALERTS_REGION): maps show it zoomed in; a region that
// contains a city-region shows both (Kyiv oblast + Kyiv, Crimea + Sevastopol)
var ALERTS_REGION_WITH = { 'UA-32': ['UA-30'], 'UA-43': ['UA-40'] };

function applyAlertsRegion() {
    var region = window.DASHBOARD_CONFIG.alertsRegion;
    if (!region) return;
    var regions = [region].concat(ALERTS_REGION_WITH[region] || []);
    document.querySelectorAll('[data-alerts-map]').forEach(function(el) {
        if (el.hasAttribute('data-alerts-regions')) return;
        el.setAttribute('data-alerts-regions', regions.join(','));
        el.classList.add('alerts-map-zoomed');
    });
}

function loadAlertsMap() {
    apiFetch(window.DASHBOARD_CONFIG.alertsMapUrl)
        .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function(map) {
            alertsMapData = map;
            buildAlertsMaps();
            renderAlerts();
        })
        .catch(function(e) { console.error('Failed to load alerts map:', e); });
}

// Kyiv and Sevastopol are cities: their single "district" is the city itself
var ALERTS_CITY_DISTRICTS = { 'UA8000': true, 'UA8500': true };
var alertsDistrictRegion = {};   // district code -> region code
var alertsDistrictNames = {};    // district code -> {uk, en}

function buildAlertsMaps() {
    var enclaveLast = function(a, b) {
        var enclave = function(id) { return id === 'UA-30' || id === 'UA-40' || ALERTS_CITY_DISTRICTS[id] ? 1 : 0; };
        return enclave(a.id) - enclave(b.id);
    };
    var districts = (alertsMapData.districts || []).slice().sort(enclaveLast);
    var regions = alertsMapData.regions.slice().sort(enclaveLast);
    districts.forEach(function(d) {
        alertsDistrictRegion[d.id] = d.region;
        alertsDistrictNames[d.id] = d.name || {};
    });
    document.querySelectorAll('[data-alerts-map]').forEach(function(el) {
        var focus = (el.getAttribute('data-alerts-regions') || '').split(',').filter(Boolean);
        el.innerHTML = alertsMapSvg(districts, regions, focus);
    });
}

// The whole country; with focus regions, zoomed to them (neighbours stay visible
// around the edges, slightly dimmed, so it reads as a zoomed-in map of Ukraine)
function alertsMapSvg(districts, regions, focus) {
    var inFocus = function(regionId) { return !focus.length || focus.indexOf(regionId) >= 0; };
    var focusRegions = regions.filter(function(r) { return focus.indexOf(r.id) >= 0; });
    var viewBox = focusRegions.length ? alertsFitViewBox(focusRegions) : alertsMapData.viewBox;
    return '<svg viewBox="' + viewBox + '" role="img" preserveAspectRatio="xMidYMid meet">' +
        '<g class="alerts-districts">' + districts.map(function(d) {
            return '<path class="alerts-district status-unknown" data-district="' + escapeHtml(d.id) +
                '" data-region="' + escapeHtml(d.region) + '"' + (inFocus(d.region) ? '' : ' data-context') +
                ' d="' + d.d + '"><title></title></path>';
        }).join('') + '</g>' +
        '<g class="alerts-outlines">' + regions.map(function(r) {
            return '<path class="alerts-outline" d="' + r.d + '"/>';
        }).join('') + '</g>' +
        // Focus regions' outline on top, so their border is never hidden by a neighbour's
        (focusRegions.length ? '<g class="alerts-outlines-focus">' + focusRegions.map(function(r) {
            return '<path class="alerts-outline alerts-outline-focus" d="' + r.d + '"/>';
        }).join('') + '</g>' : '') + '</svg>';
}

// Bounding box of the outlines (paths are absolute M/L/Z) with a margin that
// brings in a strip of the neighbouring regions (15%). Computed from the data, so it
// also works while the map is hidden.
function alertsFitViewBox(regions) {
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    regions.forEach(function(r) {
        var nums = r.d.match(/-?\d+(\.\d+)?/g) || [];
        for (var i = 0; i + 1 < nums.length; i += 2) {
            var x = +nums[i], y = +nums[i + 1];
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
        }
    });
    if (!isFinite(minX)) return alertsMapData.viewBox;
    var pad = Math.max(maxX - minX, maxY - minY) * 0.15;
    return [minX - pad, minY - pad, maxX - minX + 2 * pad, maxY - minY + 2 * pad]
        .map(function(v) { return v.toFixed(1); }).join(' ');
}

// Colour of one district: its own status, or its region's when the region is
// under alert as a whole (or partially, without district details)
function alertsDistrictStatus(d, districtId, regionId, regionsWithDistrictInfo) {
    if (!d || d.status !== 'ok') return 'unknown';
    var region = (d.regions[regionId] || {}).status || 'none';
    var own = ((d.districts || {})[districtId] || {}).status || 'none';
    if (own === 'alert' || region === 'alert') return 'alert';
    if (own === 'partial') return 'partial';
    if (own === 'threat') return 'threat';
    // Region-level status without district details covers the whole region
    if (!regionsWithDistrictInfo[regionId] && (region === 'partial' || region === 'threat')) return region;
    return 'none';
}

function alertsDistrictTitle(districtId, regionId) {
    var regionName = t('alerts.regions.' + regionId);
    if (ALERTS_CITY_DISTRICTS[districtId]) return regionName;
    var names = alertsDistrictNames[districtId] || {};
    var name = (i18nLang === 'en' ? names.en : names.uk) || names.uk || districtId;
    return t('alerts.district_name', { name: name }) + ', ' + regionName;
}

var alertsOkAt = 0;

function fetchAlerts() {
    apiFetch('/api/alerts')
        .then(function(r) { return r.json(); })
        .then(function(data) { alertsOkAt = Date.now(); alertsData = data; renderAlerts(); })
        .catch(function() {
            // Keep the last map until nothing has arrived for a while
            if (!noDataFor(alertsOkAt)) return;
            alertsData = { status: 'error' };
            renderAlerts();
        });
}

function renderAlerts() {
    var d = alertsData;
    var enabled = !(d && d.status === 'disabled');
    document.body.classList.toggle('alerts-enabled', enabled);
    document.querySelectorAll('[data-alerts-card]').forEach(function(card) {
        card.style.display = enabled ? '' : 'none';
    });
    if (!enabled) {
        if (alertsIntervalId) { clearInterval(alertsIntervalId); alertsIntervalId = null; }
        return;
    }

    var ok = d && d.status === 'ok';
    var regionsWithDistrictInfo = {};
    if (ok) {
        Object.keys(d.districts || {}).forEach(function(id) {
            if (alertsDistrictRegion[id]) regionsWithDistrictInfo[alertsDistrictRegion[id]] = true;
        });
    }
    document.querySelectorAll('[data-alerts-map] .alerts-district').forEach(function(path) {
        var id = path.getAttribute('data-district');
        var region = path.getAttribute('data-region');
        var status = alertsDistrictStatus(d, id, region, regionsWithDistrictInfo);
        path.setAttribute('class', 'alerts-district status-' + status);
        var title = alertsDistrictTitle(id, region);
        if (status !== 'unknown') title += ' — ' + t('alerts.status_' + status);
        path.firstChild.textContent = title;
    });

    var summary;
    if (!d || d.status === 'unknown') summary = t('alerts.loading');
    else if (!ok) summary = t('alerts.unavailable');
    // With data: no own timestamp, the status line's "Updated" covers the dashboard
    else summary = '';
    document.querySelectorAll('[data-alerts-summary]').forEach(function(el) { el.textContent = summary; });
    // Region shown on the map, next to the title
    var region = window.DASHBOARD_CONFIG.alertsRegion;
    document.querySelectorAll('[data-alerts-region-name]').forEach(function(el) {
        el.hidden = !region;
        el.textContent = region ? t('alerts.regions.' + region) : '';
    });
    document.querySelectorAll('.alerts-demo-badge').forEach(function(el) { el.hidden = !(d && d.demo); });
    // Data source link + disclaimer whenever real data is shown (NEPTUN's terms require them)
    document.querySelectorAll('[data-alerts-credit]').forEach(function(el) {
        var shown = false;
        el.querySelectorAll('[data-alerts-source]').forEach(function(link) {
            link.hidden = !(d && link.getAttribute('data-alerts-source') === d.provider);
            shown = shown || !link.hidden;
        });
        el.hidden = !shown;
    });
}

applyAlertsRegion();
loadAlertsMap();
fetchAlerts();
alertsIntervalId = setInterval(fetchAlerts, ALERTS_POLL_MS);

// Screen back on / tab visible again, or network back: don't wait for the next
// poll (timers are throttled or paused while the tablet sleeps)
function refreshAllData() {
    updateClock();
    updateDashboard();
    fetchSmartMeter();
    fetchAlerts();
    fetchOutageSchedule();
    fetchWeather();
    fetchOutageReadiness();
}
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') refreshAllData();
});
window.addEventListener('online', refreshAllData);

// ===== GENERATOR =====
function fetchGeneratorData() {
    apiFetch('/api/generator')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            generatorData = data;
            updateGeneratorBanner(data);
            updateGeneratorCard(data);
        })
        .catch(function() { generatorData = null; });
}

function updateGeneratorBanner(data) {
    var banner = document.getElementById('generator-banner');
    var valueEl = document.getElementById('generator-banner-value');
    var statusEl = document.getElementById('generator-banner-status');
    if (!banner || !valueEl || !statusEl) return;

    var power = data && data.power || 0;
    var running = power > 0;

    banner.classList.remove('generator-running', 'generator-off');
    if (running) {
        banner.classList.add('generator-running');
        setPlainText(valueEl, t('units.power_w', { value: power }));
        var hours = data ? data.today_runtime_hours : 0;
        statusEl.textContent = t('generator.runtime_today', { time: formatGeneratorDuration(hours) });
    } else {
        banner.classList.add('generator-off');
        setI18nText(valueEl, 'generator.off');
        statusEl.textContent = t('generator.standby');
    }
}

function updateGeneratorCard(data) {
    var card = document.getElementById('generator-card');
    if (!card) return;

    var power = data && data.power || 0;
    var running = power > 0;
    var todayHours = data ? data.today_runtime_hours : 0;

    // Show card when running or has runtime today
    if (running || todayHours > 0) {
        card.classList.add('generator-card-visible');
    } else {
        card.classList.remove('generator-card-visible');
    }

    // Active state styling
    if (running) {
        card.classList.add('generator-active');
    } else {
        card.classList.remove('generator-active');
    }

    var powerEl = document.getElementById('generator-power');
    if (powerEl) powerEl.innerHTML = power + '<span class="unit">' + t('units.w') + '</span>';

    var todayEl = document.getElementById('generator-today-runtime');
    if (todayEl) todayEl.textContent = data ? formatGeneratorDuration(data.today_runtime_hours) : '--';

    var monthlyEl = document.getElementById('generator-monthly-runtime');
    if (monthlyEl) monthlyEl.textContent = data ? formatGeneratorDuration(data.monthly_runtime_hours) : '--';

    // Fuel row
    var fuelRow = document.getElementById('generator-fuel-row');
    if (fuelRow && data) {
        if (data.fuel_rate && data.fuel_rate > 0) {
            fuelRow.style.display = '';
            var fuelTodayEl = document.getElementById('generator-fuel-today');
            if (fuelTodayEl) fuelTodayEl.textContent = data.fuel_today_liters != null ? data.fuel_today_liters.toFixed(1) : '--';
            var fuelMonthlyEl = document.getElementById('generator-fuel-monthly');
            if (fuelMonthlyEl) fuelMonthlyEl.textContent = data.fuel_monthly_liters != null ? data.fuel_monthly_liters.toFixed(1) : '--';
        } else {
            fuelRow.style.display = 'none';
        }
    }

    // Oil change row
    var oilRow = document.getElementById('generator-oil-row');
    if (oilRow && data) {
        if (data.oil_change_date) {
            oilRow.style.display = '';
            var oilEl = document.getElementById('generator-oil-hours');
            if (oilEl) oilEl.textContent = data.oil_change_hours_since != null ? formatGeneratorDuration(data.oil_change_hours_since) : '--';
        } else {
            oilRow.style.display = 'none';
        }
    }
}

// === Onboarding Wizard ===
var onboardingStep = 1;
var ONBOARDING_STEPS = 5;   // inverter, weather, outages, air raid alerts, optional features
var onboardingEditMode = false;
var selectedOutageProvider = 'lvivoblenergo';

function checkFirstRun() {
    if (!window.DASHBOARD_CONFIG.firstRun) return;
    // A configuration exists but isn't valid: the wizard fixes it, but it's
    // still protected by the settings PIN
    if (window.DASHBOARD_CONFIG.settingsLocked) {
        showPinPrompt(function() { showOnboarding(false); });
    } else {
        showOnboarding(false);
    }
}

function showOnboarding(editMode) {
    onboardingEditMode = editMode;
    onboardingStep = 1;
    var overlay = document.getElementById('onboarding-overlay');
    overlay.classList.remove('hidden');
    fillInverterTypeOptions();
    fillAlertsRegionOptions();
    updateOnboardingUI();

    if (editMode) {
        // Load current config values
        settingsFetch('/api/config')
            .then(function(r) { return r.json(); })
            .then(function(cfg) {
                if (cfg.INVERTER_TYPE) document.getElementById('onb-type').value = cfg.INVERTER_TYPE;
                if (cfg.INVERTER_IP) document.getElementById('onb-ip').value = cfg.INVERTER_IP;
                if (cfg.LOGGER_SERIAL) document.getElementById('onb-serial').value = cfg.LOGGER_SERIAL;
                document.getElementById('onb-phases').value =
                    cfg.INVERTER_PHASES === '1' || cfg.INVERTER_PHASES === '3' ? cfg.INVERTER_PHASES : '';
                if (cfg.WEATHER_LATITUDE) document.getElementById('onb-lat').value = cfg.WEATHER_LATITUDE;
                if (cfg.WEATHER_LONGITUDE) document.getElementById('onb-lon').value = cfg.WEATHER_LONGITUDE;

                // Outage provider
                var prov = cfg.OUTAGE_PROVIDER || 'lvivoblenergo';
                selectOutageProviderByValue(prov);
                if (cfg.OUTAGE_GROUP) document.getElementById('onb-outage-group').value = cfg.OUTAGE_GROUP;
                if (cfg.OUTAGE_REGION_ID) document.getElementById('onb-yasno-region').value = cfg.OUTAGE_REGION_ID;
                if (cfg.OUTAGE_DSO_ID) document.getElementById('onb-yasno-dso').value = cfg.OUTAGE_DSO_ID;
                if (cfg.OUTAGE_GROUP && prov === 'yasno') document.getElementById('onb-yasno-group').value = cfg.OUTAGE_GROUP;
                if (cfg.OUTAGE_DTEK_REGION) document.getElementById('onb-dtek-region').value = cfg.OUTAGE_DTEK_REGION;
                if (cfg.OUTAGE_GROUP && prov === 'dtek') document.getElementById('onb-dtek-group').value = cfg.OUTAGE_GROUP;

                // Generator
                if (cfg.INVERTER_HAS_GENERATOR && cfg.INVERTER_HAS_GENERATOR.toLowerCase() === 'true') {
                    var genToggle = document.getElementById('onb-gen-toggle');
                    if (!genToggle.classList.contains('on')) toggleOnb(genToggle, 'onb-gen-fields');
                }
                if (cfg.GENERATOR_FUEL_RATE) document.getElementById('onb-gen-fuel').value = cfg.GENERATOR_FUEL_RATE;
                if (cfg.GENERATOR_OIL_CHANGE_DATE) document.getElementById('onb-gen-oil').value = cfg.GENERATOR_OIL_CHANGE_DATE;

                document.getElementById('onb-title').value = cfg.DASHBOARD_TITLE || '';

                // Telegram
                if (cfg.TELEGRAM_ENABLED && cfg.TELEGRAM_ENABLED.toLowerCase() === 'true') {
                    var tgToggle = document.getElementById('onb-tg-toggle');
                    if (!tgToggle.classList.contains('on')) toggleOnb(tgToggle, 'onb-tg-fields');
                }
                if (cfg.TELEGRAM_BOT_TOKEN) document.getElementById('onb-tg-token').value = cfg.TELEGRAM_BOT_TOKEN;
                if (cfg.TELEGRAM_ALLOWED_USERS) document.getElementById('onb-tg-users').value = cfg.TELEGRAM_ALLOWED_USERS;
                if (cfg.TELEGRAM_PUBLIC && cfg.TELEGRAM_PUBLIC.toLowerCase() === 'true') {
                    var pubToggle = document.getElementById('onb-tg-public-toggle');
                    if (!pubToggle.classList.contains('on')) toggleOnb(pubToggle);
                }

                // Air raid alerts. No provider set: alerts.in.ua with a token, else NEPTUN.
                // The token comes masked; sent back unchanged, the server keeps it.
                var alertsProvider = (cfg.AIR_ALERTS_PROVIDER || (cfg.ALERTS_IN_UA_TOKEN ? 'alerts_in_ua' : 'neptun')).toLowerCase();
                var alertsToggle = document.getElementById('onb-alerts-toggle');
                if (alertsToggle.classList.contains('on') !== (alertsProvider !== 'none')) {
                    toggleOnb(alertsToggle, 'onb-alerts-fields');
                }
                if (alertsProvider !== 'none') document.getElementById('onb-alerts-provider').value = alertsProvider;
                document.getElementById('onb-alerts-token').value = cfg.ALERTS_IN_UA_TOKEN || '';
                updateAlertsProviderFields();
                document.getElementById('onb-alerts-region').value = cfg.AIR_ALERTS_REGION || '';
            })
            .catch(function() {});
    }
}

function hideOnboarding() {
    document.getElementById('onboarding-overlay').classList.add('hidden');
}

function updateOnboardingUI() {
    // Update progress circles
    var circles = document.querySelectorAll('#onboarding-progress .step-circle');
    var lines = document.querySelectorAll('#onboarding-progress .step-line');
    circles.forEach(function(c) {
        var s = parseInt(c.getAttribute('data-step'));
        c.classList.remove('active', 'done');
        if (s === onboardingStep) c.classList.add('active');
        else if (s < onboardingStep) c.classList.add('done');
    });
    lines.forEach(function(l) {
        var s = parseInt(l.getAttribute('data-line'));
        l.classList.toggle('done', s < onboardingStep);
    });

    // Show/hide steps
    for (var i = 1; i <= ONBOARDING_STEPS; i++) {
        var stepEl = document.getElementById('onb-step-' + i);
        stepEl.classList.toggle('active', i === onboardingStep);
    }

    // Back button visibility
    document.getElementById('onb-back-btn').style.visibility = onboardingStep > 1 ? 'visible' : 'hidden';

    // Skip button (steps 2-4)
    var skipBtn = document.getElementById('onb-skip-btn');
    skipBtn.style.display = (onboardingStep >= 2 && onboardingStep <= 4) ? '' : 'none';

    // Next/Finish button text
    var nextBtn = document.getElementById('onb-next-btn');
    if (onboardingStep === ONBOARDING_STEPS) {
        setI18nText(nextBtn, onboardingEditMode ? 'onboarding.save_restart' : 'onboarding.finish_start');
    } else {
        setI18nText(nextBtn, 'onboarding.next');
    }
}

function onboardingGoTo(step) {
    if (step < 1) step = 1;
    if (step > ONBOARDING_STEPS) step = ONBOARDING_STEPS;
    onboardingStep = step;
    updateOnboardingUI();
    if (step === 2) useMyLocation();
}

function onboardingNext() {
    if (onboardingStep < ONBOARDING_STEPS) {
        if (onboardingStep === 1 && !validateStep1()) return;
        if (onboardingStep === 4 && !validateAlertsStep()) return;
        onboardingGoTo(onboardingStep + 1);
    } else {
        saveConfig();
    }
}

function validateStep1() {
    var ip = document.getElementById('onb-ip').value.trim();
    var serial = document.getElementById('onb-serial').value.trim();
    var valid = true;

    document.getElementById('onb-ip').classList.remove('error');
    document.getElementById('onb-serial').classList.remove('error');

    if (!ip || !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
        document.getElementById('onb-ip').classList.add('error');
        valid = false;
    }
    if (!serial || !/^\d+$/.test(serial) || serial === '0') {
        document.getElementById('onb-serial').classList.add('error');
        valid = false;
    }
    return valid;
}

function discoverInverters() {
    var btn = document.getElementById('onb-scan-btn');
    var statusEl = document.getElementById('onb-scan-status');
    var resultsEl = document.getElementById('onb-discover-results');

    btn.disabled = true;
    statusEl.innerHTML = '<span class="onboarding-spinner"></span> ' + t('onboarding.scanning');
    resultsEl.innerHTML = '';

    var typeSelect = document.getElementById('onb-type');
    settingsFetch('/api/config/discover' + (typeSelect.value ? '?type=' + encodeURIComponent(typeSelect.value) : ''))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            btn.disabled = false;
            var devices = data.devices || [];
            if (devices.length === 0) {
                statusEl.textContent = t('onboarding.no_devices');
                return;
            }
            statusEl.textContent = t('onboarding.devices_found', { count: devices.length });
            resultsEl.innerHTML = '';
            devices.forEach(function(dev) {
                var div = document.createElement('div');
                div.className = 'discover-device';
                // textContent, not innerHTML: the model string comes from any
                // device on the LAN answering the discovery broadcast
                var ipEl = document.createElement('div');
                ipEl.className = 'device-ip';
                ipEl.textContent = dev.ip;
                var modelEl = document.createElement('div');
                modelEl.className = 'device-model';
                modelEl.textContent = dev.model || t('onboarding.unknown_model');
                div.append(ipEl, modelEl);
                div.onclick = function() {
                    document.querySelectorAll('.discover-device').forEach(function(d) { d.classList.remove('selected'); });
                    div.classList.add('selected');
                    document.getElementById('onb-ip').value = dev.ip;
                };
                resultsEl.appendChild(div);
            });
        })
        .catch(function() {
            btn.disabled = false;
            statusEl.textContent = t('onboarding.scan_failed');
        });
}

function useMyLocation() {
    // Try browser geolocation first (requires HTTPS)
    if (navigator.geolocation && location.protocol === 'https:') {
        navigator.geolocation.getCurrentPosition(function(pos) {
            document.getElementById('onb-lat').value = pos.coords.latitude.toFixed(4);
            document.getElementById('onb-lon').value = pos.coords.longitude.toFixed(4);
        });
        return;
    }
    // Fallback: IP-based geolocation (works over HTTP)
    fetch('http://ip-api.com/json/?fields=lat,lon')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.lat && data.lon) {
                document.getElementById('onb-lat').value = data.lat.toFixed(4);
                document.getElementById('onb-lon').value = data.lon.toFixed(4);
            }
        })
        .catch(function() {});
}

function selectOutageProvider(el) {
    document.querySelectorAll('#onb-outage-radios .onboarding-radio').forEach(function(r) { r.classList.remove('selected'); });
    el.classList.add('selected');
    selectedOutageProvider = el.getAttribute('data-value');

    document.getElementById('onb-outage-lviv-fields').classList.toggle('visible', selectedOutageProvider === 'lvivoblenergo');
    document.getElementById('onb-outage-yasno-fields').classList.toggle('visible', selectedOutageProvider === 'yasno');
    document.getElementById('onb-outage-dtek-fields').classList.toggle('visible', selectedOutageProvider === 'dtek');
}

function selectOutageProviderByValue(value) {
    var radios = document.querySelectorAll('#onb-outage-radios .onboarding-radio');
    radios.forEach(function(r) {
        r.classList.remove('selected');
        if (r.getAttribute('data-value') === value) {
            r.classList.add('selected');
        }
    });
    selectedOutageProvider = value;
    document.getElementById('onb-outage-lviv-fields').classList.toggle('visible', value === 'lvivoblenergo');
    document.getElementById('onb-outage-yasno-fields').classList.toggle('visible', value === 'yasno');
    document.getElementById('onb-outage-dtek-fields').classList.toggle('visible', value === 'dtek');
}

// Inverter driver list (server: one entry per driver; hidden while there is only one)
function fillInverterTypeOptions() {
    var types = window.DASHBOARD_CONFIG.inverterTypes || [];
    var select = document.getElementById('onb-type');
    document.getElementById('onb-type-field').hidden = types.length < 2;
    if (select.options.length) return;
    types.forEach(function(type) {
        var option = document.createElement('option');
        option.value = type.id;
        option.textContent = type.name;
        select.appendChild(option);
    });
}

// Alerts step: the token field only for alerts.in.ua
function updateAlertsProviderFields() {
    var needsToken = document.getElementById('onb-alerts-provider').value === 'alerts_in_ua';
    document.getElementById('onb-alerts-token-field').hidden = !needsToken;
    if (!needsToken) document.getElementById('onb-alerts-token').classList.remove('error');
}

// alerts.in.ua needs its token
function validateAlertsStep() {
    var alertsOn = document.getElementById('onb-alerts-toggle').classList.contains('on');
    var tokenInput = document.getElementById('onb-alerts-token');
    var valid = !alertsOn || document.getElementById('onb-alerts-provider').value !== 'alerts_in_ua' ||
        tokenInput.value.trim() !== '';
    tokenInput.classList.toggle('error', !valid);
    return valid;
}

// Region list for the alerts step: "Whole Ukraine" first, then regions by translated name

function fillAlertsRegionOptions() {
    var select = document.getElementById('onb-alerts-region');
    var current = select.value;
    while (select.options.length > 1) select.remove(1);
    (window.DASHBOARD_CONFIG.alertsRegionCodes || [])
        .map(function(code) { return { code: code, name: t('alerts.regions.' + code) }; })
        .sort(function(a, b) { return a.name.localeCompare(b.name, i18nLocale()); })
        .forEach(function(r) {
            var option = document.createElement('option');
            option.value = r.code;
            option.textContent = r.name;
            select.appendChild(option);
        });
    select.value = current;
}

function toggleOnb(el, fieldsId) {
    el.classList.toggle('on');
    if (fieldsId) {
        document.getElementById(fieldsId).classList.toggle('visible', el.classList.contains('on'));
    }
}

function saveConfig() {
    var config = {};

    // Step 1 (the type only when there is a choice; otherwise the server's default)
    var typeSelect = document.getElementById('onb-type');
    if (typeSelect.options.length > 1) config.INVERTER_TYPE = typeSelect.value;
    config.INVERTER_IP = document.getElementById('onb-ip').value.trim();
    config.LOGGER_SERIAL = document.getElementById('onb-serial').value.trim();
    config.INVERTER_PHASES = document.getElementById('onb-phases').value;  // '' = auto-detect

    // Step 2
    config.WEATHER_LATITUDE = document.getElementById('onb-lat').value.trim() || '50.4501';
    config.WEATHER_LONGITUDE = document.getElementById('onb-lon').value.trim() || '30.5234';

    // Step 3
    config.OUTAGE_PROVIDER = selectedOutageProvider;
    if (selectedOutageProvider === 'lvivoblenergo') {
        config.OUTAGE_GROUP = document.getElementById('onb-outage-group').value.trim() || '1.1';
    } else if (selectedOutageProvider === 'yasno') {
        config.OUTAGE_REGION_ID = document.getElementById('onb-yasno-region').value.trim() || '25';
        config.OUTAGE_DSO_ID = document.getElementById('onb-yasno-dso').value.trim() || '902';
        config.OUTAGE_GROUP = document.getElementById('onb-yasno-group').value.trim() || '2.1';
    } else if (selectedOutageProvider === 'dtek') {
        config.OUTAGE_DTEK_REGION = document.getElementById('onb-dtek-region').value;
        config.OUTAGE_GROUP = document.getElementById('onb-dtek-group').value.trim() || '1.1';
    }

    // Step 5 — Dashboard name (empty: the translated default)
    config.DASHBOARD_TITLE = document.getElementById('onb-title').value.trim();

    // Step 5 — Generator
    var genOn = document.getElementById('onb-gen-toggle').classList.contains('on');
    config.INVERTER_HAS_GENERATOR = genOn ? 'true' : 'false';
    if (genOn) {
        var fuel = document.getElementById('onb-gen-fuel').value.trim();
        if (fuel && parseFloat(fuel) > 0) config.GENERATOR_FUEL_RATE = fuel;
        var oil = document.getElementById('onb-gen-oil').value;
        if (oil) config.GENERATOR_OIL_CHANGE_DATE = oil;
    }

    // Step 5 — Telegram
    var tgOn = document.getElementById('onb-tg-toggle').classList.contains('on');
    config.TELEGRAM_ENABLED = tgOn ? 'true' : 'false';
    if (tgOn) {
        config.TELEGRAM_BOT_TOKEN = document.getElementById('onb-tg-token').value.trim();
        config.TELEGRAM_ALLOWED_USERS = document.getElementById('onb-tg-users').value.trim();
        config.TELEGRAM_PUBLIC = document.getElementById('onb-tg-public-toggle').classList.contains('on') ? 'true' : 'false';
    }

    // Step 4 — Air raid alerts (the token is kept even while another source is chosen)
    var alertsOn = document.getElementById('onb-alerts-toggle').classList.contains('on');
    config.AIR_ALERTS_PROVIDER = alertsOn ? document.getElementById('onb-alerts-provider').value : 'none';
    config.AIR_ALERTS_REGION = document.getElementById('onb-alerts-region').value;
    config.ALERTS_IN_UA_TOKEN = document.getElementById('onb-alerts-token').value.trim();

    // Validate step 1
    if (!config.INVERTER_IP || !config.LOGGER_SERIAL) {
        onboardingGoTo(1);
        validateStep1();
        return;
    }
    if (!validateAlertsStep()) {
        onboardingGoTo(4);
        validateAlertsStep();
        return;
    }

    var nextBtn = document.getElementById('onb-next-btn');
    nextBtn.disabled = true;
    setI18nText(nextBtn, 'onboarding.saving');

    postConfig(config)
    .then(function(data) {
        if (data.status === 'ok') {
            hideOnboarding();
            showRestartOverlay(data.boot);
        } else if (data.pin_required) {
            // No PIN or a wrong one (e.g. changed meanwhile): ask, then save again
            nextBtn.disabled = false;
            setI18nText(nextBtn, 'onboarding.retry');
            showPinPrompt(saveConfig);
        } else {
            nextBtn.disabled = false;
            setI18nText(nextBtn, 'onboarding.retry');
            alert(t('common.error', { message: data.error || t('onboarding.unknown_error') }));
        }
    })
    .catch(function(e) {
        nextBtn.disabled = false;
        setI18nText(nextBtn, 'onboarding.retry');
        // No answer from the server (network error, timeout): say so in words
        var noAnswer = e && (e.name === 'TypeError' || e.name === 'AbortError');
        alert(t('onboarding.save_failed', { message: noAnswer ? t('status.connection_failed') : e }));
    });
}

// POST the settings. A request that gets no answer at all (a Wi-Fi blip, or a
// kept-alive connection the server closed just then) is sent once more after a
// second; saving twice is harmless, the same .env is written again.
var SAVE_CONFIG_RETRY_MS = 1000;

function postConfig(config, retried) {
    return settingsFetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
    })
    .then(function(r) { return r.json(); })
    .catch(function(e) {
        if (retried || !e || (e.name !== 'TypeError' && e.name !== 'AbortError')) throw e;
        return new Promise(function(resolve) { setTimeout(resolve, SAVE_CONFIG_RETRY_MS); })
            .then(function() { return postConfig(config, true); });
    });
}

// Wait for the restarted server (a new boot id; the old process answers for
// up to 2 s after saving), then reload the page
function showRestartOverlay(oldBoot) {
    var overlay = document.getElementById('restart-overlay');
    overlay.classList.remove('hidden');
    var textEl = document.getElementById('restart-text');
    var startTime = Date.now();

    var pollInterval = setInterval(function() {
        apiFetch('/api/config/status')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.configured && !(oldBoot && data.boot === oldBoot)) {
                    clearInterval(pollInterval);
                    setI18nText(textEl, 'onboarding.ready');
                    setTimeout(function() { window.location.reload(); }, 500);
                }
            })
            .catch(function() {
                // Server still restarting
                if (Date.now() - startTime > 30000) {
                    setI18nText(textEl, 'onboarding.taking_longer');
                }
            });
    }, 2000);
}

function openConfigEditor() {
    closeSettingsPanel();
    showOnboarding(true);
}

function resetConfiguration() {
    if (!confirm(t('settings.reset_confirm'))) return;
    if (!confirm(t('settings.reset_confirm_again'))) return;

    settingsFetch('/api/config/reset', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'ok') {
                document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text-muted);font-size:1.2rem;">' + t('settings.restarting') + '</div>';
                setTimeout(function() { location.reload(); }, 4000);
            }
        })
        .catch(function() {
            alert(t('settings.reset_failed'));
        });
}

// Check the first run on page load.
checkFirstRun();

// ===== CARD ORDER =====
// Cards are reordered by dragging them by the header: with a mouse right away,
// by touch after a short press (a quick swipe on a header still scrolls the
// page). The order is kept per device in localStorage.
var CARD_ORDER_KEY = 'dashboard-cards-order';
var CARD_DRAG_TOUCH_DELAY_MS = 250;
var CARD_DRAG_MOUSE_START_PX = 5;   // mouse: drag starts after moving this far
var CARD_DRAG_TOUCH_SLOP_PX = 10;   // touch: moving this far before the delay is a scroll
var CARD_DRAG_EDGE_PX = 60;         // auto-scroll this close to the top / bottom of the screen
var CARD_MOVE_MS = 150;

function dashboardCards(container) {
    return Array.prototype.filter.call(container.children, function(el) {
        return el.classList.contains('card') && el.getAttribute('data-id');
    });
}

function loadCardOrder() {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(CARD_ORDER_KEY)); } catch (e) {}
    if (!Array.isArray(saved)) return [];
    // "Planned outages" replaced "Outage history"; "Consumption" replaced
    // "Home consumption" + "Load per phase": they keep the old cards' positions
    var renamed = { 'outage-history-card': 'planned-outages-card', 'load-card': 'consumption-card' };
    return saved.map(function(id) { return renamed[id] || id; })
        .filter(function(id) { return id !== 'phase-card'; });
}

function saveCardOrder(container) {
    var order = dashboardCards(container).map(function(card) { return card.getAttribute('data-id'); });
    try { localStorage.setItem(CARD_ORDER_KEY, JSON.stringify(order)); } catch (e) {}
}

// Saved cards first, in their saved order; cards added since then go last
function applyCardOrder(container) {
    var saved = loadCardOrder();
    if (!saved.length) return;
    var cards = dashboardCards(container);
    var byId = {};
    cards.forEach(function(card) { byId[card.getAttribute('data-id')] = card; });
    var placed = {};
    saved.concat(cards.map(function(card) { return card.getAttribute('data-id'); })).forEach(function(id) {
        if (!byId[id] || placed[id]) return;
        placed[id] = true;
        container.appendChild(byId[id]);
    });
}

// Moves cards with change(), then slides them from where they were (FLIP);
// no animation in low power / reduced motion mode (CSS disables transitions)
function animateCards(cards, change) {
    var before = cards.map(function(card) { return card.getBoundingClientRect(); });
    change();
    cards.forEach(function(card, i) {
        card.style.transition = 'none';
        card.style.transform = '';
        var after = card.getBoundingClientRect();
        var dx = before[i].left - after.left, dy = before[i].top - after.top;
        if (!dx && !dy) return;
        card.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
        void card.offsetWidth;  // apply the start position before the transition
        card.style.transition = 'transform ' + CARD_MOVE_MS + 'ms ease';
        card.style.transform = '';
    });
}

function initCardDrag(container) {
    var drag = null;
    var scrollFrame = null;

    function inside(el, x, y) {
        var r = el.getBoundingClientRect();
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    }

    // Keep the dragged card under the pointer, wherever its slot is now
    function follow() {
        var card = drag.card;
        card.style.transform = '';
        var slot = card.getBoundingClientRect();
        card.style.transform = 'translate(' + (drag.x - drag.grabX - slot.left) + 'px, ' +
            (drag.y - drag.grabY - slot.top) + 'px)';
    }

    function startDrag() {
        drag.timer = null;
        drag.active = true;
        var rect = drag.card.getBoundingClientRect();
        drag.grabX = drag.x - rect.left;
        drag.grabY = drag.y - rect.top;
        drag.card.style.transition = 'none';
        drag.card.classList.add('card-dragging');
        document.body.classList.add('cards-dragging');
        follow();
    }

    // Over another card: move the dragged one to its place. The card it just
    // traded places with is ignored until the pointer leaves it, so cards of
    // different sizes don't swap back and forth.
    function reorder() {
        if (drag.lastTarget && inside(drag.lastTarget, drag.x, drag.y)) return;
        drag.lastTarget = null;
        var el = document.elementFromPoint(drag.x, drag.y);
        var target = el && el.closest('.card');
        if (!target || target === drag.card || target.parentElement !== container) return;
        drag.lastTarget = target;
        var cards = dashboardCards(container);
        var forward = cards.indexOf(target) > cards.indexOf(drag.card);
        var others = cards.filter(function(card) { return card !== drag.card && card.offsetParent; });
        animateCards(others, function() {
            container.insertBefore(drag.card, forward ? target.nextSibling : target);
        });
        follow();
    }

    function autoScroll() {
        if (scrollFrame) return;
        scrollFrame = requestAnimationFrame(function step() {
            scrollFrame = null;
            if (!drag || !drag.active) return;
            var bottom = window.innerHeight - CARD_DRAG_EDGE_PX, speed = 0;
            if (drag.y < CARD_DRAG_EDGE_PX) speed = -Math.ceil((CARD_DRAG_EDGE_PX - drag.y) / 3);
            else if (drag.y > bottom) speed = Math.ceil((drag.y - bottom) / 3);
            var scrolled = window.scrollY;
            if (speed) window.scrollBy(0, speed);
            if (window.scrollY === scrolled) return;  // not near an edge, or the page ends here
            follow();
            reorder();
            scrollFrame = requestAnimationFrame(step);
        });
    }

    function endDrag() {
        if (!drag) return;
        var d = drag;
        drag = null;
        clearTimeout(d.timer);
        if (!d.active) return;
        document.body.classList.remove('cards-dragging');
        // Settle into the slot from where it was dropped
        animateCards([d.card], function() { d.card.classList.remove('card-dragging'); });
        setTimeout(function() {
            dashboardCards(container).forEach(function(card) { card.style.transition = ''; });
        }, CARD_MOVE_MS + 50);
        saveCardOrder(container);
    }

    container.addEventListener('pointerdown', function(e) {
        if (drag || (e.pointerType === 'mouse' && e.button !== 0)) return;
        var header = e.target.closest('.card-header');
        var card = header && header.parentElement;
        if (!card || card.parentElement !== container || !card.classList.contains('card')) return;
        if (e.target.closest('button, a, input, select, textarea, label')) return;
        drag = { card: card, pointerId: e.pointerId, touch: e.pointerType !== 'mouse',
                 startX: e.clientX, startY: e.clientY, x: e.clientX, y: e.clientY,
                 active: false, timer: null, lastTarget: null };
        if (drag.touch) drag.timer = setTimeout(startDrag, CARD_DRAG_TOUCH_DELAY_MS);
        else e.preventDefault();  // no text selection while dragging with the mouse
    });

    document.addEventListener('pointermove', function(e) {
        if (!drag || e.pointerId !== drag.pointerId) return;
        drag.x = e.clientX;
        drag.y = e.clientY;
        if (!drag.active) {
            var moved = Math.max(Math.abs(drag.x - drag.startX), Math.abs(drag.y - drag.startY));
            if (drag.touch) {
                if (moved > CARD_DRAG_TOUCH_SLOP_PX) endDrag();  // a swipe: the page scrolls
                return;
            }
            if (moved < CARD_DRAG_MOUSE_START_PX) return;
            startDrag();
        }
        follow();
        reorder();
        autoScroll();
    });

    document.addEventListener('pointerup', function(e) {
        if (drag && e.pointerId === drag.pointerId) endDrag();
    });
    // The browser took the touch over (e.g. to scroll), or the window lost focus
    document.addEventListener('pointercancel', function(e) {
        if (drag && e.pointerId === drag.pointerId) endDrag();
    });
    window.addEventListener('blur', endDrag);

    // While a card is dragged by touch the page must not scroll, and a long
    // press must not open the context menu. Listening only on the headers keeps
    // scrolling everywhere else free of non-passive listeners.
    dashboardCards(container).forEach(function(card) {
        var header = card.querySelector(':scope > .card-header');
        if (!header) return;
        header.addEventListener('touchmove', function(e) {
            if (drag && drag.active) e.preventDefault();
        }, { passive: false });
    });
    container.addEventListener('contextmenu', function(e) {
        if (drag) e.preventDefault();
    });
}

// ===== CARD LAYOUT =====
// Per device: the cards the viewer switched off, and the cards shown wide (two
// columns where the screen has room, see .card-wide). Set in Settings → Cards.
// A switched-off card stays hidden on top of the dashboard's own rules (no
// generator, no schedule...), which hide cards with an inline display: none
// or a class.
var CARD_HIDDEN_KEY = 'dashboard-cards-hidden';
var CARD_WIDE_KEY = 'dashboard-cards-wide';
var WIDE_ICON = '<svg viewBox="0 0 24 24"><path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg>';

function loadCardList(key) {
    var list = null;
    try { list = JSON.parse(localStorage.getItem(key)); } catch (e) {}
    return Array.isArray(list) ? list.filter(function(id) { return typeof id === 'string'; }) : [];
}

var hiddenCards = loadCardList(CARD_HIDDEN_KEY);
var wideCards = loadCardList(CARD_WIDE_KEY);

// The "Right now" strip and the cards, in page order
function layoutItems() {
    var container = document.querySelector('.dashboard');
    return container ? Array.prototype.filter.call(container.children, function(el) {
        return el.getAttribute('data-id');
    }) : [];
}

function applyCardLayout() {
    layoutItems().forEach(function(el) {
        var id = el.getAttribute('data-id');
        el.classList.toggle('card-user-hidden', hiddenCards.indexOf(id) !== -1);
        el.classList.toggle('card-wide', el.classList.contains('card') && wideCards.indexOf(id) !== -1);
    });
}

function toggleCardSetting(list, key, id) {
    var i = list.indexOf(id);
    if (i === -1) list.push(id); else list.splice(i, 1);
    try { localStorage.setItem(key, JSON.stringify(list)); } catch (e) {}
    applyCardLayout();
    renderCardSettings();
}

function cardTitle(el) {
    var keyEl = el.matches('.flow-strip') ? el.querySelector('.flow-title')
        : el.querySelector('.card-header h2[data-i18n], .card-header h2 [data-i18n]');
    return keyEl ? t(keyEl.getAttribute('data-i18n')) : el.getAttribute('data-id');
}

// One row per card the dashboard shows now (or the viewer switched off):
// name, wide button (not for the strip, it's always full width), on/off switch
function renderCardSettings() {
    var box = document.getElementById('card-settings');
    if (!box) return;
    box.innerHTML = layoutItems().filter(function(el) {
        return hiddenCards.indexOf(el.getAttribute('data-id')) !== -1 || getComputedStyle(el).display !== 'none';
    }).map(function(el) {
        var id = el.getAttribute('data-id');
        var name = cardTitle(el);
        var shown = hiddenCards.indexOf(id) === -1;
        var wide = wideCards.indexOf(id) !== -1;
        var wideBtn = el.classList.contains('card')
            ? '<button type="button" class="card-wide-btn' + (wide ? ' on' : '') + '" data-card-wide="' + escapeHtml(id) + '"' +
              ' aria-pressed="' + wide + '" aria-label="' + escapeHtml(t('settings.card_wide', { name: name })) + '"' +
              ' title="' + escapeHtml(t('settings.card_wide', { name: name })) + '">' + WIDE_ICON + '</button>'
            : '';
        return '<div class="card-setting">' +
            '<span class="card-setting-name">' + escapeHtml(name) + '</span>' + wideBtn +
            '<button type="button" class="onboarding-toggle' + (shown ? ' on' : '') + '" role="switch" data-card-show="' + escapeHtml(id) + '"' +
            ' aria-checked="' + shown + '" aria-label="' + escapeHtml(t('settings.card_show', { name: name })) + '">' +
            '<span class="toggle-knob"></span></button></div>';
    }).join('');
}

(function() {
    var box = document.getElementById('card-settings');
    if (!box) return;
    box.addEventListener('click', function(e) {
        var btn = e.target.closest('button');
        if (!btn) return;
        if (btn.hasAttribute('data-card-show')) toggleCardSetting(hiddenCards, CARD_HIDDEN_KEY, btn.getAttribute('data-card-show'));
        if (btn.hasAttribute('data-card-wide')) toggleCardSetting(wideCards, CARD_WIDE_KEY, btn.getAttribute('data-card-wide'));
    });
})();

(function() {
    var container = document.querySelector('.dashboard');
    if (!container) return;
    applyCardOrder(container);
    applyCardLayout();
    initCardDrag(container);
})();
