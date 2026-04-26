/* ── Admin Dashboard ─────────────────────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const _sectionLabels = {
    noticias: "Noticias",
    metricas: "Métricas de Agenda",
    semana: "Resumen Semanal",
    importante: "Noticia del Día",
    temas: "Temas",
    nube: "Nube de Palabras",
};

const _featureLabels = {
    group_click: "Lectura de noticia",
    ai_search: "Búsqueda IA",
    filter_change: "Cambio de filtro",
    topic_click: "Click en tema",
    login: "Login",
};

const _barColors = ["blue", "teal", "purple", "orange", "pink"];

let _dateRange = "7d";
let _activeTab = "metrics";
let _activeSubtab = "logged";
let _aiMonitorTimer = null;

let _aiInvocationsPage = 1;
let _processEventsPage = 1;
const _PAGE_SIZE = 25;

document.addEventListener("DOMContentLoaded", async () => {
    const resp = await fetch("/auth/me");
    const data = await resp.json();
    if (!data.user || data.user.role !== "admin") {
        window.location.href = "/";
        return;
    }

    setupTabs();
    setupSubtabs();
    setupDateFilters();
    setupLogFilters();
    setupInfraCostsRefresh();
    loadAll();
});

function setupInfraCostsRefresh() {
    const btn = $("#infra-costs-refresh");
    if (btn) btn.addEventListener("click", refreshInfraCostsNow);
    const logsBtn = $("#ollama-logs-refresh");
    if (logsBtn) logsBtn.addEventListener("click", loadOllamaLogs);
    const logsFilter = $("#ollama-logs-filter");
    if (logsFilter) {
        logsFilter.addEventListener("keydown", (e) => {
            if (e.key === "Enter") loadOllamaLogs();
        });
    }
}

function setupTabs() {
    $$("#admin-tabs .admin-tab").forEach(tab => {
        tab.addEventListener("click", () => {
            $$("#admin-tabs .admin-tab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            _activeTab = tab.dataset.tab;
            $$(".admin-tab-panel").forEach(p => p.classList.remove("active"));
            const panel = $(`#panel-${_activeTab}`);
            if (panel) panel.classList.add("active");
            loadAll();
        });
    });
}

function setupSubtabs() {
    $$("#admin-subtabs .admin-subtab").forEach(tab => {
        tab.addEventListener("click", () => {
            $$("#admin-subtabs .admin-subtab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            _activeSubtab = tab.dataset.subtab;
            $$("#panel-metrics .admin-subtab-panel").forEach(p => p.classList.remove("active"));
            const panel = $(`#subpanel-${_activeSubtab}`);
            if (panel) panel.classList.add("active");
            loadAll();
        });
    });
}

function setupLogFilters() {
    const applyInv = $("#ai-inv-apply");
    if (applyInv) {
        applyInv.addEventListener("click", () => { _aiInvocationsPage = 1; loadAIInvocations(); });
    }
    const applyProc = $("#proc-apply");
    if (applyProc) {
        applyProc.addEventListener("click", () => { _processEventsPage = 1; loadProcessEvents(); });
    }
}

function setupDateFilters() {
    $$("#admin-date-filters .admin-date-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            $$("#admin-date-filters .admin-date-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            _dateRange = btn.dataset.range;
            loadAll();
        });
    });
}

function computeRange(range) {
    const now = new Date();
    const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const today = fmt(now);
    switch (range) {
        case "hoy":
            return { desde: today, hasta: today };
        case "2d": {
            const d = new Date(now);
            d.setDate(d.getDate() - 1);
            return { desde: fmt(d), hasta: today };
        }
        case "7d": {
            const d = new Date(now);
            d.setDate(d.getDate() - 6);
            return { desde: fmt(d), hasta: today };
        }
        case "30d": {
            const d = new Date(now);
            d.setDate(d.getDate() - 29);
            return { desde: fmt(d), hasta: today };
        }
        case "mes": {
            const d = new Date(now.getFullYear(), now.getMonth(), 1);
            return { desde: fmt(d), hasta: today };
        }
        default:
            return { desde: null, hasta: null };
    }
}

function qs(desde, hasta) {
    const p = new URLSearchParams();
    if (desde) p.set("desde", desde);
    if (hasta) p.set("hasta", hasta);
    return p.toString() ? `?${p}` : "";
}

function loadAll() {
    const { desde, hasta } = computeRange(_dateRange);
    if (_activeTab === "metrics") {
        if (_activeSubtab === "logged") {
            loadDashboard(desde, hasta);
            loadTopContent();
            loadSearches();
            loadHourly(desde, hasta);
            loadDaily(desde, hasta);
            loadUsers();
        } else {
            loadAnonymousDashboard(desde, hasta);
        }
    } else if (_activeTab === "logs") {
        loadAIMonitor();
        loadAIInvocations();
        loadProcessEvents();
    } else if (_activeTab === "admin") {
        loadAIConfig();
        loadSchedulerConfig();
        loadOllamaTimeout();
        loadAILimits();
        loadInfraLimits();
    } else if (_activeTab === "costs") {
        loadAICosts(desde, hasta);
        loadInfraCosts();
    } else if (_activeTab === "x") {
        loadXStatus();
        loadXCampaigns();
        loadXUsage();
    }
    updateAIMonitorTimer();
}

function updateAIMonitorTimer() {
    if (_aiMonitorTimer) {
        clearInterval(_aiMonitorTimer);
        _aiMonitorTimer = null;
    }
    if (_activeTab === "logs") {
        _aiMonitorTimer = setInterval(loadAIMonitor, 20000);
    }
}

// ── Dashboard (KPIs + engagement + sections + features) ─────────────────

async function loadDashboard(desde, hasta) {
    try {
        const resp = await fetch(`/api/admin/dashboard${qs(desde, hasta)}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();

        const u = data.usage;
        const e = data.engagement;

        // KPIs
        $("#kpi-users").textContent = data.total_users;
        $("#kpi-users-sub").textContent = `${u.unique_users} activos`;
        $("#kpi-sessions").textContent = u.unique_sessions;
        $("#kpi-pageviews").textContent = u.page_views;
        $("#kpi-events").textContent = u.total_events;

        // Engagement
        $("#eng-duration").textContent = formatDuration(e.avg_duration_seconds);
        $("#eng-pages").textContent = e.avg_pages_per_session;
        $("#eng-bounce").textContent = `${e.bounce_rate}%`;
        $("#eng-events-session").textContent = e.avg_events_per_session;

        renderBarChart("sections-chart", data.sections.map(s => ({
            label: _sectionLabels[s.section] || s.section,
            count: s.count,
        })), "teal");

        renderBarChart("features-chart", data.features.map(f => ({
            label: _featureLabels[f.feature] || f.feature,
            count: f.count,
        })), "blue");
    } catch (err) {
        console.error("Dashboard load failed:", err);
    }
}

// ── Top content ─────────────────────────────────────────────────────────

async function loadTopContent() {
    try {
        const resp = await fetch("/api/admin/top-content?limit=10");
        if (resp.status === 403) return;
        const data = await resp.json();

        const container = $("#top-content");
        if (!data.content || !data.content.length) {
            container.innerHTML = `<div class="admin-empty">No hay datos de lectura aún</div>`;
            return;
        }
        renderBarChart("top-content", data.content.map(c => ({
            label: c.title,
            count: c.count,
        })), "purple");
    } catch (err) {
        console.error("Top content load failed:", err);
    }
}

// ── Searches ────────────────────────────────────────────────────────────

async function loadSearches() {
    try {
        const resp = await fetch("/api/admin/popular-searches?limit=10");
        if (resp.status === 403) return;
        const data = await resp.json();

        const container = $("#searches-list");
        if (!data.searches || !data.searches.length) {
            container.innerHTML = `<div class="admin-empty">No hay búsquedas registradas aún</div>`;
            return;
        }
        renderBarChart("searches-list", data.searches.map(s => ({
            label: `"${s.query}"`,
            count: s.count,
        })), "orange");
    } catch (err) {
        console.error("Searches load failed:", err);
    }
}

// ── Hourly distribution ─────────────────────────────────────────────────

async function loadHourly(desde, hasta) {
    try {
        const resp = await fetch(`/api/admin/hourly${qs(desde, hasta)}`);
        if (resp.status === 403) return;
        const data = await resp.json();

        const container = $("#hourly-chart");
        const hours = data.hours || [];
        if (!hours.length) {
            container.innerHTML = `<div class="admin-empty">No hay datos horarios aún</div>`;
            return;
        }

        const byHour = new Array(24).fill(0);
        hours.forEach(h => { byHour[h.hour] = h.events; });
        const max = Math.max(...byHour, 1);

        const bars = byHour.map((v, i) => {
            const pct = (v / max) * 100;
            const title = `${String(i).padStart(2, "0")}:00 — ${v} eventos`;
            return `<div class="admin-hour-bar" style="height:${Math.max(pct, 2)}%" title="${title}"></div>`;
        }).join("");

        const labels = [0, 3, 6, 9, 12, 15, 18, 21].map(h =>
            `<span style="position:absolute;left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</span>`
        ).join("");

        container.innerHTML = `
            <div class="admin-hourly">${bars}</div>
            <div style="position:relative;height:14px;margin-top:2px;font-size:0.6rem;color:var(--text-dim)">${labels}</div>
        `;
    } catch (err) {
        console.error("Hourly load failed:", err);
    }
}

// ── Daily activity ──────────────────────────────────────────────────────

async function loadDaily(desde, hasta) {
    try {
        const resp = await fetch(`/api/admin/daily-activity${qs(desde, hasta)}`);
        if (resp.status === 403) return;
        const data = await resp.json();

        const container = $("#daily-table-wrap");
        if (!data.days || !data.days.length) {
            container.innerHTML = `<div class="admin-empty">No hay datos de actividad diaria aún</div>`;
            return;
        }

        const rows = data.days.slice(0, 30).map(d => `
            <tr>
                <td>${escHtml(formatDay(d.day))}</td>
                <td>${d.sessions}</td>
                <td>${d.users}</td>
                <td>${d.page_views}</td>
                <td>${d.events}</td>
            </tr>
        `).join("");

        container.innerHTML = `
            <table class="admin-table">
                <thead><tr><th>Día</th><th>Sesiones</th><th>Usuarios</th><th>Vistas</th><th>Eventos</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
    } catch (err) {
        console.error("Daily load failed:", err);
    }
}

// ── Users ───────────────────────────────────────────────────────────────

async function loadUsers() {
    try {
        const resp = await fetch("/api/admin/users?limit=50");
        if (resp.status === 403) return;
        const data = await resp.json();

        const container = $("#users-table-wrap");
        if (!data.users || !data.users.length) {
            container.innerHTML = `<div class="admin-empty">No hay usuarios registrados aún</div>`;
            return;
        }

        const rows = data.users.map(u => `
            <tr>
                <td>${escHtml(u.name || "-")}</td>
                <td>${escHtml(u.email)}</td>
                <td><span class="admin-badge ${u.role === "admin" ? "admin-badge-admin" : "admin-badge-user"}">${u.role}</span></td>
                <td>${formatDatetime(u.last_login_at)}</td>
                <td>${formatDatetime(u.created_at)}</td>
            </tr>
        `).join("");

        container.innerHTML = `
            <table class="admin-table">
                <thead><tr><th>Nombre</th><th>Email</th><th>Rol</th><th>Último login</th><th>Registrado</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
    } catch (err) {
        console.error("Users load failed:", err);
    }
}

// ── Anonymous dashboard ─────────────────────────────────────────────────

async function loadAnonymousDashboard(desde, hasta) {
    try {
        const resp = await fetch(`/api/admin/anonymous${qs(desde, hasta)}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        const d = await resp.json();

        const o = d.overview;
        const e = d.engagement;

        // KPIs
        $("#anon-kpi-visitors").textContent = o.unique_visitors;
        $("#anon-kpi-sessions").textContent = o.unique_sessions;
        $("#anon-kpi-pageviews").textContent = o.page_views;
        $("#anon-kpi-events").textContent = o.total_events;

        // Ratio + engagement
        const ratio = o.anon_ratio;
        $("#anon-eng-ratio").textContent = `${ratio}%`;
        const ratioEl = $("#anon-eng-ratio");
        ratioEl.className = "admin-eng-value";

        $("#anon-eng-duration").textContent = formatDuration(e.avg_duration_seconds);
        $("#anon-eng-pages").textContent = e.avg_pages_per_session;
        $("#anon-eng-bounce").textContent = `${e.bounce_rate}%`;

        // Sections
        renderBarChart("anon-sections-chart", (d.sections || []).map(s => ({
            label: _sectionLabels[s.section] || s.section,
            count: s.count,
        })), "teal");

        // Features
        renderBarChart("anon-features-chart", (d.features || []).map(f => ({
            label: _featureLabels[f.feature] || f.feature,
            count: f.count,
        })), "blue");

        // Top content
        renderBarChart("anon-top-content", (d.top_content || []).map(c => ({
            label: c.title,
            count: c.count,
        })), "purple");

        // Searches
        renderBarChart("anon-searches-list", (d.searches || []).map(s => ({
            label: `"${s.query}"`,
            count: s.count,
        })), "orange");

        // Hourly
        renderAnonHourly(d.hourly || []);

        // Daily
        renderAnonDaily(d.daily || []);

        // Top visitors
        renderAnonVisitors(d.top_visitors || []);
    } catch (err) {
        console.error("Anonymous dashboard load failed:", err);
    }
}

function renderAnonHourly(hours) {
    const container = $("#anon-hourly-chart");
    if (!hours.length) {
        container.innerHTML = `<div class="admin-empty">No hay datos horarios aún</div>`;
        return;
    }
    const byHour = new Array(24).fill(0);
    hours.forEach(h => { byHour[h.hour] = h.events; });
    const max = Math.max(...byHour, 1);

    const bars = byHour.map((v, i) => {
        const pct = (v / max) * 100;
        const title = `${String(i).padStart(2, "0")}:00 — ${v} eventos`;
        return `<div class="admin-hour-bar" style="height:${Math.max(pct, 2)}%" title="${title}"></div>`;
    }).join("");

    const labels = [0, 3, 6, 9, 12, 15, 18, 21].map(h =>
        `<span style="position:absolute;left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</span>`
    ).join("");

    container.innerHTML = `
        <div class="admin-hourly">${bars}</div>
        <div style="position:relative;height:14px;margin-top:2px;font-size:0.6rem;color:var(--text-dim)">${labels}</div>
    `;
}

function renderAnonDaily(days) {
    const container = $("#anon-daily-table-wrap");
    if (!days.length) {
        container.innerHTML = `<div class="admin-empty">No hay datos de actividad diaria aún</div>`;
        return;
    }
    const rows = days.slice(0, 30).map(d => `
        <tr>
            <td>${escHtml(formatDay(d.day))}</td>
            <td>${d.visitors}</td>
            <td>${d.sessions}</td>
            <td>${d.page_views}</td>
            <td>${d.events}</td>
        </tr>
    `).join("");

    container.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Día</th><th>Visitantes (IP)</th><th>Sesiones</th><th>Vistas</th><th>Eventos</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
}

function renderAnonVisitors(visitors) {
    const container = $("#anon-visitors-table-wrap");
    visitors = visitors.filter(v => v.ip && v.ip.trim());
    if (!visitors.length) {
        container.innerHTML = `<div class="admin-empty">No hay datos de visitantes aún</div>`;
        return;
    }
    const rows = visitors.slice(0, 20).map(v => `
        <tr>
            <td><code style="font-size:0.78rem">${escHtml(v.ip)}</code></td>
            <td>${v.sessions}</td>
            <td>${v.events}</td>
            <td>${formatDatetime(v.last_seen)}</td>
        </tr>
    `).join("");

    container.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>IP</th><th>Sesiones</th><th>Eventos</th><th>Última actividad</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
}

// ── AI dashboard ────────────────────────────────────────────────────────

const _eventLabels = {
    search: "Búsqueda manual",
    search_prefetch: "Búsqueda automática",
    topics: "Temas del día",
    weekly_summary: "Resumen semanal",
    top_story: "Noticia del día",
};

const _eventDescs = {
    search: "Cuando un usuario busca un tema manualmente",
    search_prefetch: "Búsqueda automática por cada tema para que cargue al instante",
    topics: "Genera los 6 temas destacados a partir de las noticias",
    weekly_summary: "Arma el resumen con lo más importante de la semana",
    top_story: "Elige y redacta la noticia más importante del día",
};

const _providerLabels = {
    gemini: "Gemini",
    groq: "Groq",
    ollama: "Ollama",
};

const _PROVIDER_SLOT_LABELS = [
    "Primer proveedor",
    "Si falla, 2do",
    "Si falla, 3ro",
    "Si falla, 4to",
];
const _MAX_PROVIDER_SLOTS = 4;

const _providerColors = {
    gemini: { bar: "#4088c7", bg: "rgba(64,136,199,0.12)" },
    groq: { bar: "#0d9488", bg: "rgba(13,148,136,0.12)" },
    ollama: { bar: "#a855f7", bg: "rgba(168,85,247,0.12)" },
};

function _providerBadgeClass(provider) {
    if (provider === "gemini") return "admin-badge-admin";
    if (provider === "ollama") return "admin-badge-ollama";
    return "admin-badge-user";
}

function fmtUSD(v) {
    if (v == null) return "$0.00";
    return "$" + Number(v).toFixed(v < 0.01 ? 4 : 2);
}

function fmtTokens(n) {
    if (!n) return "0";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
    return String(n);
}

// Inclusive day count between `desde`/`hasta` (YYYY-MM-DD). Returns days in
// the current month when the range is open (e.g. "Todo" filter) so the
// infra prorate keeps the same magnitude as the cumulative month figure.
function _periodDaysBetween(desde, hasta) {
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    if (!desde || !hasta) return daysInMonth;
    const d = new Date(desde + "T00:00:00");
    const h = new Date(hasta + "T00:00:00");
    if (isNaN(d) || isNaN(h)) return daysInMonth;
    const diff = Math.round((h - d) / 86_400_000) + 1;
    return diff > 0 ? diff : daysInMonth;
}

async function loadAICosts(desde, hasta) {
    try {
        const now = new Date();
        const mesDesde = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
        const mesHasta = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

        const [resp, respMes, respInfra] = await Promise.all([
            fetch(`/api/admin/ai-cost${qs(desde, hasta)}`),
            fetch(`/api/admin/ai-cost${qs(mesDesde, mesHasta)}`),
            fetch(`/api/admin/infra-costs`).catch(() => null),
        ]);
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();
        const dataMes = await respMes.json();
        let infraMonth = 0;
        let infraAvailable = false;
        if (respInfra && respInfra.ok) {
            try {
                const infraData = await respInfra.json();
                if (infraData && infraData.available) {
                    infraMonth = Number(infraData.total_usd_month) || 0;
                    infraAvailable = true;
                }
            } catch (_) { /* ignore */ }
        }

        const s = data.summary;
        const t = s.totals;

        $("#ai-kpi-calls").textContent = t.calls;
        $("#ai-kpi-tokens-in").textContent = fmtTokens(t.input_tokens);
        $("#ai-kpi-tokens-out").textContent = fmtTokens(t.output_tokens);

        const mesTotals = dataMes.summary.totals;
        const costMes = mesTotals.cost_total || 0;
        const daysWithData = mesTotals.distinct_days || 0;
        const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
        const projectedAI = daysWithData > 0 ? (costMes / daysWithData) * daysInMonth : 0;

        // Pro-rate the Railway monthly estimate to the selected period so the
        // "Costo (USD)" KPI mixes apples with apples (AI period cost + infra
        // share for the same span). Falls back to month-to-date when we can't
        // determine the period length.
        const periodDays = _periodDaysBetween(desde, hasta);
        const infraPeriod = infraAvailable && periodDays > 0
            ? infraMonth * (periodDays / daysInMonth)
            : 0;

        const aiCostPeriod = Number(t.cost_total) || 0;
        const totalCostPeriod = aiCostPeriod + infraPeriod;
        $("#ai-kpi-cost").textContent = fmtUSD(totalCostPeriod);
        const costSub = $("#ai-kpi-cost-sub");
        if (costSub) {
            costSub.innerHTML = infraAvailable
                ? `IA ${fmtUSD(aiCostPeriod)} <span style="opacity:.5">·</span> Infra ${fmtUSD(infraPeriod)}`
                : `IA ${fmtUSD(aiCostPeriod)} <span style="opacity:.5">·</span> <span style="opacity:.5">infra n/d</span>`;
        }

        const totalProjection = projectedAI + (infraAvailable ? infraMonth : 0);
        $("#ai-kpi-projection").textContent = fmtUSD(totalProjection);
        const projSub = $("#ai-kpi-projection-sub");
        if (projSub) {
            projSub.innerHTML = infraAvailable
                ? `IA ${fmtUSD(projectedAI)} <span style="opacity:.5">·</span> Infra ${fmtUSD(infraMonth)}`
                : `IA ${fmtUSD(projectedAI)} <span style="opacity:.5">·</span> <span style="opacity:.5">infra n/d</span>`;
        }

        const strip = $("#ai-providers-strip");
        if (s.by_provider.length) {
            strip.innerHTML = s.by_provider.map(p => {
                const scheme = _providerColors[p.provider] || { bar: "#64748b", bg: "rgba(100,116,139,0.12)" };
                const color = scheme.bar;
                const bg = scheme.bg;
                return `
                <div class="admin-eng-card">
                    <div class="admin-eng-icon" style="background:${bg}">
                        <svg viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4v1a3 3 0 0 1 3 3v1a2 2 0 0 1-2 2h-1v3l-2-2h-4l-2 2v-3H7a2 2 0 0 1-2-2v-1a3 3 0 0 1 3-3V6a4 4 0 0 1 4-4z"/></svg>
                    </div>
                    <div class="admin-eng-data">
                        <div class="admin-eng-value">${fmtUSD(p.cost_total)}</div>
                        <div class="admin-eng-label">${escHtml(p.provider)} (${p.calls} calls)</div>
                        <div class="admin-eng-tokens" style="display:flex;gap:10px;margin-top:4px;font-size:.78rem;color:#94a3b8">
                            <span title="Tokens entrada">▲ ${fmtTokens(p.input_tokens)}</span>
                            <span title="Tokens salida">▼ ${fmtTokens(p.output_tokens)}</span>
                        </div>
                    </div>
                </div>`;
            }).join("");
        } else {
            strip.innerHTML = "";
        }

        renderAIEventTable(s.by_event);
        renderAIDailyTable(data.daily || []);
    } catch (err) {
        console.error("AI costs load failed:", err);
    }
}

