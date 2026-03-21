/* ── Comparador de Noticias — Frontend ─────────────────────────────────── */

const API = "";
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// ── State ─────────────────────────────────────────────────────────────────
let state = {
    groups: [],
    sources: {},
    category: "",
    multiOnly: true,
    sourceFilter: "",
    currentView: "noticias",
    metricsData: null,
};

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    setHeaderDate();
    setupViewNav();
    setupFilters();
    setupModal();
    await loadData();
});

function setHeaderDate() {
    const d = new Date();
    const opts = { weekday: "long", year: "numeric", month: "long", day: "numeric" };
    const dateStr = d.toLocaleDateString("es-AR", opts);
    $("#header-date").textContent = dateStr.charAt(0).toUpperCase() + dateStr.slice(1);
}

// ── Data loading ──────────────────────────────────────────────────────────
async function loadData() {
    showLoading(true);
    try {
        const [groupsRes, statusRes, sourcesRes] = await Promise.all([
            fetch(`${API}/api/grupos?limit=200`).then(r => r.json()),
            fetch(`${API}/api/status`).then(r => r.json()),
            fetch(`${API}/api/fuentes`).then(r => r.json()),
        ]);

        state.groups = groupsRes.groups || [];
        state.sources = sourcesRes || {};

        updateStats(statusRes);
        populateSourceFilter(sourcesRes);
        populateFooterSources(sourcesRes);
        renderGroups();
    } catch (err) {
        console.error("Error loading data:", err);
        $("#news-grid").innerHTML = `
            <div class="empty-state">
                <h3>Error al cargar noticias</h3>
                <p>No se pudo conectar con el servidor. Asegurate de que esté corriendo.</p>
                <p style="margin-top:.5rem;font-size:.8rem;color:var(--text-dim)">${err.message}</p>
            </div>`;
    }
    showLoading(false);
}

function showLoading(show) {
    $("#loading").style.display = show ? "flex" : "none";
}

function updateStats(status) {
    $("#stat-articles").textContent = `${status.total_articles} noticias recolectadas`;
    $("#stat-compared").textContent = `${status.multi_source_groups} noticias en 2+ medios`;
    if (status.oldest_article && status.newest_article) {
        const fmt = d => new Date(d).toLocaleDateString("es-AR", { day: "numeric", month: "short" });
        const desde = fmt(status.oldest_article);
        const hasta = fmt(status.newest_article);
        $("#stat-dates").textContent = desde === hasta ? `Noticias del ${desde}` : `Noticias del ${desde} al ${hasta}`;
    }
    if (status.last_update) {
        const d = new Date(status.last_update);
        $("#stat-updated").textContent = `Actualizado: ${d.toLocaleTimeString("es-AR")}`;
    }
}

function populateSourceFilter(sources) {
    const sel = $("#source-filter");
    Object.keys(sources).sort().forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        sel.appendChild(opt);
    });
}

function populateFooterSources(sources) {
    $("#footer-sources").textContent = "Fuentes: " + Object.keys(sources).join(" · ");
}

// ── View navigation ───────────────────────────────────────────────────────
function setupViewNav() {
    $$(".view-tab").forEach(tab => {
        tab.addEventListener("click", () => {
            const view = tab.dataset.view;
            if (view === state.currentView) return;
            state.currentView = view;
            $$(".view-tab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            switchView(view);
        });
    });
}

let _metricsFiltersReady = false;

function switchView(view) {
    const noticias = $("#view-noticias");
    const metricas = $("#view-metricas");
    const filtersBar = $("#filters-bar");

    if (view === "metricas") {
        noticias.hidden = true;
        filtersBar.style.display = "none";
        metricas.hidden = false;
        if (!_metricsFiltersReady) {
            setupMetricsFilters();
            _metricsFiltersReady = true;
        }
        if (!state.metricsData) {
            const { desde, hasta } = computeDateRange("hoy");
            loadMetrics(desde, hasta);
        }
    } else {
        metricas.hidden = true;
        filtersBar.style.display = "";
        noticias.hidden = false;
    }
}

// ── Metrics ───────────────────────────────────────────────────────────────

function setupMetricsFilters() {
    $$(".date-quick-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            $$(".date-quick-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const range = btn.dataset.range;
            const { desde, hasta } = computeDateRange(range);
            $("#metric-date-desde").value = desde || "";
            $("#metric-date-hasta").value = hasta || "";
            loadMetrics(desde, hasta);
        });
    });

    $("#btn-apply-dates").addEventListener("click", () => {
        $$(".date-quick-btn").forEach(b => b.classList.remove("active"));
        const desde = $("#metric-date-desde").value || null;
        const hasta = $("#metric-date-hasta").value || null;
        loadMetrics(desde, hasta);
    });
}

