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
let _activeTab = "general";

document.addEventListener("DOMContentLoaded", async () => {
    const resp = await fetch("/auth/me");
    const data = await resp.json();
    if (!data.user || data.user.role !== "admin") {
        window.location.href = "/";
        return;
    }

    setupTabs();
    setupDateFilters();
    loadAll();
});

function setupTabs() {
    $$("#admin-tabs .admin-tab").forEach(tab => {
        tab.addEventListener("click", () => {
            $$("#admin-tabs .admin-tab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            _activeTab = tab.dataset.tab;
            $$(".admin-tab-panel").forEach(p => p.classList.remove("active"));
            $(`#panel-${_activeTab}`).classList.add("active");
            loadAll();
        });
    });
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
    if (_activeTab === "general") {
        loadDashboard(desde, hasta);
        loadTopContent();
        loadSearches();
        loadHourly(desde, hasta);
        loadDaily(desde, hasta);
        loadUsers();
    } else if (_activeTab === "ai") {
        loadAIDashboard(desde, hasta);
        loadAIConfig();
        loadSchedulerConfig();
    } else {
        loadAnonymousDashboard(desde, hasta);
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
    gemini_fallback_groq: "Gemini (fallback Groq)",
    groq_fallback_gemini: "Groq (fallback Gemini)",
};

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

async function loadAIDashboard(desde, hasta) {
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
                const color = p.provider === "gemini" ? "#4088c7" : "#0d9488";
                const bg = p.provider === "gemini" ? "rgba(64,136,199,0.12)" : "rgba(13,148,136,0.12)";
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
        console.error("AI dashboard load failed:", err);
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
            <td><span class="admin-badge ${e.provider === "gemini" ? "admin-badge-admin" : "admin-badge-user"}">${escHtml(e.provider)}</span></td>
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

function formatDatetime(iso) {
    if (!iso) return "-";
    try {
        const raw = iso.includes("T") && !iso.includes("Z") && !iso.includes("+") ? iso + "Z" : iso;
        const d = new Date(raw);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString("es-AR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
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