function renderAIEventTable(events) {
    const container = $("#ai-event-table-wrap");
    if (!events || !events.length) {
        container.innerHTML = `<div class="admin-empty">No hay llamadas IA registradas aún</div>`;
        return;
    }

    let totCalls = 0, totIn = 0, totOut = 0, totCostIn = 0, totCostOut = 0, totCost = 0;
    const rows = events.map(e => {
        totCalls += e.calls;
        totIn += e.input_tokens;
        totOut += e.output_tokens;
        totCostIn += e.cost_input;
        totCostOut += e.cost_output;
        totCost += e.cost_total;
        const desc = _eventDescs[e.event_type] || "";
        return `<tr>
            <td title="${escHtml(desc)}">
                <div>${escHtml(_eventLabels[e.event_type] || e.event_type)}</div>
                <div class="admin-cell-desc">${escHtml(desc)}</div>
            </td>
            <td><span class="admin-badge ${_providerBadgeClass(e.provider)}">${escHtml(e.provider)}</span></td>
            <td>${e.calls}</td>
            <td>${fmtTokens(e.input_tokens)}</td>
            <td>${fmtTokens(e.output_tokens)}</td>
            <td>${fmtUSD(e.cost_input)}</td>
            <td>${fmtUSD(e.cost_output)}</td>
            <td style="font-weight:600">${fmtUSD(e.cost_total)}</td>
        </tr>`;
    }).join("");

    const footer = `<tr style="border-top:2px solid var(--border);font-weight:700">
        <td colspan="2">TOTAL</td>
        <td>${totCalls}</td>
        <td>${fmtTokens(totIn)}</td>
        <td>${fmtTokens(totOut)}</td>
        <td>${fmtUSD(totCostIn)}</td>
        <td>${fmtUSD(totCostOut)}</td>
        <td>${fmtUSD(totCost)}</td>
    </tr>`;

    container.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Evento</th><th>Provider</th><th>Calls</th><th>Input tok</th><th>Output tok</th><th>Costo in</th><th>Costo out</th><th>Total</th></tr></thead>
            <tbody>${rows}${footer}</tbody>
        </table>`;
}

function renderAIDailyTable(days) {
    const container = $("#ai-daily-table-wrap");
    if (!days || !days.length) {
        container.innerHTML = `<div class="admin-empty">No hay datos diarios aún</div>`;
        return;
    }
    const rows = days.slice(0, 30).map(d => `
        <tr>
            <td>${escHtml(formatDay(d.day))}</td>
            <td>${d.calls}</td>
            <td>${fmtTokens(d.input_tokens)}</td>
            <td>${fmtTokens(d.output_tokens)}</td>
            <td style="font-weight:600">${fmtUSD(d.cost_total)}</td>
        </tr>
    `).join("");

    container.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Día</th><th>Llamadas</th><th>Input tok</th><th>Output tok</th><th>Costo</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
}

async function loadAIConfig() {
    try {
        const resp = await fetch("/api/admin/ai-config");
        if (resp.status === 403) return;
        const data = await resp.json();
        renderAIConfig(data.config, data.valid_providers, data.valid_event_types, data.schedule || {});
    } catch (err) {
        console.error("AI config load failed:", err);
    }
}

function _buildHourOptions(selected) {
    const opts = ['<option value="">—</option>'];
    for (let h = 0; h < 24; h++) {
        const val = String(h).padStart(2, "0") + ":00";
        opts.push(`<option value="${val}" ${val === selected ? "selected" : ""}>${val}</option>`);
    }
    return opts.join("");
}

function _normalizeProviderChain(raw) {
    if (Array.isArray(raw)) {
        const seen = new Set();
        const out = [];
        for (const p of raw) {
            if (typeof p !== "string" || !p) continue;
            if (seen.has(p)) continue;
            seen.add(p);
            out.push(p);
        }
        return out;
    }
    if (typeof raw === "string" && raw) {
        if (raw.includes("_fallback_")) {
            const [primary, secondary] = raw.split("_fallback_");
            return [primary, secondary].filter(Boolean);
        }
        return [raw];
    }
    return [];
}

function _renderSlotSelect(et, slotIdx, chain, providers) {
    const value = chain[slotIdx] || "";
    const isFirstSlot = slotIdx === 0;
    const previous = chain.slice(0, slotIdx);
    const prevComplete = previous.length === slotIdx && previous.every(Boolean);
    const disabled = !isFirstSlot && (!prevComplete || previous.length >= providers.length);

    const opts = [];
    if (!isFirstSlot) {
        opts.push(`<option value="" ${value ? "" : "selected"}>(ninguno)</option>`);
    }
    for (const p of providers) {
        const usedEarlier = previous.includes(p);
        if (usedEarlier && p !== value) continue;
        const selected = p === value ? "selected" : "";
        opts.push(`<option value="${escHtml(p)}" ${selected}>${escHtml(_providerLabels[p] || p)}</option>`);
    }
    if (isFirstSlot && !value && providers.length) {
        // First slot must have a value; force selection of first option
        opts[0] = opts[0].replace("<option", "<option selected");
    }

    return `
        <div style="display:flex;flex-direction:column;gap:0.15rem">
            <div style="font-size:0.62rem;color:var(--text-dim)">${escHtml(_PROVIDER_SLOT_LABELS[slotIdx])}</div>
            <select class="ai-config-slot" data-event="${et}" data-slot="${slotIdx}" ${disabled ? "disabled" : ""}>${opts.join("")}</select>
        </div>`;
}

function _renderSlotsForEvent(et, chain, providers) {
    const slots = [];
    for (let i = 0; i < _MAX_PROVIDER_SLOTS; i++) {
        slots.push(_renderSlotSelect(et, i, chain, providers));
    }
    return slots.join("");
}

function _readChainFromDom(et) {
    const chain = [];
    for (let i = 0; i < _MAX_PROVIDER_SLOTS; i++) {
        const sel = $(`.ai-config-slot[data-event="${et}"][data-slot="${i}"]`);
        if (!sel) break;
        const v = sel.value;
        if (v) chain.push(v);
    }
    // Dedupe defensively while preserving order
    const seen = new Set();
    return chain.filter(p => (seen.has(p) ? false : (seen.add(p), true)));
}

function _rerenderSlotsForEvent(et, chain, providers) {
    const host = $(`.ai-config-slots[data-event="${et}"]`);
    if (host) host.innerHTML = _renderSlotsForEvent(et, chain, providers);
}

// Re-fetch the full provider config from the backend and re-render the slots
// for ``et`` using the server's authoritative value. Used as a safety net on
// save failures so the optimistic UI doesn't claim a chain that was never
// persisted (exactly the bug where the panel showed Gemini→Groq→Ollama while
// the DB still had legacy "ollama" and every call skipped the fallbacks).
async function _resyncProviderChain(et, providers) {
    try {
        const resp = await fetch("/api/admin/ai-config");
        if (!resp.ok) return;
        const data = await resp.json();
        const actual = _normalizeProviderChain((data.config || {})[et]);
        if (!actual.length && providers.length) actual.push(providers[0]);
        _rerenderSlotsForEvent(et, actual, providers);
    } catch {
        // Best effort: if the refetch itself fails, leave the optimistic
        // state in place rather than wiping the user's input.
    }
}

async function _saveProviderChain(et, chain, providers) {
    const status = $(`.ai-config-status[data-event="${et}"]`);
    if (status) {
        status.textContent = "Guardando...";
        status.style.color = "var(--text-dim)";
    }
    try {
        const resp = await fetch("/api/admin/ai-config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event_type: et, providers: chain }),
        });
        if (resp.ok) {
            // Re-render from the server-confirmed chain so the UI reflects
            // exactly what got persisted, not what we optimistically drew.
            const data = await resp.json().catch(() => ({}));
            const confirmed = _normalizeProviderChain(data.providers);
            if (confirmed.length && providers && providers.length) {
                _rerenderSlotsForEvent(et, confirmed, providers);
            }
            if (status) {
                status.textContent = "Guardado";
                status.style.color = "#0d9488";
                setTimeout(() => { if (status) status.textContent = ""; }, 2000);
            }
        } else {
            const err = await resp.json().catch(() => ({}));
            if (status) {
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
            if (providers && providers.length) {
                await _resyncProviderChain(et, providers);
            }
        }
    } catch (err) {
        if (status) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
        if (providers && providers.length) {
            await _resyncProviderChain(et, providers);
        }
    }
}

function renderAIConfig(config, validProviders, validEventTypes, schedule) {
    const container = $("#ai-config-wrap");
    if (!validEventTypes || !validEventTypes.length) {
        container.innerHTML = `<div class="admin-empty">No hay configuración disponible</div>`;
        return;
    }

    const providers = (validProviders || []).filter(p => _providerLabels[p]);

    const cards = validEventTypes.map(et => {
        const chain = _normalizeProviderChain(config[et]);
        if (!chain.length && providers.length) chain.push(providers[0]);

        const desc = _eventDescs[et] || "";

        let scheduleHtml = "";
        if (et === "search_prefetch") {
            const sched = schedule[et] || {};
            const qStart = sched.quiet_start || "";
            const qEnd = sched.quiet_end || "";
            scheduleHtml = `
                <div style="margin-top:0.4rem;padding-top:0.4rem;border-top:1px solid var(--border)">
                    <div style="font-size:0.72rem;font-weight:600;color:var(--text);margin-bottom:0.3rem">Franja horaria de desactivación</div>
                    <div style="display:flex;gap:0.4rem;align-items:center">
                        <select class="ai-schedule-start" data-event="${et}" style="width:auto;flex:1">
                            ${_buildHourOptions(qStart)}
                        </select>
                        <span style="font-size:0.75rem;color:var(--text-dim)">a</span>
                        <select class="ai-schedule-end" data-event="${et}" style="width:auto;flex:1">
                            ${_buildHourOptions(qEnd)}
                        </select>
                    </div>
                    <div class="ai-schedule-status" data-event="${et}" style="font-size:0.65rem;min-height:1rem;color:var(--text-dim)"></div>
                </div>`;
        }

        const slotsHtml = _renderSlotsForEvent(et, chain, providers);

        return `
            <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.4rem">
                <div style="font-size:0.82rem;font-weight:600;color:var(--text)">${escHtml(_eventLabels[et] || et)}</div>
                <div style="font-size:0.68rem;color:var(--text-dim);margin-bottom:0.2rem">${escHtml(desc)}</div>
                <div class="ai-config-slots" data-event="${et}" style="display:flex;flex-direction:column;gap:0.35rem">${slotsHtml}</div>
                <div class="ai-config-status" data-event="${et}" style="font-size:0.65rem;min-height:1rem;color:var(--text-dim)"></div>
                ${scheduleHtml}
            </div>`;
    }).join("");

    container.innerHTML = `<div class="admin-engagement">${cards}</div>`;

    // Use onchange property (not addEventListener) so repeated calls to
    // renderAIConfig don't stack duplicate handlers.
    container.onchange = async (evt) => {
        const sel = evt.target;
        if (!sel || !sel.classList || !sel.classList.contains("ai-config-slot")) return;
        const et = sel.dataset.event;
        const chain = _readChainFromDom(et);
        if (!chain.length && providers.length) {
            chain.push(providers[0]);
        }
        _rerenderSlotsForEvent(et, chain, providers);
        await _saveProviderChain(et, chain, providers);
    };

    $$(".ai-schedule-start, .ai-schedule-end").forEach(sel => {
        sel.addEventListener("change", async () => {
            const et = sel.dataset.event;
            const startSel = $(`.ai-schedule-start[data-event="${et}"]`);
            const endSel = $(`.ai-schedule-end[data-event="${et}"]`);
            const qStart = startSel.value;
            const qEnd = endSel.value;

            if ((qStart && !qEnd) || (!qStart && qEnd)) return;

            const status = $(`.ai-schedule-status[data-event="${et}"]`);
            status.textContent = "Guardando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-schedule", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ event_type: et, quiet_start: qStart, quiet_end: qEnd }),
                });
                if (resp.ok) {
                    status.textContent = qStart && qEnd ? `Desactivado de ${qStart} a ${qEnd}` : "Sin restricción";
                    status.style.color = "#0d9488";
                    setTimeout(() => { status.textContent = ""; }, 3000);
                } else {
                    const err = await resp.json();
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });
}

// ── Scheduler config ────────────────────────────────────────────────────

const _schedulerLabels = {
    refresh_news: "Actualización de RSS",
    prefetch_topics: "Regenerar temas del día",
};

const _schedulerDescs = {
    refresh_news: "Cada cuántos minutos se buscan noticias nuevas en los feeds RSS",
    prefetch_topics: "Cada cuántos minutos se regeneran los 6 temas destacados con IA",
};

function _fmtInterval(minutes) {
    if (minutes < 60) return `${minutes} min`;
    const h = minutes / 60;
    return h === 1 ? "1 h" : `${h} h`;
}

async function loadSchedulerConfig() {
    try {
        const resp = await fetch("/api/admin/scheduler-config");
        if (resp.status === 403) return;
        const data = await resp.json();
        renderSchedulerConfig(data.config, data.valid_intervals);
    } catch (err) {
        console.error("Scheduler config load failed:", err);
        const container = $("#scheduler-config-wrap");
        if (container) container.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red</div>`;
    }
}

