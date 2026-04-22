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
    } else if (_activeTab === "costs") {
        loadAICosts(desde, hasta);
        loadInfraCosts();
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
    gemini_fallback_groq: "Gemini (fallback Groq)",
    groq_fallback_gemini: "Groq (fallback Gemini)",
    gemini_fallback_ollama: "Gemini (fallback Ollama)",
    ollama_fallback_gemini: "Ollama (fallback Gemini)",
    groq_fallback_ollama: "Groq (fallback Ollama)",
    ollama_fallback_groq: "Ollama (fallback Groq)",
};

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

async function loadAICosts(desde, hasta) {
    try {
        const now = new Date();
        const mesDesde = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
        const mesHasta = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

        const [resp, respMes] = await Promise.all([
            fetch(`/api/admin/ai-cost${qs(desde, hasta)}`),
            fetch(`/api/admin/ai-cost${qs(mesDesde, mesHasta)}`),
        ]);
        if (resp.status === 403) { window.location.href = "/"; return; }
        const data = await resp.json();
        const dataMes = await respMes.json();

        const s = data.summary;
        const t = s.totals;

        $("#ai-kpi-calls").textContent = t.calls;
        $("#ai-kpi-cost").textContent = fmtUSD(t.cost_total);
        $("#ai-kpi-tokens-in").textContent = fmtTokens(t.input_tokens);
        $("#ai-kpi-tokens-out").textContent = fmtTokens(t.output_tokens);

        const mesTotals = dataMes.summary.totals;
        const costMes = mesTotals.cost_total || 0;
        const daysWithData = mesTotals.distinct_days || 0;
        const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
        const projected = daysWithData > 0 ? (costMes / daysWithData) * daysInMonth : 0;
        $("#ai-kpi-projection").textContent = fmtUSD(projected);

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

function renderAIConfig(config, validProviders, validEventTypes, schedule) {
    const container = $("#ai-config-wrap");
    if (!validEventTypes || !validEventTypes.length) {
        container.innerHTML = `<div class="admin-empty">No hay configuración disponible</div>`;
        return;
    }

    const selectStyle = "";

    const cards = validEventTypes.map(et => {
        const current = config[et] || "gemini_fallback_groq";
        const options = validProviders.map(p =>
            `<option value="${p}" ${p === current ? "selected" : ""}>${escHtml(_providerLabels[p] || p)}</option>`
        ).join("");

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

        return `
            <div class="admin-eng-card" style="flex-direction:column;align-items:stretch;gap:0.4rem">
                <div style="font-size:0.82rem;font-weight:600;color:var(--text)">${escHtml(_eventLabels[et] || et)}</div>
                <div style="font-size:0.68rem;color:var(--text-dim);margin-bottom:0.2rem">${escHtml(desc)}</div>
                <select class="ai-config-select" data-event="${et}" style="${selectStyle}">${options}</select>
                <div class="ai-config-status" data-event="${et}" style="font-size:0.65rem;min-height:1rem;color:var(--text-dim)"></div>
                ${scheduleHtml}
            </div>`;
    }).join("");

    container.innerHTML = `<div class="admin-engagement">${cards}</div>`;

    $$(".ai-config-select").forEach(sel => {
        sel.addEventListener("change", async () => {
            const et = sel.dataset.event;
            const provider = sel.value;
            const status = $(`.ai-config-status[data-event="${et}"]`);
            status.textContent = "Guardando...";
            status.style.color = "var(--text-dim)";
            try {
                const resp = await fetch("/api/admin/ai-config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ event_type: et, provider }),
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
            ? `<div style="margin-top:0.4rem;padding:0.6rem 0.8rem;background:rgba(234,88,12,0.10);border-radius:6px;font-size:0.78rem;color:#fca5a5">
                ${escHtml(_phaseHints[item.error_phase])}
               </div>`
            : "";
        technicalHtml = `
            <div style="font-size:0.78rem;color:var(--text-dim);margin-top:0.8rem">Detalle técnico</div>
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
        ? `<div style="font-size:0.78rem;color:#fca5a5;margin-top:0.8rem">Error</div><pre style="color:#fca5a5">${escHtml(item.error_message)}</pre>`
        : "";
    const noPreviewHint = (!promptHtml && !responseHtml && !errorHtml && !technicalHtml)
        ? `<div class="admin-empty" style="margin-top:0.8rem">Sin preview disponible. Habilitá <code>AI_LOG_PREVIEWS=1</code> para registrar previews.</div>`
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
