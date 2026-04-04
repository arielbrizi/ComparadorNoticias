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