function renderSchedulerConfig(config, validIntervals) {
    const container = $("#scheduler-config-wrap");
    if (!validIntervals || !Object.keys(validIntervals).length) {
        container.innerHTML = `<div class="admin-empty">No hay configuración disponible</div>`;
        return;
    }

    const cards = Object.keys(validIntervals).map(jobKey => {
        const current = config[jobKey] || 10;
        const options = validIntervals[jobKey].map(v =>
            `<option value="${v}" ${v === current ? "selected" : ""}>${_fmtInterval(v)}</option>`
        ).join("");

        const desc = _schedulerDescs[jobKey] || "";

        return `
            <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.4rem;flex:1;min-width:220px">
                <div style="font-size:0.82rem;font-weight:600;color:var(--text)">${escHtml(_schedulerLabels[jobKey] || jobKey)}</div>
                <div style="font-size:0.68rem;color:var(--text-dim);margin-bottom:0.2rem">${escHtml(desc)}</div>
                <select class="ai-config-select scheduler-interval-select" data-job="${jobKey}">${options}</select>
                <div class="scheduler-interval-status" data-job="${jobKey}" style="font-size:0.65rem;min-height:1rem;color:var(--text-dim)"></div>
            </div>`;
    }).join("");

    container.innerHTML = `<div class="admin-engagement">${cards}</div>`;

    $$(".scheduler-interval-select").forEach(sel => {
        sel.addEventListener("change", async () => {
            const jobKey = sel.dataset.job;
            const intervalMinutes = parseInt(sel.value, 10);
            const status = $(`.scheduler-interval-status[data-job="${jobKey}"]`);
            status.textContent = "Guardando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/scheduler-config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ job_key: jobKey, interval_minutes: intervalMinutes }),
                });
                if (resp.ok) {
                    status.textContent = "Guardado";
                    status.style.color = "#0d9488";
                    setTimeout(() => { status.textContent = ""; }, 2000);
                } else {
                    const err = await resp.json();
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });
}

// ── Ollama invocation timeout ───────────────────────────────────────────

async function loadOllamaTimeout() {
    try {
        const resp = await fetch("/api/admin/ollama-config");
        if (resp.status === 403) return;
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderOllamaTimeout(data);
    } catch (err) {
        console.error("Ollama timeout config load failed:", err);
        const container = $("#ollama-timeout-wrap");
        if (container) container.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red</div>`;
    }
}

function renderOllamaTimeout(data) {
    const container = $("#ollama-timeout-wrap");
    if (!container) return;

    const current = data.timeout_seconds ?? data.default ?? 120;
    const min = data.min ?? 30;
    const max = data.max ?? 900;
    const def = data.default ?? 120;

    container.innerHTML = `
        <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.4rem;max-width:420px">
            <div style="font-size:0.68rem;color:var(--text-dim);margin-bottom:0.2rem">
                Rango permitido: ${min}–${max} segundos (default ${def}s).
            </div>
            <div style="display:flex;gap:0.5rem;align-items:center">
                <input type="number" id="ollama-timeout-input" class="ai-config-select"
                    min="${min}" max="${max}" step="10" value="${current}"
                    style="width:110px;min-width:110px;text-align:right;padding-right:0.5rem">
                <span style="font-size:0.75rem;color:var(--text-dim)">seg</span>
                <button id="ollama-timeout-save" class="ai-config-select" style="cursor:pointer;flex:1">Guardar</button>
            </div>
            <div id="ollama-timeout-status" style="font-size:0.65rem;min-height:1rem;color:var(--text-dim)"></div>
        </div>
    `;

    const input = $("#ollama-timeout-input");
    const btn = $("#ollama-timeout-save");
    const status = $("#ollama-timeout-status");

    btn.addEventListener("click", async () => {
        const raw = parseInt(input.value, 10);
        if (!Number.isFinite(raw) || raw < min || raw > max) {
            status.textContent = `Valor fuera de rango (${min}–${max})`;
            status.style.color = "#ea580c";
            return;
        }
        status.textContent = "Guardando...";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/ollama-config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ timeout_seconds: raw }),
            });
            if (resp.ok) {
                status.textContent = "Guardado";
                status.style.color = "#0d9488";
                setTimeout(() => { status.textContent = ""; }, 2000);
            } else {
                const err = await resp.json().catch(() => ({}));
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
    });
}

// ── AI provider quota limits ─────────────────────────────────────────────

const _limitFieldLabels = {
    rpm: "RPM",
    tpm: "TPM",
    rpd: "RPD",
    tpd: "TPD",
    monthly_usd: "USD/mes",
    daily_usd: "USD/día",
    monthly_usd_global: "USD/mes (global)",
    daily_usd_global: "USD/día (global)",
};

const _limitFieldDescs = {
    rpm: "Requests por minuto",
    tpm: "Tokens por minuto",
    rpd: "Requests por día",
    tpd: "Tokens por día",
};

function _fmtLimitNum(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
}

function _fmtUsd(n) {
    if (n === null || n === undefined) return "—";
    const num = Number(n);
    if (!Number.isFinite(num)) return "—";
    if (num >= 1000) return "$" + num.toFixed(0);
    if (num >= 10) return "$" + num.toFixed(2);
    if (num >= 0.01) return "$" + num.toFixed(3);
    if (num === 0) return "$0";
    return "$" + num.toFixed(4);
}

// Pricing por (provider, model) cargado junto con los límites para que la
// card pueda mostrar el costo actual y abrir el form inline. Se rebuildea
// completo cada vez que se llama loadAILimits().
let _aiPricingCache = { items: [], source_urls: {} };

function _pricingForRow(provider, model) {
    return (_aiPricingCache.items || []).find(
        p => p.provider === provider && p.model === model
    ) || null;
}

async function loadAILimits() {
    const container = $("#ai-limits-wrap");
    if (!container) return;
    try {
        const [limitsResp, pricingResp] = await Promise.all([
            fetch("/api/admin/ai-limits"),
            fetch("/api/admin/ai-pricing"),
        ]);
        if (limitsResp.status === 403 || pricingResp.status === 403) return;
        if (!limitsResp.ok) throw new Error(`HTTP ${limitsResp.status}`);
        if (!pricingResp.ok) throw new Error(`HTTP ${pricingResp.status}`);
        const data = await limitsResp.json();
        _aiPricingCache = await pricingResp.json();
        renderAILimits(data.items || []);
        renderAIGlobalBudget(data.global || null);
    } catch (err) {
        console.error("AI limits load failed:", err);
        container.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error al cargar límites</div>`;
    }
}

function _renderUsdProgress(used, cap, blocked) {
    const pct = cap && cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
    const barColor = blocked ? "#dc2626" : pct >= 80 ? "#ea580c" : "#0d9488";
    const barWidth = cap && cap > 0 ? pct : 0;
    return `<div style="height:4px;border-radius:2px;background:var(--border);overflow:hidden">
        <div style="height:100%;width:${barWidth}%;background:${barColor};transition:width .3s"></div>
    </div>`;
}

function renderAIGlobalBudget(g) {
    const container = $("#ai-budget-global-wrap");
    if (!container) return;
    if (!g) {
        container.innerHTML = "";
        return;
    }
    const monthly = g.monthly_usd;
    const monthUsed = Number(g.monthly_usd_used || 0);
    const todayUsed = Number(g.daily_usd_used || 0);
    const dailyCap = g.daily_usd_cap;
    const blockedMonth = (g.blocked_by || []).includes("monthly_usd_global");
    const blockedDay = (g.blocked_by || []).includes("daily_usd_global");
    const val = monthly === null || monthly === undefined ? "" : monthly;

    const blockedBadge = (g.blocked_by || []).length
        ? `<span style="background:#fee2e2;color:#991b1b;padding:0.15rem 0.45rem;border-radius:999px;font-size:0.62rem;font-weight:600">
             Bloqueado: ${(g.blocked_by || []).map(f => _limitFieldLabels[f] || f).join(", ")}
           </span>`
        : "";

    container.innerHTML = `
        <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.5rem">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;flex-wrap:wrap">
                <div style="display:flex;flex-direction:column;gap:0.1rem">
                    <div style="font-size:0.82rem;font-weight:600;color:var(--text)">Presupuesto USD/mes (global)</div>
                    <div style="font-size:0.65rem;color:var(--text-dim)">Techo total para todos los proveedores juntos. El cap diario se ajusta solo según lo gastado.</div>
                </div>
                ${blockedBadge}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.6rem">
                <div style="display:flex;flex-direction:column;gap:0.25rem">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                        <label style="font-size:0.7rem;font-weight:600;color:var(--text)">USD/mes</label>
                        <span style="font-size:0.62rem;color:var(--text-dim)">${escHtml(_fmtUsd(monthUsed))}${monthly !== null && monthly !== undefined ? " / " + escHtml(_fmtUsd(monthly)) : ""}</span>
                    </div>
                    <input type="number" min="0" step="0.01" id="ai-budget-global-input"
                        value="${val}" placeholder="sin límite"
                        style="width:100%;padding:0.3rem 0.4rem;font-size:0.75rem">
                    ${_renderUsdProgress(monthUsed, monthly, blockedMonth)}
                </div>
                <div style="display:flex;flex-direction:column;gap:0.25rem">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                        <label style="font-size:0.7rem;font-weight:600;color:var(--text)">USD/día (auto)</label>
                        <span style="font-size:0.62rem;color:var(--text-dim)">${escHtml(_fmtUsd(todayUsed))}${dailyCap !== null && dailyCap !== undefined ? " / " + escHtml(_fmtUsd(dailyCap)) : ""}</span>
                    </div>
                    <div style="padding:0.3rem 0.4rem;font-size:0.7rem;color:var(--text-dim);background:var(--border);border-radius:4px">
                        ${dailyCap === null || dailyCap === undefined ? "Sin presupuesto" : "Hoy: " + escHtml(_fmtUsd(dailyCap))}
                    </div>
                    ${_renderUsdProgress(todayUsed, dailyCap, blockedDay)}
                </div>
            </div>
            <div style="display:flex;gap:0.4rem;align-items:center;flex-wrap:wrap">
                <button id="ai-budget-global-save" class="ai-config-select" style="cursor:pointer;padding:0.35rem 0.8rem">Guardar</button>
                <button id="ai-budget-global-reset" style="cursor:pointer;padding:0.35rem 0.8rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim);font-size:0.72rem">
                    Quitar presupuesto
                </button>
                <div id="ai-budget-global-status" style="font-size:0.65rem;color:var(--text-dim);flex:1;min-width:100px"></div>
            </div>
        </div>`;

    const saveBtn = $("#ai-budget-global-save");
    const resetBtn = $("#ai-budget-global-reset");
    const status = $("#ai-budget-global-status");

    saveBtn?.addEventListener("click", async () => {
        const input = $("#ai-budget-global-input");
        const raw = (input?.value ?? "").trim();
        let payload;
        if (raw === "") {
            payload = { monthly_usd: null };
        } else {
            const n = Number(raw);
            if (!Number.isFinite(n) || n < 0) {
                status.textContent = "USD/mes inválido";
                status.style.color = "#ea580c";
                return;
            }
            payload = { monthly_usd: n };
        }
        status.textContent = "Guardando...";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/ai-budget-global", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (resp.ok) {
                status.textContent = "Guardado";
                status.style.color = "#0d9488";
                setTimeout(loadAILimits, 800);
            } else {
                const err = await resp.json().catch(() => ({}));
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
    });

    resetBtn?.addEventListener("click", async () => {
        status.textContent = "Quitando...";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/ai-budget-global", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ reset: true }),
            });
            if (resp.ok) {
                status.textContent = "Sin presupuesto";
                status.style.color = "#0d9488";
                setTimeout(loadAILimits, 800);
            } else {
                const err = await resp.json().catch(() => ({}));
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
    });
}

