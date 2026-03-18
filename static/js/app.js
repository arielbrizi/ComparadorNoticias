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
};

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    setHeaderDate();
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
    $("#stat-articles").textContent = `${status.total_articles} artículos`;
    $("#stat-groups").textContent = `${status.total_groups} noticias agrupadas`;
    $("#stat-compared").textContent = `${status.multi_source_groups} con múltiples fuentes`;
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
            await loadData();
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
            <span class="card-category">${escHtml(group.category)}</span>
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