function computeDateRange(range) {
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
        case "todo":
        default:
            return { desde: null, hasta: null };
    }
}

async function loadMetrics(desde = null, hasta = null) {
    ["metric-first-publisher", "metric-reaction-time", "metric-exclusivity"].forEach(id => {
        $(`#${id}`).innerHTML = `<div class="loading-state" style="padding:1.5rem"><div class="spinner"></div></div>`;
    });

    const params = new URLSearchParams();
    if (desde) params.set("desde", desde);
    if (hasta) params.set("hasta", hasta);
    const qs = params.toString() ? `?${params}` : "";

    try {
        const data = await fetch(`${API}/api/metricas${qs}`).then(r => r.json());
        state.metricsData = data;
        renderMetricsSummary(data);
        renderDateRangeInfo(data.date_range);
        renderFirstPublisher(data.first_publisher_ranking);
        renderReactionTime(data.avg_reaction_time);
        renderExclusivity(data.exclusivity_index);
    } catch (err) {
        console.error("Error loading metrics:", err);
        $(`#metric-first-publisher`).innerHTML = `<p style="color:var(--text-dim)">Error al cargar métricas</p>`;
    }
}

function renderDateRangeInfo(range) {
    const el = $("#date-range-info");
    if (!range || !range.min) {
        el.textContent = "";
        return;
    }
    el.textContent = `Datos disponibles desde ${range.min} hasta ${range.max}`;
}

function renderMetricsSummary(data) {
    $("#metrics-summary").innerHTML = `
        <div class="metrics-summary-item"><strong>${data.multi_source_groups}</strong> noticias multi-fuente analizadas</div>
        <div class="metrics-summary-item"><strong>${data.total_groups}</strong> grupos totales</div>
        <div class="metrics-summary-item"><strong>${data.first_publisher_ranking.length}</strong> medios en competencia</div>
    `;
}

function getSourceColor(sourceName) {
    return state.sources[sourceName]?.color || "#888";
}

function renderFirstPublisher(ranking) {
    const container = $("#metric-first-publisher");
    if (!ranking.length) {
        container.innerHTML = `<p style="color:var(--text-dim)">No hay datos suficientes aún</p>`;
        return;
    }

    const medals = ["🥇", "🥈", "🥉"];
    const podiumOrder = [1, 0, 2];
    const top3 = ranking.slice(0, 3);
    const rest = ranking.slice(3);
    const maxCount = ranking[0].count;

    let podioHtml = `<div class="podio-container">`;
    podiumOrder.forEach(idx => {
        if (idx >= top3.length) return;
        const item = top3[idx];
        podioHtml += `
        <div class="podio-item">
            <div class="podio-source">${escHtml(item.source)}</div>
            <div class="podio-pedestal podio-pedestal-${idx + 1}">
                <span class="podio-medal">${medals[idx]}</span>
            </div>
            <div class="podio-count">${item.count} veces primero</div>
        </div>`;
    });
    podioHtml += `</div>`;

    let restHtml = "";
    if (rest.length) {
        restHtml = `<div class="ranking-rest">`;
        rest.forEach((item, i) => {
            const pct = Math.max(8, (item.count / maxCount) * 100);
            const color = getSourceColor(item.source);
            restHtml += `
            <div class="ranking-row">
                <span class="ranking-pos">${i + 4}°</span>
                <div class="ranking-bar-track">
                    <div class="ranking-bar-fill" style="width:${pct}%;background:${escHtml(color)}">
                        <span class="ranking-bar-label">${escHtml(item.source)}</span>
                    </div>
                </div>
                <span class="ranking-count">${item.count}</span>
            </div>`;
        });
        restHtml += `</div>`;
    }

    container.innerHTML = podioHtml + restHtml;
}