function _renderLimitField(item, field) {
    const lim = item[field];
    const used = item[`${field}_used`] ?? 0;
    const tripped = item.blocked_by.includes(field);
    const pct = lim && lim > 0 ? Math.min(100, Math.round((used / lim) * 100)) : 0;
    const barColor = tripped ? "#dc2626" : pct >= 80 ? "#ea580c" : "#0d9488";
    const barWidth = lim ? pct : 0;

    const val = lim === null || lim === undefined ? "" : lim;
    return `
        <div style="display:flex;flex-direction:column;gap:0.25rem">
            <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                <label style="font-size:0.7rem;font-weight:600;color:var(--text)"
                       title="${escHtml(_limitFieldDescs[field] || "")}">${_limitFieldLabels[field]}</label>
                <span style="font-size:0.62rem;color:var(--text-dim)">
                    ${escHtml(_fmtLimitNum(used))}${lim ? " / " + escHtml(_fmtLimitNum(lim)) : ""}
                </span>
            </div>
            <input type="number" min="0" step="1"
                class="ai-limit-input" data-provider="${escHtml(item.provider)}"
                data-model="${escHtml(item.model)}" data-field="${field}"
                value="${val}" placeholder="sin límite"
                style="width:100%;padding:0.3rem 0.4rem;font-size:0.75rem">
            <div style="height:4px;border-radius:2px;background:var(--border);overflow:hidden">
                <div style="height:100%;width:${barWidth}%;background:${barColor};transition:width .3s"></div>
            </div>
        </div>`;
}

function _renderBudgetField(item) {
    const monthly = item.monthly_usd;
    const monthUsed = Number(item.monthly_usd_used || 0);
    const todayUsed = Number(item.daily_usd_used || 0);
    const dailyCap = item.daily_usd_cap;
    const blockedMonth = (item.blocked_by || []).includes("monthly_usd");
    const blockedDay = (item.blocked_by || []).includes("daily_usd");
    const val = monthly === null || monthly === undefined ? "" : monthly;

    return `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.6rem">
            <div style="display:flex;flex-direction:column;gap:0.25rem">
                <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                    <label style="font-size:0.7rem;font-weight:600;color:var(--text)"
                           title="Presupuesto en USD para todo el mes calendario">USD/mes</label>
                    <span style="font-size:0.62rem;color:var(--text-dim)">
                        ${escHtml(_fmtUsd(monthUsed))}${monthly !== null && monthly !== undefined ? " / " + escHtml(_fmtUsd(monthly)) : ""}
                    </span>
                </div>
                <input type="number" min="0" step="0.01"
                    class="ai-limit-budget-input" data-provider="${escHtml(item.provider)}"
                    data-model="${escHtml(item.model)}"
                    value="${val}" placeholder="sin límite"
                    style="width:100%;padding:0.3rem 0.4rem;font-size:0.75rem">
                ${_renderUsdProgress(monthUsed, monthly, blockedMonth)}
            </div>
            <div style="display:flex;flex-direction:column;gap:0.25rem">
                <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                    <label style="font-size:0.7rem;font-weight:600;color:var(--text)"
                           title="Cap diario derivado del presupuesto mensual restante / días que faltan">USD/día (auto)</label>
                    <span style="font-size:0.62rem;color:var(--text-dim)">
                        ${escHtml(_fmtUsd(todayUsed))}${dailyCap !== null && dailyCap !== undefined ? " / " + escHtml(_fmtUsd(dailyCap)) : ""}
                    </span>
                </div>
                <div style="padding:0.3rem 0.4rem;font-size:0.7rem;color:var(--text-dim);background:var(--border);border-radius:4px;text-align:center">
                    ${dailyCap === null || dailyCap === undefined ? "Sin presupuesto" : "Hoy: " + escHtml(_fmtUsd(dailyCap))}
                </div>
                ${_renderUsdProgress(todayUsed, dailyCap, blockedDay)}
            </div>
        </div>`;
}

function _renderPricingBlock(item) {
    const pricing = _pricingForRow(item.provider, item.model);
    const url = (_aiPricingCache.source_urls || {})[item.provider] || "";
    const inUsd = pricing ? Number(pricing.input_usd_per_1m) : null;
    const outUsd = pricing ? Number(pricing.output_usd_per_1m) : null;
    const isDefault = pricing ? !!pricing.is_default : true;
    const updatedAt = pricing && pricing.updated_at ? formatDatetimeART(pricing.updated_at) : "—";

    const tooltip =
        "Costo USD por llamada = (tokens_in × in_$/1M + tokens_out × out_$/1M) / 1.000.000. " +
        "Estos valores se aplican a cada log_ai_usage y alimentan los totales mensual/diario " +
        "que ves arriba. Ollama suele ser $0 (self-hosted) salvo que cargues un valor manual.";

    const updateBtn = url
        ? `<a class="ai-pricing-source" data-provider="${escHtml(item.provider)}"
              data-model="${escHtml(item.model)}"
              href="${escHtml(url)}" target="_blank" rel="noopener noreferrer"
              title="Se buscará en internet el valor actual"
              style="font-size:0.62rem;color:#0d9488;text-decoration:underline">
              Actualizar valores ↗
           </a>`
        : `<span style="font-size:0.62rem;color:var(--text-dim);font-style:italic">
              Sin pricing externo (self-hosted)
           </span>`;

    const customBadge = isDefault
        ? ""
        : `<span style="background:#dbeafe;color:#1e40af;padding:0.1rem 0.35rem;border-radius:999px;font-size:0.58rem">
              custom
           </span>`;

    const inDisplay = inUsd === null || isNaN(inUsd) ? "—" : "$" + inUsd.toFixed(4) + "/1M";
    const outDisplay = outUsd === null || isNaN(outUsd) ? "—" : "$" + outUsd.toFixed(4) + "/1M";

    return `
        <div class="admin-pricing-block" data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
             style="border:1px solid var(--border);border-radius:6px;padding:0.4rem 0.55rem;display:flex;flex-direction:column;gap:0.35rem">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;flex-wrap:wrap">
                <div style="display:flex;align-items:center;gap:0.3rem">
                    <span style="font-size:0.7rem;font-weight:600;color:var(--text)" title="${escHtml(tooltip)}">
                        Precio por 1M tokens ⓘ
                    </span>
                    ${customBadge}
                </div>
                ${updateBtn}
            </div>
            <div style="display:flex;justify-content:space-between;gap:0.4rem;font-size:0.65rem;color:var(--text-dim)">
                <span>in: <b style="color:var(--text)">${escHtml(inDisplay)}</b></span>
                <span>out: <b style="color:var(--text)">${escHtml(outDisplay)}</b></span>
                <span title="Última actualización del pricing">act.: ${escHtml(updatedAt)}</span>
            </div>
            <div class="ai-pricing-form" data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                 style="display:none;flex-direction:column;gap:0.3rem;background:var(--bg);padding:0.4rem;border-radius:4px">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.4rem">
                    <label style="font-size:0.62rem;color:var(--text-dim);display:flex;flex-direction:column;gap:0.15rem">
                        Input USD/1M
                        <input type="number" min="0" step="0.0001"
                               class="ai-pricing-input-in" data-provider="${escHtml(item.provider)}"
                               data-model="${escHtml(item.model)}"
                               value="${inUsd === null || isNaN(inUsd) ? "" : inUsd}"
                               placeholder="0.0000"
                               style="width:100%;padding:0.25rem 0.35rem;font-size:0.72rem">
                    </label>
                    <label style="font-size:0.62rem;color:var(--text-dim);display:flex;flex-direction:column;gap:0.15rem">
                        Output USD/1M
                        <input type="number" min="0" step="0.0001"
                               class="ai-pricing-input-out" data-provider="${escHtml(item.provider)}"
                               data-model="${escHtml(item.model)}"
                               value="${outUsd === null || isNaN(outUsd) ? "" : outUsd}"
                               placeholder="0.0000"
                               style="width:100%;padding:0.25rem 0.35rem;font-size:0.72rem">
                    </label>
                </div>
                <div style="display:flex;gap:0.3rem;align-items:center;flex-wrap:wrap">
                    <button class="ai-pricing-save ai-config-select"
                            data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                            style="cursor:pointer;padding:0.25rem 0.6rem;font-size:0.7rem">Guardar pricing</button>
                    <button class="ai-pricing-reset"
                            data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                            style="cursor:pointer;padding:0.25rem 0.6rem;font-size:0.65rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim)">
                        Restaurar default
                    </button>
                    <button class="ai-pricing-cancel"
                            data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                            style="cursor:pointer;padding:0.25rem 0.6rem;font-size:0.65rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim)">
                        Cerrar
                    </button>
                    <div class="ai-pricing-status"
                         data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                         style="font-size:0.62rem;color:var(--text-dim);flex:1;min-width:80px"></div>
                </div>
            </div>
        </div>`;
}

function _wirePricingHandlers() {
    const findForm = (provider, model) => document.querySelector(
        `.ai-pricing-form[data-provider="${provider}"][data-model="${model}"]`
    );

    $$(".ai-pricing-source").forEach(link => {
        link.addEventListener("click", (ev) => {
            // Cmd/Ctrl-click sigue abriendo en pestaña — solo abrimos el form
            // en click izquierdo simple. Mantenemos el href real como fallback.
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
            const form = findForm(link.dataset.provider, link.dataset.model);
            if (form) {
                form.style.display = form.style.display === "none" ? "flex" : "none";
            }
        });
    });

    $$(".ai-pricing-cancel").forEach(btn => {
        btn.addEventListener("click", () => {
            const form = findForm(btn.dataset.provider, btn.dataset.model);
            if (form) form.style.display = "none";
        });
    });

    $$(".ai-pricing-save").forEach(btn => {
        btn.addEventListener("click", async () => {
            const provider = btn.dataset.provider;
            const model = btn.dataset.model;
            const status = document.querySelector(
                `.ai-pricing-status[data-provider="${provider}"][data-model="${model}"]`
            );
            const inputIn = document.querySelector(
                `.ai-pricing-input-in[data-provider="${provider}"][data-model="${model}"]`
            );
            const inputOut = document.querySelector(
                `.ai-pricing-input-out[data-provider="${provider}"][data-model="${model}"]`
            );
            const inVal = Number((inputIn?.value ?? "").trim());
            const outVal = Number((inputOut?.value ?? "").trim());
            if (!Number.isFinite(inVal) || inVal < 0 || !Number.isFinite(outVal) || outVal < 0) {
                status.textContent = "Valores inválidos";
                status.style.color = "#ea580c";
                return;
            }
            status.textContent = "Guardando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-pricing", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        provider, model,
                        input_usd_per_1m: inVal,
                        output_usd_per_1m: outVal,
                    }),
                });
                if (resp.ok) {
                    status.textContent = "Guardado";
                    status.style.color = "#0d9488";
                    setTimeout(loadAILimits, 600);
                } else {
                    const err = await resp.json().catch(() => ({}));
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });

    $$(".ai-pricing-reset").forEach(btn => {
        btn.addEventListener("click", async () => {
            const provider = btn.dataset.provider;
            const model = btn.dataset.model;
            const status = document.querySelector(
                `.ai-pricing-status[data-provider="${provider}"][data-model="${model}"]`
            );
            status.textContent = "Restaurando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-pricing", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider, model, reset: true }),
                });
                if (resp.ok) {
                    status.textContent = "Default restaurado";
                    status.style.color = "#0d9488";
                    setTimeout(loadAILimits, 600);
                } else {
                    const err = await resp.json().catch(() => ({}));
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });
}

function renderAILimits(items) {
    const container = $("#ai-limits-wrap");
    if (!container) return;
    if (!items.length) {
        container.innerHTML = `<div class="admin-empty">Sin proveedores configurados</div>`;
        return;
    }

    const cards = items.map(item => {
        const provName = _providerDisplayNames[item.provider] || item.provider;
        const blockedBadge = item.blocked_by.length
            ? `<span style="background:#fee2e2;color:#991b1b;padding:0.15rem 0.45rem;border-radius:999px;font-size:0.62rem;font-weight:600">
                 Bloqueado: ${item.blocked_by.map(f => _limitFieldLabels[f] || f).join(", ")}
               </span>`
            : "";
        const defaultBadge = item.is_default
            ? `<span style="background:var(--border);color:var(--text-dim);padding:0.15rem 0.45rem;border-radius:999px;font-size:0.62rem">default</span>`
            : `<span style="background:#dbeafe;color:#1e40af;padding:0.15rem 0.45rem;border-radius:999px;font-size:0.62rem">personalizado</span>`;

        const ollamaNote = item.provider === "ollama"
            ? `<div style="font-size:0.65rem;color:var(--text-dim);font-style:italic">Self-hosted: sin cupo externo. Dejalos vacíos para no limitar, o configurá un tope manual para acotar el gasto de CPU (se respeta estrictamente).</div>`
            : "";

        return `
            <div class="admin-eng-card" data-provider="${escHtml(item.provider)}" data-model="${escHtml(item.model)}"
                 style="flex-direction:column;align-items:stretch;gap:0.5rem;flex:1;min-width:280px">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:0.4rem;flex-wrap:wrap">
                    <div style="display:flex;flex-direction:column;gap:0.1rem">
                        <div style="font-size:0.82rem;font-weight:600;color:var(--text)">${escHtml(provName)}</div>
                        <div style="font-size:0.65rem;color:var(--text-dim);font-family:monospace">${escHtml(item.model)}</div>
                    </div>
                    <div style="display:flex;gap:0.3rem;align-items:center;flex-wrap:wrap">
                        ${blockedBadge}
                        ${defaultBadge}
                    </div>
                </div>
                ${ollamaNote}
                ${_renderPricingBlock(item)}
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.6rem">
                    ${_renderLimitField(item, "rpm")}
                    ${_renderLimitField(item, "tpm")}
                    ${_renderLimitField(item, "rpd")}
                    ${_renderLimitField(item, "tpd")}
                </div>
                ${_renderBudgetField(item)}
                <div style="display:flex;gap:0.4rem;align-items:center;flex-wrap:wrap">
                    <button class="ai-limit-save ai-config-select" data-provider="${escHtml(item.provider)}"
                            data-model="${escHtml(item.model)}"
                            style="cursor:pointer;padding:0.35rem 0.8rem">Guardar</button>
                    <button class="ai-limit-reset" data-provider="${escHtml(item.provider)}"
                            data-model="${escHtml(item.model)}"
                            style="cursor:pointer;padding:0.35rem 0.8rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim);font-size:0.72rem">
                        Restaurar default
                    </button>
                    <div class="ai-limit-status" data-provider="${escHtml(item.provider)}"
                         data-model="${escHtml(item.model)}"
                         style="font-size:0.65rem;color:var(--text-dim);flex:1;min-width:100px"></div>
                </div>
            </div>`;
    }).join("");

    container.innerHTML = `<div class="admin-engagement" style="flex-wrap:wrap">${cards}</div>`;

    _wirePricingHandlers();

    $$(".ai-limit-save").forEach(btn => {
        btn.addEventListener("click", async () => {
            const provider = btn.dataset.provider;
            const model = btn.dataset.model;
            const status = $(`.ai-limit-status[data-provider="${provider}"][data-model="${model}"]`);
            const payload = { provider, model };
            for (const field of ["rpm", "tpm", "rpd", "tpd"]) {
                const input = document.querySelector(
                    `.ai-limit-input[data-provider="${provider}"][data-model="${model}"][data-field="${field}"]`
                );
                const raw = (input?.value ?? "").trim();
                if (raw === "") {
                    payload[field] = null;
                } else {
                    const n = parseInt(raw, 10);
                    if (!Number.isFinite(n) || n < 0) {
                        status.textContent = `${_limitFieldLabels[field]} inválido`;
                        status.style.color = "#ea580c";
                        return;
                    }
                    payload[field] = n;
                }
            }

            const budgetInput = document.querySelector(
                `.ai-limit-budget-input[data-provider="${provider}"][data-model="${model}"]`
            );
            const budgetRaw = (budgetInput?.value ?? "").trim();
            if (budgetRaw === "") {
                payload.monthly_usd = null;
            } else {
                const n = Number(budgetRaw);
                if (!Number.isFinite(n) || n < 0) {
                    status.textContent = "USD/mes inválido";
                    status.style.color = "#ea580c";
                    return;
                }
                payload.monthly_usd = n;
            }

            status.textContent = "Guardando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-limits", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (resp.ok) {
                    status.textContent = "Guardado";
                    status.style.color = "#0d9488";
                    setTimeout(loadAILimits, 800);
                } else {
                    const err = await resp.json().catch(() => ({}));
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });

    $$(".ai-limit-reset").forEach(btn => {
        btn.addEventListener("click", async () => {
            const provider = btn.dataset.provider;
            const model = btn.dataset.model;
            const status = $(`.ai-limit-status[data-provider="${provider}"][data-model="${model}"]`);
            status.textContent = "Restaurando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-limits", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider, model, reset: true }),
                });
                if (resp.ok) {
                    status.textContent = "Defaults restaurados";
                    status.style.color = "#0d9488";
                    setTimeout(loadAILimits, 800);
                } else {
                    const err = await resp.json().catch(() => ({}));
                    status.textContent = err.error || "Error";
                    status.style.color = "#ea580c";
                }
            } catch (err) {
                status.textContent = "Error de red";
                status.style.color = "#ea580c";
            }
        });
    });
}

// ── Railway infra USD limits ────────────────────────────────────────────

async function loadInfraLimits() {
    const container = $("#infra-limits-wrap");
    if (!container) return;
    try {
        const resp = await fetch("/api/admin/infra-limits");
        if (resp.status === 403) return;
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderInfraLimits(data);
    } catch (err) {
        console.error("Infra limits load failed:", err);
        container.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error al cargar límites de Railway</div>`;
    }
}

function renderInfraLimits(data) {
    const container = $("#infra-limits-wrap");
    if (!container) return;

    const limits = data.limits || {};
    const spend = data.spend || {};
    const blocked = data.blocked || [];
    const dailyMax = limits.daily_max;
    const monthlyMax = limits.monthly_max;
    const todayUsd = spend.today_usd;
    const monthUsd = spend.month_usd;
    const fetchedAt = spend.fetched_at;
    const blockedDay = blocked.includes("daily");
    const blockedMonth = blocked.includes("monthly");

    const dailyVal = dailyMax === null || dailyMax === undefined ? "" : dailyMax;
    const monthlyVal = monthlyMax === null || monthlyMax === undefined ? "" : monthlyMax;

    const todayDisplay = todayUsd === null || todayUsd === undefined ? "—" : _fmtUsd(todayUsd);
    const monthDisplay = monthUsd === null || monthUsd === undefined ? "—" : _fmtUsd(monthUsd);

    const banner = blocked.length
        ? `<div style="background:#fee2e2;color:#991b1b;padding:0.4rem 0.6rem;border-radius:4px;font-size:0.7rem;font-weight:600">
              Bloqueado por gasto de Railway:
              ${blockedMonth ? "USD/mes" : ""}${blockedMonth && blockedDay ? " + " : ""}${blockedDay ? "USD/día" : ""}.
              Las llamadas a Ollama se redirigen al próximo proveedor de la cadena.
           </div>`
        : "";

    const unavailableNote = data.available
        ? ""
        : `<div style="font-size:0.65rem;color:var(--text-dim);font-style:italic">
              Railway no está configurado (falta token / project ID). El guard se queda permisivo.
           </div>`;

    const noBaselineNote = (data.available && (todayUsd === null || todayUsd === undefined))
        ? `<div style="font-size:0.62rem;color:var(--text-dim);font-style:italic">
              Aún no hay suficientes snapshots del día para calcular gasto diario.
           </div>`
        : "";

    container.innerHTML = `
        <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.5rem">
            ${banner}
            ${unavailableNote}
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.6rem">
                <div style="display:flex;flex-direction:column;gap:0.25rem">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                        <label style="font-size:0.7rem;font-weight:600;color:var(--text)"
                               title="Tope diario de gasto en Railway. Si lo superás se bloquea Ollama.">USD/día (Railway)</label>
                        <span style="font-size:0.62rem;color:var(--text-dim)">${escHtml(todayDisplay)}${dailyMax !== null && dailyMax !== undefined ? " / " + escHtml(_fmtUsd(dailyMax)) : ""}</span>
                    </div>
                    <input type="number" min="0" step="0.01" id="infra-daily-input"
                        value="${dailyVal}" placeholder="sin límite"
                        style="width:100%;padding:0.3rem 0.4rem;font-size:0.75rem">
                    ${_renderUsdProgress(Number(todayUsd || 0), dailyMax, blockedDay)}
                </div>
                <div style="display:flex;flex-direction:column;gap:0.25rem">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.4rem">
                        <label style="font-size:0.7rem;font-weight:600;color:var(--text)"
                               title="Tope mensual de gasto en Railway. Si lo superás se bloquea Ollama.">USD/mes (Railway)</label>
                        <span style="font-size:0.62rem;color:var(--text-dim)">${escHtml(monthDisplay)}${monthlyMax !== null && monthlyMax !== undefined ? " / " + escHtml(_fmtUsd(monthlyMax)) : ""}</span>
                    </div>
                    <input type="number" min="0" step="0.1" id="infra-monthly-input"
                        value="${monthlyVal}" placeholder="sin límite"
                        style="width:100%;padding:0.3rem 0.4rem;font-size:0.75rem">
                    ${_renderUsdProgress(Number(monthUsd || 0), monthlyMax, blockedMonth)}
                </div>
            </div>
            ${noBaselineNote}
            <div style="display:flex;gap:0.4rem;align-items:center;flex-wrap:wrap">
                <button id="infra-limits-save" class="ai-config-select" style="cursor:pointer;padding:0.35rem 0.8rem">Guardar</button>
                <button id="infra-limits-reset" style="cursor:pointer;padding:0.35rem 0.8rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim);font-size:0.72rem">
                    Quitar límites
                </button>
                <button id="infra-limits-snapshot" title="Consulta Railway ahora y guarda un snapshot. Necesario al menos 2 por día para calcular el gasto diario." style="cursor:pointer;padding:0.35rem 0.8rem;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--text-dim);font-size:0.72rem">
                    Tomar snapshot
                </button>
                <div id="infra-limits-status" style="font-size:0.65rem;color:var(--text-dim);flex:1;min-width:100px">
                    ${fetchedAt ? "Último snapshot: " + escHtml(formatDatetimeART(fetchedAt)) : ""}
                </div>
            </div>
        </div>`;

    $("#infra-limits-save")?.addEventListener("click", async () => {
        const status = $("#infra-limits-status");
        const dRaw = ($("#infra-daily-input")?.value ?? "").trim();
        const mRaw = ($("#infra-monthly-input")?.value ?? "").trim();
        const payload = {};
        if (dRaw === "") {
            payload.daily_max = null;
        } else {
            const n = Number(dRaw);
            if (!Number.isFinite(n) || n < 0) {
                status.textContent = "USD/día inválido";
                status.style.color = "#ea580c";
                return;
            }
            payload.daily_max = n;
        }
        if (mRaw === "") {
            payload.monthly_max = null;
        } else {
            const n = Number(mRaw);
            if (!Number.isFinite(n) || n < 0) {
                status.textContent = "USD/mes inválido";
                status.style.color = "#ea580c";
                return;
            }
            payload.monthly_max = n;
        }
        status.textContent = "Guardando...";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/infra-limits", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (resp.ok) {
                status.textContent = "Guardado";
                status.style.color = "#0d9488";
                setTimeout(loadInfraLimits, 600);
            } else {
                const err = await resp.json().catch(() => ({}));
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
    });

    $("#infra-limits-reset")?.addEventListener("click", async () => {
        const status = $("#infra-limits-status");
        status.textContent = "Quitando...";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/infra-limits", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ reset: true }),
            });
            if (resp.ok) {
                status.textContent = "Sin límites";
                status.style.color = "#0d9488";
                setTimeout(loadInfraLimits, 600);
            } else {
                const err = await resp.json().catch(() => ({}));
                status.textContent = err.error || "Error";
                status.style.color = "#ea580c";
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        }
    });

    $("#infra-limits-snapshot")?.addEventListener("click", async () => {
        const btn = $("#infra-limits-snapshot");
        const status = $("#infra-limits-status");
        if (!btn) return;
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "Consultando…";
        status.textContent = "Consultando Railway…";
        status.style.color = "var(--text-dim)";
        try {
            const resp = await fetch("/api/admin/infra-costs/refresh", { method: "POST" });
            if (resp.status === 403) { window.location.href = "/"; return; }
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            if (!data.available) {
                status.textContent = "Falló: " + (data.reason || "no disponible");
                status.style.color = "#ea580c";
            } else if ((data.saved_rows || 0) === 0) {
                status.textContent = "Railway respondió OK pero sin servicios";
                status.style.color = "#ea580c";
            } else {
                status.textContent = `Snapshot guardado (${data.saved_rows} filas)`;
                status.style.color = "#0d9488";
                setTimeout(loadInfraLimits, 600);
            }
        } catch (err) {
            status.textContent = "Error de red";
            status.style.color = "#ea580c";
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });
}

// ── AI monitor (provider status + recent calls) ─────────────────────────

const _providerDisplayNames = {
    gemini: "Gemini",
    groq: "Groq",
    ollama: "Ollama",
};

const _statusLabels = {
    green: "Operativo",
    amber: "Con avisos",
    red: "Con problemas",
};

async function loadAIMonitor() {
    try {
        const resp = await fetch("/api/admin/ai-monitor");
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderAIMonitorProviders(data.providers || []);
    } catch (err) {
        console.error("AI monitor load failed:", err);
        const providers = $("#ai-monitor-providers");
        if (providers) {
            providers.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error al cargar estado de proveedores</div>`;
        }
    }
}

function renderAIMonitorProviders(providers) {
    const container = $("#ai-monitor-providers");
    if (!container) return;
    if (!providers.length) {
        container.innerHTML = `<div class="admin-empty">Sin proveedores configurados</div>`;
        return;
    }

    container.innerHTML = providers.map(p => {
        const name = _providerDisplayNames[p.provider] || p.provider;
        const statusClass = p.status || "amber";
        const statusLabel = _statusLabels[statusClass] || statusClass;

        const rows = [];

        const credLabel = p.provider === "ollama" ? "Base URL" : "API key";
        if (!p.configured) {
            rows.push(_row(credLabel, "No configurada", "#dc2626"));
        } else {
            rows.push(_row(credLabel, "Configurada"));
        }

        if (p.rate_limit_active) {
            rows.push(_row("Rate limit", `Cooldown ${p.rate_limit_seconds_remaining}s`, "#dc2626"));
        }

        if (p.recent_calls > 0) {
            const rate = p.success_rate != null ? `${Math.round(p.success_rate * 100)}%` : "—";
            rows.push(_row("Éxito reciente", `${rate} (${p.recent_success_count}/${p.recent_calls})`));
        } else {
            rows.push(_row("Éxito reciente", "Sin datos"));
        }

        if (p.errors_last_window > 0) {
            rows.push(_row("Errores 24h", String(p.errors_last_window), "#dc2626"));
        }

        if (p.last_success) {
            const when = formatDatetimeART(p.last_success.created_at);
            const ev = _eventLabels[p.last_success.event_type] || p.last_success.event_type;
            rows.push(_row("Último OK", `${when} · ${ev}`));
        } else {
            rows.push(_row("Último OK", "Nunca"));
        }

        let errorBlock = "";
        if (p.last_error) {
            const when = formatDatetimeART(p.last_error.created_at);
            const ev = _eventLabels[p.last_error.event_type] || p.last_error.event_type;
            const msg = escHtml(p.last_error.error_message || "(sin mensaje)");
            errorBlock = `
                <div class="admin-provider-error" title="${msg}">
                    <div style="font-weight:600;margin-bottom:0.2rem">Último error · ${escHtml(when)} · ${escHtml(ev)}</div>
                    <div>${msg}</div>
                </div>`;
        } else if (p.configured) {
            errorBlock = `<div class="admin-provider-ok">Sin errores registrados</div>`;
        }

        return `
            <div class="admin-provider-card">
                <div class="admin-provider-head">
                    <div>
                        <div class="admin-provider-name">${escHtml(name)}</div>
                        <div class="admin-provider-model">${escHtml(p.model || "")}</div>
                    </div>
                    <span class="admin-provider-pill ${statusClass}">${escHtml(statusLabel)}</span>
                </div>
                ${rows.join("")}
                ${errorBlock}
            </div>`;
    }).join("");
}

function _row(label, value, valueColor) {
    const style = valueColor ? ` style="color:${valueColor}"` : "";
    return `<div class="admin-provider-row"><span class="label">${escHtml(label)}</span><span class="value"${style}>${escHtml(value)}</span></div>`;
}

function renderAIMonitorRecent(calls) {
    const container = $("#ai-monitor-recent");
    if (!container) return;
    if (!calls.length) {
        container.innerHTML = `<div class="admin-empty">No se registraron invocaciones todavía</div>`;
        return;
    }
    const rows = calls.map(c => {
        const ev = _eventLabels[c.event_type] || c.event_type;
        const statusCell = c.success
            ? `<span class="admin-badge admin-badge-admin" style="background:rgba(13,148,136,0.15);color:#0d9488">OK</span>`
            : `<span class="admin-badge" style="background:rgba(220,38,38,0.15);color:#dc2626">ERROR</span>`;
        const detail = c.success
            ? `${fmtTokens(c.input_tokens)} → ${fmtTokens(c.output_tokens)} · ${c.latency_ms || 0}ms`
            : escHtml((c.error_message || "(sin mensaje)")).slice(0, 200);
        return `
            <tr>
                <td style="white-space:nowrap">${escHtml(formatDatetimeART(c.created_at))}</td>
                <td>${escHtml(ev)}</td>
                <td><span class="admin-badge ${_providerBadgeClass(c.provider)}">${escHtml(c.provider)}</span></td>
                <td>${statusCell}</td>
                <td style="font-size:0.72rem;color:var(--text-dim);word-break:break-word">${detail}</td>
            </tr>`;
    }).join("");

    container.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Cuándo</th><th>Evento</th><th>Provider</th><th>Estado</th><th>Detalle</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
}

// ── AI invocations (paginated) ──────────────────────────────────────────