function renderReactionTime(reactions) {
    const container = $("#metric-reaction-time");
    if (!reactions.length) {
        container.innerHTML = `<p style="color:var(--text-dim)">No hay datos suficientes aún</p>`;
        return;
    }

    const maxMin = Math.max(...reactions.map(r => r.avg_minutes));

    let html = `<div class="reaction-list">`;
    reactions.forEach(r => {
        const pct = Math.max(12, (r.avg_minutes / maxMin) * 100);
        const color = getSourceColor(r.source);
        const label = r.avg_minutes < 60
            ? `${Math.round(r.avg_minutes)} min`
            : `${(r.avg_minutes / 60).toFixed(1)}h`;

        html += `
        <div class="reaction-row">
            <div class="reaction-source">
                <span class="reaction-source-dot" style="background:${escHtml(color)}"></span>
                ${escHtml(r.source)}
            </div>
            <div class="reaction-bar-track">
                <div class="reaction-bar-fill" style="width:${pct}%;background:${escHtml(color)}">
                    <span class="reaction-value">${label}</span>
                </div>
            </div>
            <span class="reaction-note">${r.sample_size} noticias</span>
        </div>`;
    });
    html += `</div>`;
    container.innerHTML = html;
}

function renderExclusivity(exclusivity) {
    const container = $("#metric-exclusivity");
    if (!exclusivity.length) {
        container.innerHTML = `<p style="color:var(--text-dim)">No hay datos suficientes aún</p>`;
        return;
    }

    let html = `<div class="exclusivity-list">`;
    exclusivity.forEach(e => {
        const pct = Math.max(5, e.percentage);
        const color = getSourceColor(e.source);

        html += `
        <div class="exclusivity-row">
            <div class="exclusivity-source">
                <span class="exclusivity-source-dot" style="background:${escHtml(color)}"></span>
                ${escHtml(e.source)}
            </div>
            <div class="exclusivity-bar-track">
                <div class="exclusivity-bar-fill" style="width:${pct}%;background:${escHtml(color)}">
                    <span class="exclusivity-pct">${e.percentage}%</span>
                </div>
            </div>
            <span class="exclusivity-detail">${e.exclusive}/${e.total}</span>
        </div>`;
    });
    html += `</div>`;

    html += `
    <div class="exclusivity-legend">
        <strong>Alta exclusividad</strong> = agenda propia, temas que otros no cubren.<br>
        <strong>Baja exclusividad</strong> = cubre mayormente lo que cubren todos los medios.
    </div>`;

    container.innerHTML = html;
}

// ── Filters ───────────────────────────────────────────────────────────────
function setupFilters() {
    $$("#category-filters .filter-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            $$("#category-filters .filter-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.category = btn.dataset.category;
            renderGroups();
        });
    });

    $("#toggle-multi").addEventListener("change", (e) => {
        state.multiOnly = e.target.checked;
        renderGroups();
    });

    $("#source-filter").addEventListener("change", (e) => {
        state.sourceFilter = e.target.value;
        renderGroups();
    });

    $("#btn-refresh").addEventListener("click", async () => {
        const btn = $("#btn-refresh");
        btn.classList.add("spinning");
        btn.disabled = true;
        try {
            await fetch(`${API}/api/refresh`, { method: "POST" });
            state.metricsData = null;
            await loadData();
            if (state.currentView === "metricas") {
                const desde = $("#metric-date-desde").value || null;
                const hasta = $("#metric-date-hasta").value || null;
                loadMetrics(desde, hasta);
            }
        } finally {
            btn.classList.remove("spinning");
            btn.disabled = false;
        }
    });
}