async function loadAIInvocations() {
    const wrap = $("#ai-invocations-wrap");
    const pager = $("#ai-invocations-pagination");
    if (!wrap) return;

    const provSel = $("#ai-inv-provider");
    const evSel = $("#ai-inv-event");
    const sucSel = $("#ai-inv-success");

    const params = new URLSearchParams();
    params.set("page", String(_aiInvocationsPage));
    params.set("page_size", String(_PAGE_SIZE));
    if (provSel && provSel.value) params.set("provider", provSel.value);
    if (evSel && evSel.value) params.set("event_type", evSel.value);
    if (sucSel && sucSel.value) params.set("success", sucSel.value);

    try {
        const resp = await fetch(`/api/admin/ai-invocations?${params}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (resp.status === 404) {
            wrap.innerHTML = `<div class="admin-empty">Endpoint no disponible (requiere Fase 2)</div>`;
            if (pager) pager.innerHTML = "";
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        _populateInvocationFilters(data.filters || {});
        renderAIInvocations(data.items || [], data.total || 0, data.page || 1, data.page_size || _PAGE_SIZE);
    } catch (err) {
        console.error("AI invocations load failed:", err);
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red</div>`;
    }
}

function _populateInvocationFilters(filters) {
    const provSel = $("#ai-inv-provider");
    const evSel = $("#ai-inv-event");
    if (provSel && !provSel.dataset.initialized && filters.providers) {
        const current = provSel.value;
        provSel.innerHTML = `<option value="">Todos los proveedores</option>` +
            filters.providers.map(p => `<option value="${escHtml(p)}" ${p === current ? "selected" : ""}>${escHtml(_providerDisplayNames[p] || p)}</option>`).join("");
        provSel.dataset.initialized = "1";
    }
    if (evSel && !evSel.dataset.initialized && filters.event_types) {
        const current = evSel.value;
        evSel.innerHTML = `<option value="">Todos los eventos</option>` +
            filters.event_types.map(e => `<option value="${escHtml(e)}" ${e === current ? "selected" : ""}>${escHtml(_eventLabels[e] || e)}</option>`).join("");
        evSel.dataset.initialized = "1";
    }
}

function renderAIInvocations(items, total, page, pageSize) {
    const wrap = $("#ai-invocations-wrap");
    const pager = $("#ai-invocations-pagination");
    if (!items.length) {
        wrap.innerHTML = `<div class="admin-empty">No hay invocaciones en el rango seleccionado</div>`;
        if (pager) pager.innerHTML = "";
        return;
    }

    const rows = items.map((c, idx) => {
        const ev = _eventLabels[c.event_type] || c.event_type;
        const statusCell = c.success
            ? `<span class="admin-status-pill admin-status-ok">OK</span>`
            : `<span class="admin-status-pill admin-status-error">ERROR</span>`;
        const detail = c.success
            ? `${fmtTokens(c.input_tokens || 0)} → ${fmtTokens(c.output_tokens || 0)} · ${c.latency_ms || 0}ms`
            : escHtml((c.error_message || "(sin mensaje)")).slice(0, 200);
        const hasPreview = c.prompt_preview || c.response_preview;
        const hasErrorDetail = !c.success && (c.error_type || c.error_phase || c.http_status != null || c.error_message);
        const clickable = (hasPreview || hasErrorDetail) ? "clickable" : "";
        return `
            <tr class="${clickable}" data-idx="${idx}">
                <td style="white-space:nowrap">${escHtml(formatDatetimeART(c.created_at))}</td>
                <td>${escHtml(ev)}</td>
                <td><span class="admin-badge ${_providerBadgeClass(c.provider)}">${escHtml(c.provider || "")}</span></td>
                <td style="font-size:0.7rem;color:var(--text-dim)">${escHtml(c.model || "")}</td>
                <td>${statusCell}</td>
                <td style="font-size:0.72rem;color:var(--text-dim);word-break:break-word">${detail}</td>
            </tr>`;
    }).join("");

    wrap.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Cuándo</th><th>Evento</th><th>Provider</th><th>Modelo</th><th>Estado</th><th>Detalle</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;

    $$("#ai-invocations-wrap tr.clickable").forEach(tr => {
        tr.addEventListener("click", () => {
            const idx = parseInt(tr.dataset.idx, 10);
            const item = items[idx];
            if (item) openInvocationModal(item);
        });
    });

    _renderPagination(pager, total, page, pageSize, (newPage) => {
        _aiInvocationsPage = newPage;
        loadAIInvocations();
    });
}

const _phaseHints = {
    connect: "El request NUNCA llegó a Ollama. El servicio está caído, apagado, o la URL (OLLAMA_BASE_URL) es incorrecta.",
    write: "Se abrió la conexión pero no se terminó de enviar el request. Probablemente la red se cortó.",
    read: "El request SÍ llegó a Ollama. El modelo está cargado pero no respondió a tiempo (prompt muy largo, GPU saturada, o el modelo no está cargado en memoria).",
    response: "Ollama respondió pero con un error HTTP o un payload inválido. Ver error_message para el detalle.",
    unknown: "Error no clasificado. Revisá error_message y los logs de Ollama en Railway.",
};

function openInvocationModal(item) {
    const host = $("#admin-modal-host");
    if (!host) return;
    const rowsHtml = [
        _modalRow("Cuándo", formatDatetimeART(item.created_at)),
        _modalRow("Evento", _eventLabels[item.event_type] || item.event_type),
        _modalRow("Provider", _providerDisplayNames[item.provider] || item.provider),
        _modalRow("Modelo", item.model || "—"),
        _modalRow("Estado", item.success ? "OK" : "ERROR"),
        _modalRow("Tokens", `in ${fmtTokens(item.input_tokens || 0)} · out ${fmtTokens(item.output_tokens || 0)}`),
        _modalRow("Latencia", `${item.latency_ms || 0} ms`),
        _modalRow("Costo", fmtUSD(item.cost_total || 0)),
    ].join("");

    // Technical detail block (only for failures, only if the structured
    // error metadata is present — pre-migration rows may have it all NULL).
    let technicalHtml = "";
    if (!item.success && (item.error_type || item.error_phase || item.http_status != null || item.request_sent_at)) {
        const techRows = [
            item.error_type ? _modalRow("Tipo de error", item.error_type) : "",
            item.error_phase ? _modalRow("Fase", item.error_phase) : "",
            item.http_status != null ? _modalRow("HTTP status", String(item.http_status)) : "",
            item.request_sent_at ? _modalRow("Request enviado", formatDatetimeART(item.request_sent_at)) : "",
        ].join("");
        const hint = item.error_phase && _phaseHints[item.error_phase]
            ? `<div style="margin-top:0.5rem;padding:0.7rem 0.9rem;background:rgba(234,88,12,0.18);border:1px solid rgba(234,88,12,0.45);border-radius:6px;font-size:0.82rem;line-height:1.45;color:#fed7aa">
                ${escHtml(_phaseHints[item.error_phase])}
               </div>`
            : "";
        technicalHtml = `
            <div style="font-size:0.78rem;color:var(--text-dim);margin-top:0.8rem;text-transform:uppercase;letter-spacing:0.04em">Detalle técnico</div>
            ${techRows}
            ${hint}`;
    }

    const promptHtml = item.prompt_preview
        ? `<div style="font-size:0.78rem;color:var(--text-dim);margin-top:0.8rem">Prompt (preview)</div><pre>${escHtml(item.prompt_preview)}</pre>`
        : "";
    const responseHtml = item.response_preview
        ? `<div style="font-size:0.78rem;color:var(--text-dim)">Respuesta (preview)</div><pre>${escHtml(item.response_preview)}</pre>`
        : "";
    const errorHtml = item.error_message
        ? `<div style="font-size:0.78rem;color:#fca5a5;margin-top:0.8rem;text-transform:uppercase;letter-spacing:0.04em">Error</div><pre style="color:#fecaca;background:rgba(127,29,29,0.25);border:1px solid rgba(220,38,38,0.35);white-space:pre-wrap;word-break:break-word">${escHtml(item.error_message)}</pre>`
        : "";
    const noPreviewHint = (!promptHtml && !responseHtml && !errorHtml && !technicalHtml)
        ? `<div class="admin-empty" style="margin-top:0.8rem">Sin preview disponible. Los errores de Ollama guardan el prompt automáticamente; para el resto habilitá <code>AI_LOG_PREVIEWS=1</code>.</div>`
        : "";

    host.innerHTML = `
        <div class="admin-modal-backdrop" id="admin-modal-bd">
            <div class="admin-modal" onclick="event.stopPropagation()">
                <button class="admin-modal-close" id="admin-modal-close">&times;</button>
                <h3>Invocación IA #${item.id || ""}</h3>
                ${rowsHtml}
                ${technicalHtml}
                ${errorHtml}
                ${promptHtml}
                ${responseHtml}
                ${noPreviewHint}
            </div>
        </div>`;
    const bd = $("#admin-modal-bd");
    const close = () => { host.innerHTML = ""; };
    if (bd) bd.addEventListener("click", close);
    const btn = $("#admin-modal-close");
    if (btn) btn.addEventListener("click", close);
}

function _modalRow(label, value) {
    return `<div class="admin-modal-row"><span class="label">${escHtml(label)}</span><span>${escHtml(value == null ? "—" : String(value))}</span></div>`;
}

// ── Process events (scheduler + lifecycle) ──────────────────────────────

const _componentLabels = {
    scheduler: "Scheduler",
    ai: "IA",
    rss: "RSS",
    lifespan: "Ciclo de vida",
    railway: "Railway",
};

async function loadProcessEvents() {
    const wrap = $("#process-events-wrap");
    const pager = $("#process-events-pagination");
    if (!wrap) return;

    const compSel = $("#proc-component");
    const statSel = $("#proc-status");

    const params = new URLSearchParams();
    params.set("page", String(_processEventsPage));
    params.set("page_size", String(_PAGE_SIZE));
    if (compSel && compSel.value) params.set("component", compSel.value);
    if (statSel && statSel.value) params.set("status", statSel.value);

    try {
        const resp = await fetch(`/api/admin/process-events?${params}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (resp.status === 404) {
            wrap.innerHTML = `<div class="admin-empty">Endpoint no disponible (requiere Fase 2)</div>`;
            if (pager) pager.innerHTML = "";
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        _populateProcessFilters(data.filters || {});
        renderProcessEvents(data.items || [], data.total || 0, data.page || 1, data.page_size || _PAGE_SIZE);
    } catch (err) {
        console.error("Process events load failed:", err);
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red</div>`;
    }
}

function _populateProcessFilters(filters) {
    const compSel = $("#proc-component");
    if (compSel && !compSel.dataset.initialized && filters.components) {
        const current = compSel.value;
        compSel.innerHTML = `<option value="">Todos los componentes</option>` +
            filters.components.map(c => `<option value="${escHtml(c)}" ${c === current ? "selected" : ""}>${escHtml(_componentLabels[c] || c)}</option>`).join("");
        compSel.dataset.initialized = "1";
    }
}

function renderProcessEvents(items, total, page, pageSize) {
    const wrap = $("#process-events-wrap");
    const pager = $("#process-events-pagination");
    if (!items.length) {
        wrap.innerHTML = `<div class="admin-empty">No hay eventos registrados</div>`;
        if (pager) pager.innerHTML = "";
        return;
    }

    const rows = items.map(it => {
        const status = (it.status || "info").toLowerCase();
        const statusClass = `admin-status-${status}`;
        const dur = it.duration_ms != null ? `${it.duration_ms} ms` : "—";
        const msg = it.message || "";
        return `
            <tr>
                <td style="white-space:nowrap">${escHtml(formatDatetimeART(it.created_at))}</td>
                <td>${escHtml(_componentLabels[it.component] || it.component || "—")}</td>
                <td style="font-family:ui-monospace,monospace;font-size:0.72rem">${escHtml(it.event_type || "")}</td>
                <td><span class="admin-status-pill ${statusClass}">${escHtml(status.toUpperCase())}</span></td>
                <td style="font-size:0.72rem;color:var(--text-dim)">${dur}</td>
                <td style="font-size:0.72rem;word-break:break-word">${escHtml(msg).slice(0, 300)}</td>
            </tr>`;
    }).join("");

    wrap.innerHTML = `
        <table class="admin-table">
            <thead><tr><th>Cuándo</th><th>Componente</th><th>Evento</th><th>Estado</th><th>Duración</th><th>Mensaje</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;

    _renderPagination(pager, total, page, pageSize, (newPage) => {
        _processEventsPage = newPage;
        loadProcessEvents();
    });
}

// ── Infra costs (Railway) ───────────────────────────────────────────────

async function loadInfraCosts() {
    const wrap = $("#infra-costs-wrap");
    if (!wrap) return;
    try {
        const resp = await fetch("/api/admin/infra-costs");
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (resp.status === 404) {
            wrap.innerHTML = `<div class="admin-empty">Endpoint no disponible (requiere Fase 3)</div>`;
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderInfraCosts(data);
    } catch (err) {
        console.error("Infra costs load failed:", err);
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red</div>`;
    }
}

function _reasonMessage(reason) {
    const r = String(reason || "").toLowerCase();
    if (r === "no_token") return "Integración no configurada. Definí <code>RAILWAY_API_TOKEN</code> y <code>RAILWAY_PROJECT_ID</code> para ver el consumo.";
    if (r === "http_401" || r === "http_403") return "Railway rechazó el token (401/403). Asegurate de usar un <b>Account/Team Token</b>, no un Project Token, y que tenga acceso al proyecto configurado.";
    if (r === "http_404") return "Railway devolvió 404. Revisá que <code>RAILWAY_PROJECT_ID</code> sea correcto (UUID del proyecto).";
    if (r === "http_400") return "Railway devolvió 400. Probablemente cambió el schema GraphQL; hay que actualizar la query en <code>app/railway_client.py</code>.";
    if (r === "timeout") return "Railway API timeout. Reintentá en unos segundos.";
    if (r.startsWith("http_")) return `Railway respondió con un error HTTP (${escHtml(reason)}).`;
    if (r.startsWith("error")) return `Error al llamar a Railway: ${escHtml(reason)}`;
    return `No disponible: ${escHtml(reason)}`;
}

function renderInfraCosts(data) {
    const wrap = $("#infra-costs-wrap");
    if (!wrap) return;

    if (!data.available) {
        wrap.innerHTML = `<div class="admin-empty">${_reasonMessage(data.reason)}</div>`;
        return;
    }

    const services = data.services || [];
    const totalMonth = services.reduce((a, s) => a + (s.estimated_usd_month || 0), 0);
    const fetchedAt = data.fetched_at ? formatDatetimeART(data.fetched_at) : "—";

    const emptySnapshotWarning = (!services.length && !data.fetched_at)
        ? `<div class="admin-empty" style="margin-bottom:0.8rem;background:rgba(234,88,12,0.12);color:#ea580c;border-radius:6px;padding:0.6rem 0.8rem">
             Configurado pero sin snapshot todavía. El refresh automático corre cada hora; probá <b>Actualizar ahora</b> para ver el motivo si hay un error.
           </div>`
        : "";

    const serviceRows = services.length
        ? services.map(s => `
            <tr>
                <td>${escHtml(s.service_name || s.service_id || "—")}</td>
                <td style="font-weight:600">${fmtUSD(s.estimated_usd_month || 0)}</td>
            </tr>`).join("")
        : `<tr><td colspan="2" class="admin-empty">Sin servicios reportados</td></tr>`;

    const history = data.history || [];
    const historyHtml = history.length
        ? `<div style="margin-top:0.8rem">
            <div style="font-size:0.75rem;color:var(--text-dim);margin-bottom:0.4rem">Historial diario</div>
            <table class="admin-table">
                <thead><tr><th>Día</th><th>Estimado mensual</th></tr></thead>
                <tbody>${history.slice(0, 14).map(h => `<tr><td>${escHtml(formatDay(h.day))}</td><td>${fmtUSD(h.estimated_usd_month || 0)}</td></tr>`).join("")}</tbody>
            </table>
        </div>`
        : "";

    wrap.innerHTML = `
        ${emptySnapshotWarning}
        <div class="admin-kpis" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:0.8rem">
            <div class="admin-kpi">
                <div class="admin-kpi-value">${fmtUSD(totalMonth)}</div>
                <div class="admin-kpi-label">Estimado mensual</div>
                <div class="admin-kpi-desc">Actualizado ${escHtml(fetchedAt)}</div>
            </div>
            <div class="admin-kpi">
                <div class="admin-kpi-value">${services.length}</div>
                <div class="admin-kpi-label">Servicios</div>
                <div class="admin-kpi-desc">Activos en el proyecto</div>
            </div>
        </div>
        <table class="admin-table">
            <thead><tr><th>Servicio</th><th>Estimado mensual</th></tr></thead>
            <tbody>${serviceRows}</tbody>
        </table>
        ${historyHtml}`;
}

async function refreshInfraCostsNow() {
    const btn = $("#infra-costs-refresh");
    const wrap = $("#infra-costs-wrap");
    if (!btn || !wrap) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Consultando Railway…";
    try {
        const resp = await fetch("/api/admin/infra-costs/refresh", { method: "POST" });
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (!data.available) {
            wrap.innerHTML = `
                <div class="admin-empty" style="color:#ea580c">
                    <div style="margin-bottom:0.4rem"><b>El fetch a Railway falló.</b></div>
                    ${_reasonMessage(data.reason)}
                </div>`;
            return;
        }

        if ((data.saved_rows || 0) > 0) {
            await loadInfraCosts();
        } else {
            wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">
                Railway respondió OK pero no devolvió servicios para guardar. Revisá el <code>RAILWAY_PROJECT_ID</code>.
            </div>`;
        }
    } catch (err) {
        console.error("Infra costs manual refresh failed:", err);
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red: ${escHtml(err.message || err)}</div>`;
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// ── Ollama service logs (Railway) ───────────────────────────────────────

function _ollamaLogsReasonMessage(data) {
    const reason = String(data.reason || "").toLowerCase();
    if (reason === "no_token") {
        return "Integración con Railway no configurada. Definí <code>RAILWAY_API_TOKEN</code> y <code>RAILWAY_PROJECT_ID</code> para poder traer los logs.";
    }
    if (reason === "service_not_found") {
        const known = (data.known_services || []).filter(Boolean);
        const knownHtml = known.length
            ? `Servicios detectados en el proyecto: ${known.map(n => `<code>${escHtml(n)}</code>`).join(", ")}.`
            : "No se encontró ningún servicio en el proyecto.";
        return `No existe un servicio llamado <code>${escHtml(data.service_name || "ollama")}</code>. ${knownHtml} Definí <code>RAILWAY_OLLAMA_SERVICE_NAME</code> con el nombre correcto.`;
    }
    if (reason === "no_deployment") {
        return `El servicio <code>${escHtml(data.service_name || "ollama")}</code> existe pero todavía no tiene deployments.`;
    }
    if (reason === "timeout") return "Railway API timeout. Reintentá en unos segundos.";
    if (reason.startsWith("http_401") || reason.startsWith("http_403")) {
        return "Railway rechazó el token (401/403). Asegurate de usar un <b>Account/Team Token</b> con acceso al proyecto.";
    }
    if (reason.startsWith("http_404")) {
        return "Railway devolvió 404. Revisá que <code>RAILWAY_PROJECT_ID</code> sea correcto.";
    }
    if (reason.startsWith("http_400")) {
        return "Railway devolvió 400. Probablemente cambió el schema GraphQL; hay que actualizar <code>app/railway_client.py</code>.";
    }
    if (reason.startsWith("http_")) return `Railway respondió con un error HTTP (${escHtml(reason)}).`;
    if (reason.startsWith("error")) return `Error al llamar a Railway: ${escHtml(reason)}`;
    return `No disponible: ${escHtml(reason || "unknown")}`;
}

async function loadOllamaLogs() {
    const wrap = $("#ollama-logs-wrap");
    const meta = $("#ollama-logs-meta");
    const btn = $("#ollama-logs-refresh");
    if (!wrap) return;

    const limit = parseInt(($("#ollama-logs-limit") || {}).value, 10) || 200;
    const filter = (($("#ollama-logs-filter") || {}).value || "").trim();

    const params = new URLSearchParams();
    params.set("limit", String(limit));
    if (filter) params.set("filter", filter);

    wrap.innerHTML = `<div class="admin-loading"><div class="spinner"></div></div>`;
    if (meta) meta.textContent = "";
    if (btn) { btn.disabled = true; btn.textContent = "Consultando Railway…"; }

    try {
        const resp = await fetch(`/api/admin/ollama-logs?${params}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        if (resp.status === 404) {
            wrap.innerHTML = `<div class="admin-empty">Endpoint no disponible.</div>`;
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderOllamaLogs(data);
    } catch (err) {
        console.error("Ollama logs load failed:", err);
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">Error de red: ${escHtml(err.message || err)}</div>`;
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Recargar"; }
    }
}

function renderOllamaLogs(data) {
    const wrap = $("#ollama-logs-wrap");
    const meta = $("#ollama-logs-meta");
    if (!wrap) return;

    if (!data.available) {
        wrap.innerHTML = `<div class="admin-empty" style="color:#ea580c">${_ollamaLogsReasonMessage(data)}</div>`;
        if (meta) meta.innerHTML = "";
        return;
    }

    const logs = data.logs || [];
    if (meta) {
        meta.innerHTML = `Servicio <code>${escHtml(data.service_name || "—")}</code> · deployment <code>${escHtml((data.deployment_id || "—").slice(0, 12))}</code> · ${logs.length} línea${logs.length === 1 ? "" : "s"}`;
    }

    if (!logs.length) {
        wrap.innerHTML = `<div class="admin-empty">Sin logs para mostrar (probá sin filtro o con más líneas).</div>`;
        return;
    }

    // Render oldest-first so the most recent log line ends up at the bottom,
    // matching how a `tail -f` looks and what admins expect when diagnosing.
    const ordered = logs.slice().sort((a, b) => {
        const ta = a.timestamp || "";
        const tb = b.timestamp || "";
        return ta.localeCompare(tb);
    });

    const lines = ordered.map(l => {
        const ts = l.timestamp ? _shortLogTime(l.timestamp) : "        ";
        const sev = (l.severity || "").toUpperCase();
        const sevColor = _logSeverityColor(sev);
        const sevLabel = sev ? `<span style="color:${sevColor}">[${escHtml(sev.padEnd(5).slice(0, 5))}]</span>` : "";
        return `<span style="color:var(--text-dim)">${escHtml(ts)}</span> ${sevLabel} ${escHtml(l.message || "")}`;
    }).join("\n");

    wrap.innerHTML = `
        <pre id="ollama-logs-pre" style="max-height:480px;overflow:auto;background:rgba(0,0,0,0.25);border:1px solid var(--border);border-radius:6px;padding:0.8rem;font-size:0.72rem;line-height:1.4;white-space:pre-wrap;word-break:break-word">${lines}</pre>`;

    // Autoscroll to bottom (most recent line).
    const pre = $("#ollama-logs-pre");
    if (pre) pre.scrollTop = pre.scrollHeight;
}

function _shortLogTime(ts) {
    try {
        const d = new Date(ts);
        if (isNaN(d.getTime())) return ts.slice(11, 19) || ts.slice(0, 8);
        const hh = String(d.getHours()).padStart(2, "0");
        const mm = String(d.getMinutes()).padStart(2, "0");
        const ss = String(d.getSeconds()).padStart(2, "0");
        return `${hh}:${mm}:${ss}`;
    } catch {
        return ts;
    }
}

function _logSeverityColor(sev) {
    switch (sev) {
        case "ERROR":
        case "FATAL":
        case "CRIT":
        case "CRITICAL":
            return "#fca5a5";
        case "WARN":
        case "WARNING":
            return "#fbbf24";
        case "INFO":
            return "#93c5fd";
        case "DEBUG":
        case "TRACE":
            return "var(--text-dim)";
        default:
            return "var(--text-dim)";
    }
}

// ── Pagination helper ───────────────────────────────────────────────────

function _renderPagination(container, total, page, pageSize, onChange) {
    if (!container) return;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (totalPages <= 1) {
        container.innerHTML = `<span>${total} resultado${total === 1 ? "" : "s"}</span>`;
        return;
    }
    container.innerHTML = `
        <span>${total} resultados · Página ${page} de ${totalPages}</span>
        <button ${page <= 1 ? "disabled" : ""} data-action="prev">← Anterior</button>
        <button ${page >= totalPages ? "disabled" : ""} data-action="next">Siguiente →</button>`;
    const prev = container.querySelector('[data-action="prev"]');
    const next = container.querySelector('[data-action="next"]');
    if (prev) prev.addEventListener("click", () => onChange(page - 1));
    if (next) next.addEventListener("click", () => onChange(page + 1));
}

// ── Shared rendering ────────────────────────────────────────────────────

function renderBarChart(containerId, items, colorClass) {
    const container = $(`#${containerId}`);
    if (!items || !items.length) {
        container.innerHTML = `<div class="admin-empty">Sin datos</div>`;
        return;
    }
    const max = items[0].count || 1;
    container.innerHTML = items.map(item => {
        const pct = Math.max(5, (item.count / max) * 100);
        return `
            <div class="admin-bar-row">
                <span class="admin-bar-label" title="${escHtml(item.label)}">${escHtml(item.label)}</span>
                <div class="admin-bar-track">
                    <div class="admin-bar-fill ${colorClass}" style="width:${pct}%">${item.count}</div>
                </div>
            </div>`;
    }).join("");
}

// ── Helpers ──────────────────────────────────────────────────────────────

function formatDuration(seconds) {
    if (!seconds || seconds < 1) return "0s";
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return s ? `${m}m ${s}s` : `${m}m`;
}

function formatDay(iso) {
    if (!iso) return "-";
    try {
        const [y, m, d] = iso.split("-");
        const date = new Date(+y, +m - 1, +d);
        return date.toLocaleDateString("es-AR", { weekday: "short", day: "numeric", month: "short" });
    } catch {
        return iso;
    }
}

// ARG tz is forced in render options so the admin panel shows the same time
// regardless of the browser's local timezone (e.g. UTC on a remote dev box).
const _ARG_TZ = "America/Argentina/Buenos_Aires";
const _FMT_OPTS = {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    timeZone: _ARG_TZ,
};

// For timestamps stored as UTC-naive (user_store, tracking_store): append "Z"
// so the JS Date parses as UTC, then render in ARG.
function formatDatetime(iso) {
    if (!iso) return "-";
    try {
        const raw = iso.includes("T") && !iso.includes("Z") && !iso.includes("+") ? iso + "Z" : iso;
        const d = new Date(raw);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString("es-AR", _FMT_OPTS);
    } catch {
        return iso;
    }
}

// For timestamps stored as ARG-naive (ai_store uses datetime.now(ART)):
// append the ARG offset so JS parses them correctly, then render in ARG.
function formatDatetimeART(iso) {
    if (!iso) return "-";
    try {
        let raw = iso;
        if (iso.includes("T") && !iso.includes("Z") && !/[+-]\d{2}:?\d{2}$/.test(iso)) {
            raw = iso + "-03:00";
        }
        const d = new Date(raw);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString("es-AR", _FMT_OPTS);
    } catch {
        return iso;
    }
}

function escHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}


// ── X (Twitter) tab ─────────────────────────────────────────────────────────

let _xStatusCache = null;
let _xUsagePage = 1;
const _X_CAMPAIGN_LABELS = {
    cloud: { title: "Nube del día", subtitle: "Renderiza la nube como PNG y la postea con las top palabras." },
    topstory: { title: "Noticia del día", subtitle: "Usa el análisis IA del grupo más cubierto." },
    weekly: { title: "Resumen semanal", subtitle: "Publica un hilo con los temas de la semana." },
    topics: { title: "Temas del día", subtitle: "Hilo breve con los trending topics detectados." },
    breaking: { title: "Breaking news", subtitle: "Se dispara cuando aparece un grupo con N+ fuentes." },
};
const _X_STATUS_LABELS = {
    ok: "OK",
    error: "Error",
    rate_limited: "Rate limit",
    quota_exceeded: "Cupo superado",
    disabled_by_tier: "Bloqueado (tier)",
    skipped: "Saltado",
};
const _X_DAY_LABELS = { mon: "Lunes", tue: "Martes", wed: "Miércoles", thu: "Jueves", fri: "Viernes", sat: "Sábado", sun: "Domingo" };

async function loadXStatus() {
    const host = $("#x-status-wrap");
    if (!host) return;
    try {
        const resp = await fetch("/api/admin/x-status");
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();
        _xStatusCache = data;
        host.innerHTML = renderXStatus(data);
        bindXStatusEvents();
    } catch (err) {
        host.innerHTML = `<div class="admin-empty">Error cargando estado: ${escHtml(err.message || err)}</div>`;
    }
}

const _X_TIER_LABELS = {
    disabled: "Apagado",
    basic: "Basic",
    pro: "Pro",
    pay_per_use: "Pay per Use",
};

function _xTierLabel(t) {
    if (_X_TIER_LABELS[t]) return _X_TIER_LABELS[t];
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
}

function renderXStatus(data) {
    const tier = data.tier || {};
    const usage = data.usage || {};
    const defaults = data.tier_defaults || {};
    const tierOpts = (data.valid_tiers || ["disabled", "basic", "pro", "pay_per_use"])
        .map(t => `<option value="${t}" ${t === tier.tier ? "selected" : ""}>${_xTierLabel(t)}</option>`)
        .join("");

    const dailyPct = tier.daily_cap > 0 ? Math.min(100, (usage.posts_today / tier.daily_cap) * 100) : 0;
    const monthPct = tier.monthly_cap > 0 ? Math.min(100, (usage.posts_this_month / tier.monthly_cap) * 100) : 0;
    const dailyClass = dailyPct >= 90 ? "danger" : dailyPct >= 70 ? "warn" : "";
    const monthClass = monthPct >= 90 ? "danger" : monthPct >= 70 ? "warn" : "";
    const configBadge = data.configured
        ? `<span class="x-status-badge ok">Conectada</span>`
        : `<span class="x-status-badge error">Sin tokens</span>`;

    const disabledWarn = tier.tier === "disabled"
        ? `<div class="x-tier-warn">Tier <strong>Apagado</strong>: kill-switch interno. Ninguna campaña postea. Cambiá a Basic/Pro/Pay per Use para habilitar posteo.</div>`
        : "";

    const readOnly = tier.tier !== "pay_per_use" && tier.tier !== "disabled";
    const capReadOnly = tier.tier === "disabled" ? "disabled" : (readOnly ? "readonly" : "");

    return `
        <div class="x-status-grid">
            <div class="x-status-cell">
                <div class="label">Cuenta</div>
                <div class="value">${escHtml(data.handle || "—")} ${configBadge}</div>
                <div class="hint">${data.token_updated_at ? "Token actualizado: " + escHtml(data.token_updated_at) : "Sin refresh registrado"}</div>
                <div style="margin-top:0.5rem"><button class="btn-secondary" id="x-refresh-handle">Refrescar handle</button></div>
            </div>
            <div class="x-status-cell">
                <div class="label">Posts hoy</div>
                <div class="value">${usage.posts_today ?? 0} / ${tier.daily_cap || "∞"}</div>
                <div class="x-progress-row">
                    <div class="x-progress-track"><div class="x-progress-fill ${dailyClass}" style="width:${dailyPct}%"></div></div>
                    <div class="x-progress-label">${Math.round(dailyPct)}%</div>
                </div>
            </div>
            <div class="x-status-cell">
                <div class="label">Posts este mes</div>
                <div class="value">${usage.posts_this_month ?? 0} / ${tier.monthly_cap || "∞"}</div>
                <div class="x-progress-row">
                    <div class="x-progress-track"><div class="x-progress-fill ${monthClass}" style="width:${monthPct}%"></div></div>
                    <div class="x-progress-label">${Math.round(monthPct)}%</div>
                </div>
            </div>
            <div class="x-status-cell">
                <div class="label">Costo mensual estimado</div>
                <div class="value">USD ${Number(tier.monthly_usd || 0).toFixed(2)}</div>
                <div class="hint">Según plan contratado en X Developer Portal</div>
            </div>
        </div>

        <div class="x-tier-form">
            <label>Tier
                <select id="x-tier-select">${tierOpts}</select>
            </label>
            <label>Daily cap
                <input type="number" min="0" id="x-daily-cap" value="${tier.daily_cap ?? 0}" ${capReadOnly} />
            </label>
            <label>Monthly cap
                <input type="number" min="0" id="x-monthly-cap" value="${tier.monthly_cap ?? 0}" ${capReadOnly} />
            </label>
            <label>Costo mensual (USD)
                <input type="number" min="0" step="0.01" id="x-monthly-usd" value="${Number(tier.monthly_usd || 0)}" ${tier.tier === "disabled" ? "disabled" : ""} />
            </label>
            <div>
                <button class="btn-primary" id="x-save-tier">Guardar tier</button>
            </div>
        </div>
        ${disabledWarn}

        <div class="x-hint" data-defaults='${JSON.stringify(defaults)}'>
            Apagado: kill-switch · Basic: 50/día, 1500/mes · Pro: 10k/día, 300k/mes · Pay per Use: caps manuales (~USD 0.01 por tweet).
        </div>
    `;
}

function bindXStatusEvents() {
    const sel = $("#x-tier-select");
    if (sel) sel.addEventListener("change", onXTierChange);
    const saveBtn = $("#x-save-tier");
    if (saveBtn) saveBtn.addEventListener("click", saveXTier);
    const refreshBtn = $("#x-refresh-handle");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshXHandle);
}

function onXTierChange(ev) {
    const tier = ev.target.value;
    const defaults = (_xStatusCache && _xStatusCache.tier_defaults) || {};
    const def = defaults[tier] || {};
    const dailyInput = $("#x-daily-cap");
    const monthlyInput = $("#x-monthly-cap");
    const usdInput = $("#x-monthly-usd");
    if (dailyInput) dailyInput.value = def.daily_cap ?? 0;
    if (monthlyInput) monthlyInput.value = def.monthly_cap ?? 0;
    if (usdInput) usdInput.value = def.monthly_usd ?? 0;

    const readOnly = tier !== "pay_per_use" && tier !== "disabled";
    const isDisabled = tier === "disabled";
    [dailyInput, monthlyInput].forEach(el => {
        if (!el) return;
        el.readOnly = readOnly && !isDisabled;
        el.disabled = isDisabled;
    });
    if (usdInput) usdInput.disabled = isDisabled;
}

async function saveXTier() {
    const tier = $("#x-tier-select").value;
    const payload = {
        tier,
        daily_cap: Number($("#x-daily-cap").value || 0),
        monthly_cap: Number($("#x-monthly-cap").value || 0),
        monthly_usd: Number($("#x-monthly-usd").value || 0),
    };
    const btn = $("#x-save-tier");
    if (btn) { btn.disabled = true; btn.textContent = "Guardando…"; }
    try {
        const resp = await fetch("/api/admin/x-tier", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || "Error guardando tier");
            return;
        }
        await loadXStatus();
        await loadXCampaigns();
    } catch (err) {
        alert("Error: " + (err.message || err));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Guardar tier"; }
    }
}

async function refreshXHandle() {
    const btn = $("#x-refresh-handle");
    if (btn) { btn.disabled = true; btn.textContent = "Consultando…"; }
    try {
        const resp = await fetch("/api/admin/x-refresh-handle", { method: "POST" });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.message || data.error || "No se pudo consultar /users/me");
        }
        await loadXStatus();
    } catch (err) {
        alert("Error: " + (err.message || err));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Refrescar handle"; }
    }
}

async function loadXCampaigns() {
    const host = $("#x-campaigns-wrap");
    if (!host) return;
    try {
        const resp = await fetch("/api/admin/x-campaigns");
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();
        host.innerHTML = (data.items || []).map(renderXCampaignCard).join("") || `<div class="admin-empty">No hay campañas configuradas.</div>`;
        (data.items || []).forEach(c => bindXCampaignEvents(c.campaign_key));

        const sel = $("#x-usage-filter-campaign");
        if (sel) {
            const current = sel.value;
            sel.innerHTML = `<option value="">Todas las campañas</option>` +
                (data.valid_keys || []).map(k => `<option value="${k}" ${k === current ? "selected" : ""}>${_X_CAMPAIGN_LABELS[k]?.title || k}</option>`).join("");
        }
    } catch (err) {
        host.innerHTML = `<div class="admin-empty">Error cargando campañas: ${escHtml(err.message || err)}</div>`;
    }
}

function renderXCampaignCard(c) {
    const label = _X_CAMPAIGN_LABELS[c.campaign_key] || { title: c.campaign_key, subtitle: "" };
    const schedule = c.schedule || {};
    const template = c.template || {};
    const last = c.last_run_at
        ? `Último run: <span class="x-status-badge ${c.last_run_status || 'skipped'}">${escHtml(_X_STATUS_LABELS[c.last_run_status] || c.last_run_status || 'n/d')}</span> <span style="opacity:0.7">${escHtml(c.last_run_at)}</span>`
        : `Nunca ejecutada`;

    let scheduleHtml = "";
    if (c.campaign_key === "breaking") {
        const cats = (schedule.categories || []).join(", ");
        scheduleHtml = `
            <label>Mín. fuentes distintas<input type="number" min="1" max="20" data-field="min_source_count" value="${schedule.min_source_count ?? 3}" /></label>
            <label>Cooldown (min)<input type="number" min="0" max="1440" data-field="cooldown_minutes" value="${schedule.cooldown_minutes ?? 60}" /></label>
            <label style="grid-column:1/-1">Categorías permitidas (coma separadas)
                <input type="text" data-field="categories" value="${escHtml(cats)}" placeholder="Política, Economía" />
            </label>
        `;
    } else {
        const timeVal = `${String(schedule.hour ?? 9).padStart(2, "0")}:${String(schedule.minute ?? 0).padStart(2, "0")}`;
        scheduleHtml = `<label>Hora ART<input type="time" data-field="time" value="${timeVal}" /></label>`;
        if (c.campaign_key === "weekly") {
            const dow = schedule.day_of_week || "mon";
            const dowOpts = Object.entries(_X_DAY_LABELS).map(([k, v]) => `<option value="${k}" ${k === dow ? "selected" : ""}>${v}</option>`).join("");
            scheduleHtml += `<label>Día de la semana<select data-field="day_of_week">${dowOpts}</select></label>`;
        }
    }

    const attachImageRow = c.campaign_key === "cloud" || c.campaign_key === "breaking"
        ? `<label style="grid-column:1/-1;flex-direction:row;gap:0.5rem;align-items:center">
               <input type="checkbox" data-field="attach_image" ${template.attach_image ? "checked" : ""} />
               Adjuntar imagen ${c.campaign_key === "cloud" ? "(nube del día PNG)" : ""}
           </label>`
        : "";

    const threadRow = (c.campaign_key === "weekly" || c.campaign_key === "topics")
        ? `<label style="flex-direction:row;gap:0.5rem;align-items:center">
               <input type="checkbox" data-field="thread" ${template.thread ? "checked" : ""} /> Postear como hilo
           </label>
           <label>Máx. posts en hilo<input type="number" min="1" max="10" data-field="thread_max_posts" value="${template.thread_max_posts ?? 4}" /></label>`
        : "";

    return `
        <div class="x-campaign-card" data-campaign="${c.campaign_key}">
            <div class="x-campaign-head">
                <div>
                    <div class="x-campaign-title">${escHtml(label.title)}</div>
                    <div class="x-campaign-subtitle">${escHtml(label.subtitle)}</div>
                </div>
                <label class="x-toggle">
                    <input type="checkbox" data-field="enabled" ${c.enabled ? "checked" : ""} />
                    <span class="x-toggle-track"></span>
                    <span class="x-toggle-label">${c.enabled ? "Activa" : "Deshabilitada"}</span>
                </label>
            </div>
            <div class="x-campaign-body">
                ${scheduleHtml}
                ${threadRow}
                <label style="grid-column:1/-1">Texto (plantilla)
                    <textarea data-field="text" rows="3">${escHtml(template.text || "")}</textarea>
                </label>
                <label style="grid-column:1/-1">Hashtags
                    <input type="text" data-field="hashtags" value="${escHtml(template.hashtags || "")}" />
                </label>
                ${attachImageRow}
            </div>
            <div class="x-campaign-actions">
                <button class="btn-primary" data-action="save">Guardar</button>
                <button class="btn-secondary" data-action="test">Probar ahora</button>
                <span class="x-last-run">${last}</span>
            </div>
        </div>
    `;
}