// ── Render Groups ─────────────────────────────────────────────────────────
function renderGroups() {
    const grid = $("#news-grid");
    let groups = [...state.groups];

    if (state.category) {
        groups = groups.filter(g => g.category === state.category);
    }
    if (state.multiOnly) {
        groups = groups.filter(g => g.source_count >= 2);
    }
    if (state.sourceFilter) {
        groups = groups.filter(g =>
            g.articles.some(a => a.source === state.sourceFilter)
        );
    }

    if (!groups.length) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1/-1">
                <h3>No se encontraron noticias</h3>
                <p>Probá cambiando los filtros o esperá la próxima actualización.</p>
            </div>`;
        return;
    }

    grid.innerHTML = groups.map(g => renderCard(g)).join("");

    grid.querySelectorAll(".news-card").forEach((card, i) => {
        card.addEventListener("click", () => openComparison(groups[i]));
    });
}

function renderCard(group) {
    const img = group.representative_image
        ? `<img class="card-image" src="${escHtml(group.representative_image)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\\'card-image-placeholder\\'>&#9783;</div>'">`
        : `<div class="card-image-placeholder">&#9783;</div>`;

    const badges = group.articles.map(a =>
        `<span class="source-badge" style="background:${escHtml(a.source_color)}">${escHtml(a.source)}</span>`
    ).join("");

    const summary = group.articles
        .map(a => a.summary)
        .filter(Boolean)
        .sort((a, b) => b.length - a.length)[0] || "";
    const shortSummary = summary.length > 200
        ? summary.slice(0, 199).replace(/\s+\S*$/, "") + "…"
        : summary;

    const timeStr = group.published
        ? timeAgo(new Date(group.published))
        : "";

    const dateStr = group.published
        ? formatDate(new Date(group.published))
        : "";

    const isMulti = group.source_count >= 2;
    const compareHint = isMulti
        ? `<span class="card-compare-hint">
             <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5"/><path d="M8 3H3v5"/><path d="M21 3l-7 7"/><path d="M3 3l7 7"/><path d="M16 21h5v-5"/><path d="M8 21H3v-5"/><path d="M21 21l-7-7"/><path d="M3 21l7-7"/></svg>
             Comparar ${group.source_count} fuentes
           </span>`
        : "";

    const cardClass = isMulti ? "news-card" : "news-card card-single-source";

    return `
    <article class="${cardClass}" data-group-id="${escHtml(group.group_id)}">
        ${img}
        <div class="card-body">
            <div class="card-meta">
                <span class="card-category">${escHtml(group.category)}</span>
                ${dateStr ? `<span class="card-date">${dateStr}</span>` : ""}
            </div>
            <h3 class="card-title">${escHtml(group.representative_title)}</h3>
            <p class="card-summary">${escHtml(shortSummary)}</p>
        </div>
        <div class="card-footer">
            <div class="source-badges">${badges}</div>
            ${compareHint}
            ${timeStr ? `<span class="card-time">${timeStr}</span>` : ""}
        </div>
    </article>`;
}

// ── Comparison Modal ──────────────────────────────────────────────────────
function setupModal() {
    const overlay = $("#compare-modal");
    $("#modal-close").addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeModal();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeModal();
    });
    setupModalSwipe();
}

function setupModalSwipe() {
    const content = $(".modal-content");
    let startY = 0;
    let currentY = 0;
    let isDragging = false;

    const isMobile = () => window.innerWidth <= 480;

    content.addEventListener("touchstart", (e) => {
        if (!isMobile()) return;
        if (content.scrollTop > 0) return;
        const touch = e.touches[0];
        startY = touch.clientY;
        isDragging = true;
        content.style.transition = "none";
    }, { passive: true });

    content.addEventListener("touchmove", (e) => {
        if (!isDragging || !isMobile()) return;
        const touch = e.touches[0];
        currentY = touch.clientY - startY;
        if (currentY < 0) { currentY = 0; return; }
        content.style.transform = `translateY(${currentY}px)`;
        content.style.opacity = Math.max(0.5, 1 - currentY / 400);
    }, { passive: true });

    content.addEventListener("touchend", () => {
        if (!isDragging || !isMobile()) return;
        isDragging = false;
        content.style.transition = "transform 0.3s ease, opacity 0.3s ease";
        if (currentY > 120) {
            content.style.transform = `translateY(100%)`;
            content.style.opacity = "0";
            setTimeout(() => {
                closeModal();
                content.style.transform = "";
                content.style.opacity = "";
            }, 300);
        } else {
            content.style.transform = "";
            content.style.opacity = "";
        }
        currentY = 0;
    });
}