function bindXCampaignEvents(key) {
    const card = document.querySelector(`.x-campaign-card[data-campaign="${key}"]`);
    if (!card) return;
    const toggleLabel = card.querySelector(".x-toggle-label");
    const toggleInput = card.querySelector('input[data-field="enabled"]');
    if (toggleInput && toggleLabel) {
        toggleInput.addEventListener("change", () => {
            toggleLabel.textContent = toggleInput.checked ? "Activa" : "Deshabilitada";
        });
    }
    card.querySelector('[data-action="save"]').addEventListener("click", () => saveXCampaign(key));
    card.querySelector('[data-action="test"]').addEventListener("click", () => testXCampaign(key));
}

function readXCampaignForm(key) {
    const card = document.querySelector(`.x-campaign-card[data-campaign="${key}"]`);
    if (!card) return null;
    const enabled = card.querySelector('input[data-field="enabled"]').checked;

    const schedule = {};
    if (key === "breaking") {
        schedule.min_source_count = Number(card.querySelector('[data-field="min_source_count"]').value || 3);
        schedule.cooldown_minutes = Number(card.querySelector('[data-field="cooldown_minutes"]').value || 60);
        const catsRaw = card.querySelector('[data-field="categories"]').value || "";
        schedule.categories = catsRaw.split(",").map(s => s.trim()).filter(Boolean);
    } else {
        const timeVal = card.querySelector('[data-field="time"]').value || "09:00";
        const [h, m] = timeVal.split(":");
        schedule.hour = Number(h);
        schedule.minute = Number(m);
        if (key === "weekly") {
            schedule.day_of_week = card.querySelector('[data-field="day_of_week"]').value || "mon";
        }
    }

    const template = {
        text: card.querySelector('[data-field="text"]').value || "",
        hashtags: card.querySelector('[data-field="hashtags"]').value || "",
    };
    const attach = card.querySelector('[data-field="attach_image"]');
    if (attach) template.attach_image = attach.checked;
    const thread = card.querySelector('[data-field="thread"]');
    if (thread) template.thread = thread.checked;
    const tmp = card.querySelector('[data-field="thread_max_posts"]');
    if (tmp) template.thread_max_posts = Number(tmp.value || 4);

    return { campaign_key: key, enabled, schedule, template };
}

async function saveXCampaign(key) {
    const payload = readXCampaignForm(key);
    if (!payload) return;
    const card = document.querySelector(`.x-campaign-card[data-campaign="${key}"]`);
    const btn = card.querySelector('[data-action="save"]');
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Guardando…";
    try {
        const resp = await fetch("/api/admin/x-campaigns", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || "Error guardando campaña");
            return;
        }
        await loadXCampaigns();
    } catch (err) {
        alert("Error: " + (err.message || err));
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
}

async function testXCampaign(key) {
    const card = document.querySelector(`.x-campaign-card[data-campaign="${key}"]`);
    const btn = card.querySelector('[data-action="test"]');
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Ejecutando…";
    try {
        const resp = await fetch("/api/admin/x-test-post", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ campaign_key: key }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || "Error ejecutando test");
        } else if (data.ok) {
            alert(`Posteado correctamente. Tweets: ${(data.post_ids || []).join(", ")}`);
        } else {
            alert(`No se posteó (${data.status}): ${data.reason || data.message || "sin detalle"}`);
        }
        await loadXStatus();
        await loadXUsage();
        await loadXCampaigns();
    } catch (err) {
        alert("Error: " + (err.message || err));
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
}

async function loadXUsage() {
    const host = $("#x-usage-wrap");
    if (!host) return;

    const filterCampaign = $("#x-usage-filter-campaign")?.value || "";
    const filterStatus = $("#x-usage-filter-status")?.value || "";

    const params = new URLSearchParams();
    params.set("page", _xUsagePage);
    params.set("page_size", 25);
    if (filterCampaign) params.set("campaign_key", filterCampaign);
    if (filterStatus) params.set("status", filterStatus);

    try {
        const resp = await fetch(`/api/admin/x-usage?${params}`);
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();

        const sSel = $("#x-usage-filter-status");
        if (sSel && sSel.options.length <= 1) {
            sSel.innerHTML = `<option value="">Cualquier estado</option>` +
                (data.filters?.statuses || []).map(s => `<option value="${s}" ${s === filterStatus ? "selected" : ""}>${_X_STATUS_LABELS[s] || s}</option>`).join("");
        }

        if (!data.items || data.items.length === 0) {
            host.innerHTML = `<div class="admin-empty">No hay publicaciones registradas con estos filtros.</div>`;
        } else {
            host.innerHTML = `
                <table class="admin-table">
                    <thead>
                        <tr>
                            <th>Fecha</th>
                            <th>Campaña</th>
                            <th>Status</th>
                            <th>Post</th>
                            <th>Detalle</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.items.map(renderXUsageRow).join("")}
                    </tbody>
                </table>
            `;
        }
        renderXUsagePagination(data.total || 0, data.page, data.page_size);
    } catch (err) {
        host.innerHTML = `<div class="admin-empty">Error cargando historial: ${escHtml(err.message || err)}</div>`;
    }
}

function renderXUsageRow(r) {
    const label = _X_CAMPAIGN_LABELS[r.campaign_key]?.title || r.campaign_key;
    const statusLabel = _X_STATUS_LABELS[r.status] || r.status;
    const postCell = r.post_id
        ? `<a href="https://x.com/i/web/status/${escHtml(r.post_id.split(",")[0])}" target="_blank" rel="noopener">${escHtml(r.post_id.split(",")[0])}</a>${r.posts_count > 1 ? ` <span style="color:var(--text-dim)">(+${r.posts_count - 1})</span>` : ""}`
        : "—";
    const detail = r.error_message
        ? `<span style="color:#f87171">${escHtml(r.error_message)}</span>`
        : (r.preview ? `<span style="color:var(--text-dim)">${escHtml(r.preview.slice(0, 120))}${r.preview.length > 120 ? "…" : ""}</span>` : "—");
    return `
        <tr>
            <td>${escHtml(r.created_at || "")}</td>
            <td>${escHtml(label)}</td>
            <td><span class="x-status-badge ${r.status}">${escHtml(statusLabel)}</span></td>
            <td>${postCell}</td>
            <td>${detail}</td>
        </tr>
    `;
}

function renderXUsagePagination(total, page, pageSize) {
    const host = $("#x-usage-pagination");
    if (!host) return;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    host.innerHTML = `
        <span>${total} registros · página ${page} de ${totalPages}</span>
        <button ${page <= 1 ? "disabled" : ""} id="x-usage-prev">◀</button>
        <button ${page >= totalPages ? "disabled" : ""} id="x-usage-next">▶</button>
    `;
    const prev = $("#x-usage-prev");
    const next = $("#x-usage-next");
    if (prev) prev.addEventListener("click", () => { if (_xUsagePage > 1) { _xUsagePage--; loadXUsage(); } });
    if (next) next.addEventListener("click", () => { if (page < totalPages) { _xUsagePage++; loadXUsage(); } });
}

document.addEventListener("DOMContentLoaded", () => {
    const refresh = document.getElementById("x-usage-refresh");
    if (refresh) refresh.addEventListener("click", () => { _xUsagePage = 1; loadXUsage(); });
    const fc = document.getElementById("x-usage-filter-campaign");
    if (fc) fc.addEventListener("change", () => { _xUsagePage = 1; loadXUsage(); });
    const fs = document.getElementById("x-usage-filter-status");
    if (fs) fs.addEventListener("change", () => { _xUsagePage = 1; loadXUsage(); });
});