async function openComparison(group) {
    const body = $("#compare-body");

    body.innerHTML = `<div class="loading-state" style="padding:3rem"><div class="spinner"></div><p>Analizando cobertura…</p></div>`;
    $("#compare-modal").hidden = false;
    document.body.style.overflow = "hidden";

    let data;
    try {
        const resp = await fetch(`${API}/api/comparar/${group.group_id}`);
        data = await resp.json();
    } catch {
        data = null;
    }

    if (!data || data.error) {
        body.innerHTML = `<div class="empty-state"><h3>No se pudo cargar la comparación</h3></div>`;
        return;
    }

    const sources = data.sources || [];
    const headlines = data.headline_analysis || {};

    // ── Section 1: Header ──
    const headerHtml = `
    <div class="compare-header">
        <h2>${escHtml(data.representative_title)}</h2>
        <p class="compare-subtitle">${data.source_count} fuente${data.source_count > 1 ? "s" : ""} cubriendo esta noticia</p>
    </div>`;

    // ── Section 2: Headline framing ──
    let framingHtml = "";
    if (headlines.different_framing && headlines.details?.length > 1) {
        const framingCards = headlines.details.map(d => {
            const srcData = sources.find(s => s.source === d.source) || {};
            const toneLabel = { alarmista: "Tono alarmista", positivo: "Tono positivo", informativo: "Tono informativo", neutral: "Tono neutral" }[d.tone] || d.tone;
            const focusLabel = { político: "Enfoque político", económico: "Enfoque económico", policial: "Enfoque policial", deportivo: "Enfoque deportivo", general: "Enfoque general" }[d.focus] || d.focus;
            const toneClass = { alarmista: "tone-alarm", positivo: "tone-positive", informativo: "tone-info", neutral: "tone-neutral" }[d.tone] || "tone-neutral";
            return `
            <div class="framing-card">
                <div class="framing-source">
                    <span class="compare-source-dot" style="background:${escHtml(srcData.source_color || '#888')}"></span>
                    ${escHtml(d.source)}
                </div>
                <div class="framing-title">"${escHtml(d.title)}"</div>
                <div class="framing-tags">
                    <span class="framing-tag ${toneClass}">${toneLabel}</span>
                    <span class="framing-tag">${focusLabel}</span>
                </div>
            </div>`;
        }).join("");

        framingHtml = `
        <div class="compare-section">
            <h3 class="section-title">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
                Cómo lo titula cada medio
            </h3>
            <div class="framing-grid">${framingCards}</div>
        </div>`;
    }

    // ── Section 3: Full coverage side by side ──
    const columnsHtml = sources.map(s => {
        const pubDate = s.published
            ? new Date(s.published).toLocaleString("es-AR", {
                day: "numeric", month: "short", year: "numeric",
                hour: "2-digit", minute: "2-digit"
              })
            : "";
        return `
        <div class="compare-column">
            <div class="compare-source-name">
                <span class="compare-source-dot" style="background:${escHtml(s.source_color)}"></span>
                ${escHtml(s.source)}
            </div>
            <div class="compare-title">${escHtml(s.title)}</div>
            <div class="compare-summary">${escHtml(s.summary || "Sin resumen disponible desde el feed RSS.")}</div>
            ${pubDate ? `<div style="font-size:.75rem;color:var(--text-dim);margin-bottom:.75rem">${pubDate}</div>` : ""}
            <a href="${escHtml(s.link)}" target="_blank" rel="noopener" class="compare-link">
                Leer nota completa
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            </a>
        </div>`;
    }).join("");

    const coverageHtml = `
    <div class="compare-section">
        <h3 class="section-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
            Cobertura completa
        </h3>
        <div class="compare-grid">${columnsHtml}</div>
    </div>`;

    const dragHandle = window.innerWidth <= 480
        ? `<div class="modal-drag-handle"></div>`
        : "";
    body.innerHTML = dragHandle + headerHtml + framingHtml + coverageHtml;
}

function closeModal() {
    $("#compare-modal").hidden = true;
    document.body.style.overflow = "";
}

// ── Helpers ───────────────────────────────────────────────────────────────
function escHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function timeAgo(date) {
    const now = new Date();
    const diffMs = now - date;
    const mins = Math.floor(diffMs / 60000);
    const hours = Math.floor(diffMs / 3600000);
    const days = Math.floor(diffMs / 86400000);

    if (mins < 1) return "Ahora";
    if (mins < 60) return `Hace ${mins} min`;
    if (hours < 24) return `Hace ${hours}h`;
    if (days < 7) return `Hace ${days}d`;
    return date.toLocaleDateString("es-AR", { day: "numeric", month: "short" });
}

function formatDate(date) {
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    const isYesterday = date.toDateString() === yesterday.toDateString();

    const time = date.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });

    if (isToday) return `Hoy ${time}`;
    if (isYesterday) return `Ayer ${time}`;
    return date.toLocaleDateString("es-AR", { day: "numeric", month: "short" }) + ` ${time}`;
}
