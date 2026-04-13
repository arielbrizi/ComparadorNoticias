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
    searchQuery: "",
    dateFilter: "hoy",
    currentView: "noticias",
    metricsData: null,
    user: null,
    aiSearch: {
        loading: false,
        loadingHistory: false,
        available: null,
        summary: "",
        relevantIds: [],
        active: false,
        hasResults: true,
    },
};

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    setHeaderDate();
    setupViewNav();
    setupFilters();
    setupModal();
    setupHeroSearch();
    setupAuth();
    initAuth();
    track("page_view", { view: "noticias", initial: true });
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
        const dateParams = computeNewsDateRange(state.dateFilter);
        const gruposUrl = new URL(`${API}/api/grupos`, location.href);
        gruposUrl.searchParams.set("limit", "200");
        if (dateParams.desde) gruposUrl.searchParams.set("desde", dateParams.desde);
        if (dateParams.hasta) gruposUrl.searchParams.set("hasta", dateParams.hasta);

        const statusUrl = new URL(`${API}/api/status`, location.href);
        if (dateParams.desde) statusUrl.searchParams.set("desde", dateParams.desde);
        if (dateParams.hasta) statusUrl.searchParams.set("hasta", dateParams.hasta);

        const [groupsRes, statusRes, sourcesRes] = await Promise.all([
            fetch(gruposUrl).then(r => r.json()),
            fetch(statusUrl).then(r => r.json()),
            fetch(`${API}/api/fuentes`).then(r => r.json()),
        ]);

        state.groups = groupsRes.groups || [];
        state.sources = sourcesRes || {};
        state.earlyFallback = !!dateParams.earlyFallback;

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

const EARLY_HOUR_THRESHOLD = 8;

function isEarlyMorning() {
    return new Date().getHours() < EARLY_HOUR_THRESHOLD;
}

function computeNewsDateRange(range) {
    const now = new Date();
    const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const today = fmt(now);

    switch (range) {
        case "hoy": {
            if (isEarlyMorning()) {
                const ayer = new Date(now);
                ayer.setDate(ayer.getDate() - 1);
                return { desde: fmt(ayer), hasta: today, earlyFallback: true };
            }
            return { desde: today, hasta: today, earlyFallback: false };
        }
        case "ayer": {
            const d = new Date(now);
            d.setDate(d.getDate() - 1);
            const ayer = fmt(d);
            return { desde: ayer, hasta: ayer };
        }
        case "3d": {
            const d = new Date(now);
            d.setDate(d.getDate() - 2);
            return { desde: fmt(d), hasta: today };
        }
        case "7d": {
            const d = new Date(now);
            d.setDate(d.getDate() - 6);
            return { desde: fmt(d), hasta: today };
        }
        case "todas":
        default:
            return { desde: null, hasta: null };
    }
}

function showLoading(show) {
    $("#loading").style.display = show ? "flex" : "none";
}

function updateStats(status) {
    let suffix = "";
    if (state.dateFilter === "hoy") {
        suffix = state.earlyFallback ? " (últimas horas)" : " hoy";
    }
    $("#stat-articles").textContent = `${status.total_articles} noticias recolectadas${suffix}`;
    $("#stat-compared").textContent = `${status.multi_source_groups} noticias en 2+ medios${suffix}`;
    if (status.last_update) {
        const d = new Date(status.last_update);
        $("#stat-updated").textContent = `Actualizado: ${d.toLocaleTimeString("es-AR")}`;
    }
}

function updateDateRangeStat() {}

let _sourceFilterReady = false;
function populateSourceFilter(sources) {
    if (_sourceFilterReady) return;
    _sourceFilterReady = true;
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
    ["btn-back-home", "btn-back-home-weekly", "btn-back-home-topstory", "btn-back-home-nube"].forEach(id => {
        const btn = $(`#${id}`);
        if (btn) btn.addEventListener("click", () => history.back());
    });
    const btnTemas = $("#btn-back-temas");
    if (btnTemas) btnTemas.addEventListener("click", () => {
        if (_temasSubview === "detail") {
            _temasSubview = "topics";
            loadTemasView();
        } else {
            history.back();
        }
    });

    window.addEventListener("popstate", (e) => {
        if (!e.state || !e.state.modal) {
            _closeModalVisual();
        }
        const view = (e.state && e.state.view) || "noticias";
        _switchViewInternal(view);
    });

    history.replaceState({ view: "noticias" }, "");

    const brandHome = $("#brand-home");
    if (brandHome) brandHome.addEventListener("click", (e) => {
        e.preventDefault();
        switchView("noticias");
    });
}

let _metricsFiltersReady = false;

function _switchViewInternal(view) {
    const noticias = $("#view-noticias");
    const metricas = $("#view-metricas");
    const semana = $("#view-semana");
    const importante = $("#view-importante");
    const temas = $("#view-temas");
    const nube = $("#view-nube");

    if (view !== state.currentView) track("page_view", { view });
    state.currentView = view;

    noticias.hidden = true;
    metricas.hidden = true;
    if (semana) semana.hidden = true;
    if (importante) importante.hidden = true;
    if (temas) temas.hidden = true;
    if (nube) nube.hidden = true;

    if (view === "metricas") {
        if (!state.user) {
            localStorage.setItem("pending_view", "metricas");
            openLoginModal();
            return;
        }
        metricas.hidden = false;
        if (!_metricsFiltersReady) {
            setupMetricsFilters();
            _metricsFiltersReady = true;
        }
        if (!state.metricsData) {
            const { desde, hasta } = computeDateRange("hoy");
            loadMetrics(desde, hasta);
        }
        window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (view === "semana") {
        if (semana) semana.hidden = false;
        loadWeeklySummary();
        window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (view === "importante") {
        if (importante) importante.hidden = false;
        loadTopStory();
        window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (view === "temas") {
        if (temas) temas.hidden = false;
        _temasSubview = "topics";
        loadTemasView();
        window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (view === "nube") {
        if (nube) nube.hidden = false;
        loadWordCloud();
        window.scrollTo({ top: 0, behavior: "smooth" });
    } else {
        noticias.hidden = false;
    }
}

function switchView(view) {
    if (state.currentView !== view) {
        history.pushState({ view }, "", `#${view}`);
    }
    _switchViewInternal(view);
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
        const resp = await fetch(`${API}/api/metricas${qs}`);
        if (resp.status === 401) {
            switchView("noticias");
            openLoginModal();
            return;
        }
        const data = await resp.json();
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
        <div class="metrics-summary-item"><strong>${data.total_groups}</strong> noticias detectadas</div>
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
            track("filter_change", { filter: "category", value: state.category });
            renderGroups();
        });
    });

    $("#toggle-multi").addEventListener("change", (e) => {
        state.multiOnly = e.target.checked;
        track("filter_change", { filter: "multi_only", value: state.multiOnly });
        renderGroups();
    });

    $("#source-filter").addEventListener("change", (e) => {
        state.sourceFilter = e.target.value;
        track("filter_change", { filter: "source", value: state.sourceFilter });
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

// ── Hero search & action cards ────────────────────────────────────────────
let _aiSearchController = null;
let _topicsCache = null;
let _topicsLoading = false;
let _topicsCacheTs = 0;
let _topicsLoadingSince = 0;
let _topicsSearchCached = new Set();
const _topicsTTL = 55 * 60 * 1000; // 55 min (slightly less than server's 1h TTL)
const _topicsLoadingTimeout = 20_000;

function _isTopicsCacheValid() {
    return _topicsCache && (Date.now() - _topicsCacheTs) < _topicsTTL;
}

function _resetStaleLoading() {
    if (_topicsLoading && _topicsLoadingSince &&
        (Date.now() - _topicsLoadingSince) > _topicsLoadingTimeout) {
        _topicsLoading = false;
        _topicsLoadingSince = 0;
    }
}

const _isTouchDevice = matchMedia("(pointer: coarse)").matches;

function setupHeroSearch() {
    const input = $("#hero-search-input");
    const clearBtn = $("#hero-search-clear");
    const suggestions = $("#search-suggestions");
    let localTimer;
    let aiTimer;

    input.addEventListener("input", () => {
        clearBtn.hidden = !input.value;
        clearTimeout(localTimer);
        clearTimeout(aiTimer);
        hideSuggestions();

        localTimer = setTimeout(() => {
            state.searchQuery = input.value.trim().toLowerCase();
            if (!state.aiSearch.active) renderGroups();
        }, 200);

        const raw = input.value.trim();
        if (raw.length >= 3) {
            aiTimer = setTimeout(() => performAISearch(raw), 600);
        } else {
            clearAISearch();
            renderGroups();
        }
    });

    input.addEventListener("focus", () => {
        if (!input.value.trim() && !_isTouchDevice) showSuggestions();
    });

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            hideSuggestions();
            clearTimeout(localTimer);
            clearTimeout(aiTimer);
            const raw = input.value.trim();
            state.searchQuery = raw.toLowerCase();
            if (raw.length >= 3) {
                performAISearch(raw);
            } else {
                clearAISearch();
                renderGroups();
            }
        }
        if (e.key === "Escape") hideSuggestions();
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest(".hero-input-wrap")) hideSuggestions();
    });

    clearBtn.addEventListener("click", () => {
        input.value = "";
        clearBtn.hidden = true;
        state.searchQuery = "";
        clearAISearch();
        renderGroups();
        input.focus();
    });

    $$(".hero-action-card").forEach(card => {
        card.addEventListener("click", () => {
            const action = card.dataset.action;
            if (action === "metricas") {
                switchView("metricas");
            } else if (action === "semana") {
                switchView("semana");
            } else if (action === "importante") {
                switchView("importante");
            } else if (action === "temas") {
                switchView("temas");
            } else if (action === "nube") {
                switchView("nube");
            } else if (action === "admin") {
                window.location.href = "/admin";
            } else {
                showToast(actionLabels[action] || "Próximamente");
            }
        });
    });

    prefetchTopics();
    if (_isTouchDevice) renderTopicChips();

    setInterval(() => {
        if (!_isTopicsCacheValid() && !_topicsLoading) {
            _topicsCache = null;
            _topicsCacheTs = 0;
            _topicsSearchCached = new Set();
            prefetchTopics();
        }
    }, 10 * 60 * 1000); // check every 10 min

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "visible") return;
        _resetStaleLoading();
        if (!_isTopicsCacheValid() && !_topicsLoading) {
            prefetchTopics();
        }
    });

    window.addEventListener("pageshow", (e) => {
        if (!e.persisted) return; // only bfcache restorations
        _topicsLoading = false;
        _topicsLoadingSince = 0;
        _topicsCache = null;
        _topicsCacheTs = 0;
        _topicsSearchCached = new Set();
        prefetchTopics();
    });
}

async function prefetchTopics() {
    _resetStaleLoading();
    if (_topicsLoading) return;
    if (_isTopicsCacheValid()) return;
    _topicsLoading = true;
    _topicsLoadingSince = Date.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const resp = await fetch(`${API}/api/topics`, { signal: controller.signal });
        const data = await resp.json();
        if (data.ai_available && data.topics?.length) {
            _topicsCache = data.topics;
            _topicsCacheTs = Date.now();
            _topicsSearchCached = new Set(data.search_cached || []);
            if (_topicsSearchCached.size < data.topics.length) {
                _scheduleCachedRecheck();
            }
        }
    } catch (err) {
        console.error("Failed to prefetch topics:", err);
    } finally {
        clearTimeout(timer);
        _topicsLoading = false;
        _topicsLoadingSince = 0;
        if (_isTouchDevice) {
            renderTopicChips();
        } else {
            const input = $("#hero-search-input");
            if (input && document.activeElement === input && !input.value.trim()) {
                showSuggestions();
            }
        }
    }
}

let _cachedRecheckTimer = null;
function _scheduleCachedRecheck() {
    if (_cachedRecheckTimer) return;
    _cachedRecheckTimer = setTimeout(async () => {
        _cachedRecheckTimer = null;
        if (!_isTopicsCacheValid()) return;
        try {
            const resp = await fetch(`${API}/api/topics`);
            const data = await resp.json();
            if (data.search_cached?.length) {
                _topicsSearchCached = new Set(data.search_cached);
                if (_isTouchDevice) renderTopicChips();
                else {
                    const input = $("#hero-search-input");
                    if (input && document.activeElement === input && !input.value.trim()) showSuggestions();
                }
                if (_topicsSearchCached.size < (_topicsCache?.length || 6)) {
                    _scheduleCachedRecheck();
                }
            }
        } catch { /* silent */ }
    }, 15_000);
}

function showSuggestions() {
    const box = $("#search-suggestions");
    if (!box) return;
    _resetStaleLoading();

    if (_isTopicsCacheValid() && _topicsCache?.length) {
        box.innerHTML =
            `<div class="suggestions-header">Temas del día</div>` +
            _topicsCache.map(t => {
                const cached = _topicsSearchCached.has(t.label);
                return `<button class="suggestion-item" data-query="${escHtml(t.label)}">`
                    + `<span class="suggestion-emoji">${t.emoji}</span>`
                    + `<span class="suggestion-label">${escHtml(t.label)}</span>`
                    + (cached ? `<span class="suggestion-cached" title="Búsqueda lista">✓</span>` : "")
                    + `</button>`;
            }).join("");
        box.hidden = false;

        box.querySelectorAll(".suggestion-item").forEach(btn => {
            btn.addEventListener("mousedown", (e) => {
                e.preventDefault();
                const query = btn.dataset.query;
                const input = $("#hero-search-input");
                input.value = query;
                $("#hero-search-clear").hidden = false;
                hideSuggestions();
                performAISearch(query);
            });
        });
    } else if (_topicsLoading) {
        box.innerHTML = `<div class="suggestions-header"><div class="ai-pulse-dot"></div> Cargando temas del día…</div>`;
        box.hidden = false;
    } else {
        box.innerHTML = `<div class="suggestions-header"><div class="ai-pulse-dot"></div> Cargando temas del día…</div>`;
        box.hidden = false;
        prefetchTopics();
    }
}

function hideSuggestions() {
    const box = $("#search-suggestions");
    if (box) box.hidden = true;
}

function renderTopicChips() {
    const container = $("#topics-chips");
    if (!container) return;
    _resetStaleLoading();

    if (_isTopicsCacheValid() && _topicsCache?.length) {
        container.innerHTML = _topicsCache.map(t => {
            const cached = _topicsSearchCached.has(t.label);
            return `<button class="topic-chip" data-query="${escHtml(t.label)}">`
                + `<span class="topic-chip-emoji">${t.emoji}</span>`
                + `<span>${escHtml(t.label)}</span>`
                + (cached ? `<span class="topic-chip-cached" title="Búsqueda lista">✓</span>` : "")
                + `</button>`;
        }).join("");
        container.hidden = false;

        container.querySelectorAll(".topic-chip").forEach(btn => {
            btn.addEventListener("click", () => {
                const query = btn.dataset.query;
                const input = $("#hero-search-input");
                input.value = query;
                $("#hero-search-clear").hidden = false;
                performAISearch(query);
            });
        });
    } else if (_topicsLoading) {
        container.innerHTML = `<span class="topic-chip-loading"><div class="ai-pulse-dot"></div> Cargando temas…</span>`;
        container.hidden = false;
    } else {
        container.innerHTML = `<span class="topic-chip-loading"><div class="ai-pulse-dot"></div> Cargando temas…</span>`;
        container.hidden = false;
        prefetchTopics();
    }
}

async function performAISearch(query) {
    track("ai_search", { query });
    if (_aiSearchController) _aiSearchController.abort();
    _aiSearchController = new AbortController();
    const signal = _aiSearchController.signal;

    state.searchQuery = query.toLowerCase();
    state.aiSearch.loading = true;
    state.aiSearch.loadingHistory = false;
    state.aiSearch.active = false;
    renderAIStatus();
    renderAISummary();
    renderGroups();

    const qEnc = encodeURIComponent(query);
    const searchUrl = `${API}/api/search?q=${qEnc}`;

    try {
        const resp = await fetch(searchUrl, { signal });
        const data = await resp.json();

        if (signal.aborted) return;

        if (data.ai_available) {
            state.aiSearch.available = true;
            state.aiSearch.summary = data.summary || "";
            state.aiSearch.relevantIds = data.relevant_group_ids || [];
            state.aiSearch.active = true;
            state.aiSearch.hasResults = data.has_results !== false;
            state.aiSearch.provider = data.ai_provider || "";

            if (data.matched_groups?.length) {
                _mergeMatchedGroups(data.matched_groups);
            }
        } else {
            state.aiSearch.available = false;
            state.aiSearch.active = false;
        }
    } catch (err) {
        if (err.name === "AbortError") {
            state.aiSearch.loading = false;
            renderAIStatus(); renderAISummary(); renderGroups();
            return;
        }
        console.error("AI search failed:", err);
        state.aiSearch.available = false;
        state.aiSearch.active = false;
    }

    if (signal.aborted) return;

    state.aiSearch.loading = false;
    _aiSearchController = null;
    renderAIStatus();
    renderAISummary();
    renderGroups();
}

function _mergeMatchedGroups(matchedGroups) {
    const existingIds = new Set(state.groups.map(g => g.group_id));
    for (const g of matchedGroups) {
        if (!existingIds.has(g.group_id)) {
            state.groups.push(g);
            existingIds.add(g.group_id);
        }
    }
}

function clearAISearch() {
    if (_aiSearchController) { _aiSearchController.abort(); _aiSearchController = null; }
    state.aiSearch = { loading: false, loadingHistory: false, available: null, summary: "", relevantIds: [], active: false, hasResults: true, provider: "" };
    renderAIStatus();
    renderAISummary();
}

function renderAISummary() {
    const container = $("#ai-summary-container");
    if (!container) return;

    if (state.aiSearch.loading) {
        container.innerHTML = `
            <div class="ai-summary-panel ai-summary-loading">
                <div class="ai-summary-header">
                    <div class="ai-pulse-dot"></div>
                    <span class="ai-summary-label">Buscando con IA…</span>
                </div>
            </div>`;
        container.hidden = false;
        return;
    }

    if (state.aiSearch.active && state.aiSearch.summary) {
        const historyHint = state.aiSearch.loadingHistory
            ? `<div class="ai-summary-history"><div class="ai-pulse-dot"></div> Buscando en noticias anteriores…</div>`
            : "";
        const provBadge = state.aiSearch.provider
            ? `<span class="ai-summary-provider">Powered by ${escHtml(state.aiSearch.provider)}</span>`
            : "";
        container.innerHTML = `
            <div class="ai-summary-panel">
                <div class="ai-summary-header">
                    <svg class="ai-sparkle-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z"/>
                        <path d="M19 1l.5 2 2 .5-2 .5-.5 2-.5-2-2-.5 2-.5z" opacity=".6"/>
                    </svg>
                    <span class="ai-summary-label">Resumen IA</span>
                    ${provBadge}
                </div>
                <p class="ai-summary-text">${escHtml(state.aiSearch.summary)}</p>
                ${historyHint}
            </div>`;
        container.hidden = false;
    } else {
        container.innerHTML = "";
        container.hidden = true;
    }
}

function renderAIStatus() {
    const el = $("#ai-status-indicator");
    if (!el) return;

    if (state.aiSearch.loading) {
        el.className = "ai-status ai-status-loading";
        el.title = "IA buscando…";
        el.innerHTML = '<div class="ai-pulse-dot-sm"></div>';
        el.hidden = false;
    } else if (state.aiSearch.available === false) {
        el.className = "ai-status ai-status-off";
        el.title = "IA desconectada";
        el.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 1l22 22"/><path d="M9.5 4a8 8 0 0 1 11 11m-2 2A8 8 0 0 1 7.5 6"/></svg>`;
        el.hidden = false;
    } else if (state.aiSearch.available === true) {
        el.className = "ai-status ai-status-on";
        el.title = "IA conectada";
        el.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z"/></svg>`;
        el.hidden = false;
    } else {
        el.hidden = true;
        el.className = "ai-status";
        el.removeAttribute("title");
        el.innerHTML = "";
    }
}

// ── Word Cloud ────────────────────────────────────────────────────────────

const _wcColors = ["#e63946","#1a73e8","#2d6a4f","#e76f51","#f4a261","#7209b7","#3a86a8"];
let _wcLoaded = false;

async function loadWordCloud() {
    const loading = $("#wordcloud-loading");
    const error = $("#wordcloud-error");
    const container = $("#wordcloud-container");
    const canvas = $("#wordcloud-canvas");

    if (_wcLoaded) return;

    loading.hidden = false;
    error.hidden = true;
    container.style.display = "none";

    try {
        const res = await fetch(`${API}/api/wordcloud`);
        const data = await res.json();
        if (!data.words || data.words.length === 0) {
            error.hidden = false;
            error.textContent = "No hay suficientes noticias para generar la nube de palabras.";
            loading.hidden = true;
            return;
        }

        container.style.display = "";
        loading.hidden = true;

        const rect = container.getBoundingClientRect();
        const w = Math.floor(rect.width) || 800;
        const h = Math.min(Math.floor(w * 0.56), 520);
        canvas.width = w * 2;
        canvas.height = h * 2;
        canvas.style.width = w + "px";
        canvas.style.height = h + "px";

        const maxCount = data.words[0][1];
        const scale = Math.max(1, (w * 2) / 800);

        WordCloud(canvas, {
            list: data.words,
            weightFactor: (size) => Math.max(10, (size / maxCount) * 60 * scale),
            fontFamily: "Inter, system-ui, sans-serif",
            fontWeight: 600,
            color: (_word, _weight, _fontSize, _distance, theta) => {
                return _wcColors[Math.abs(Math.floor(theta * 100)) % _wcColors.length];
            },
            rotateRatio: 0.3,
            rotationSteps: 2,
            backgroundColor: "transparent",
            gridSize: Math.round(8 * scale),
            shrinkToFit: true,
            drawOutOfBound: false,
        });

        _wcLoaded = true;
    } catch (e) {
        loading.hidden = true;
        error.hidden = false;
        error.innerHTML = `Error al cargar la nube de palabras.<br><button class="btn-retry" onclick="_wcLoaded=false;loadWordCloud()">Reintentar</button>`;
    }
}

const actionLabels = {
    reportes: "Reportes Comparativos — Próximamente",
};

function showToast(msg) {
    let toast = $("#toast-notification");
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "toast-notification";
        toast.className = "toast";
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.classList.remove("toast-show");
    void toast.offsetWidth;
    toast.classList.add("toast-show");
    setTimeout(() => toast.classList.remove("toast-show"), 2800);
}

// ── Render Groups ─────────────────────────────────────────────────────────
function renderGroups() {
    const grid = $("#news-grid");
    let groups = [...state.groups];

    if (state.category) {
        groups = groups.filter(g => g.category === state.category);
    }
    const isSearching = state.aiSearch.active || state.searchQuery;
    if (state.multiOnly && !isSearching) {
        groups = groups.filter(g => g.source_count >= 2);
    }
    if (state.sourceFilter && !isSearching) {
        groups = groups.filter(g =>
            g.articles.some(a => a.source === state.sourceFilter)
        );
    }
    if (state.aiSearch.active && state.aiSearch.relevantIds.length > 0) {
        const ids = new Set(state.aiSearch.relevantIds);
        groups = groups.filter(g => ids.has(g.group_id));
    } else if (state.searchQuery) {
        const q = state.searchQuery;
        groups = groups.filter(g =>
            g.representative_title.toLowerCase().includes(q) ||
            g.articles.some(a =>
                a.title.toLowerCase().includes(q) ||
                a.source.toLowerCase().includes(q) ||
                (a.summary && a.summary.toLowerCase().includes(q))
            )
        );
    }

    if (!groups.length) {
        if (state.aiSearch.loading) {
            grid.innerHTML = "";
            return;
        }
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1/-1">
                <h3>No se encontraron noticias</h3>
                <p>Probá cambiando los filtros o esperá la próxima actualización.</p>
            </div>`;
        return;
    }

    updateDateRangeStat(groups);
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
            <div class="card-footer-meta">
                ${compareHint}
                ${timeStr ? `<span class="card-time">${timeStr}</span>` : ""}
            </div>
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
    track("group_click", { group_id: group.group_id, title: group.representative_title, source_count: group.source_count });
    const body = $("#compare-body");

    body.innerHTML = `<div class="loading-state" style="padding:3rem"><div class="spinner"></div><p>Analizando cobertura…</p></div>`;
    $("#compare-modal").hidden = false;
    document.body.style.overflow = "hidden";
    history.pushState({ view: state.currentView, modal: true }, "");

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
            const framingLogo = srcData.source_logo
                ? `<img class="source-logo-sm" src="${escHtml(srcData.source_logo)}" alt="">`
                : `<span class="compare-source-dot" style="background:${escHtml(srcData.source_color || '#888')}"></span>`;
            return `
            <div class="framing-card">
                <div class="framing-source">
                    ${framingLogo}
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
        const colLogo = s.source_logo
            ? `<img class="source-logo-sm" src="${escHtml(s.source_logo)}" alt="">`
            : `<span class="compare-source-dot" style="background:${escHtml(s.source_color)}"></span>`;
        const artImg = s.image
            ? `<div class="compare-thumb-wrap"><img class="compare-thumb" src="${escHtml(s.image)}" alt="" loading="lazy"></div>`
            : "";
        return `
        <div class="compare-column">
            <div class="compare-source-name">
                ${colLogo}
                ${escHtml(s.source)}
            </div>
            ${artImg}
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

function _closeModalVisual() {
    const modal = $("#compare-modal");
    if (modal.hidden) return;
    modal.hidden = true;
    document.body.style.overflow = "";
}

function closeModal() {
    if ($("#compare-modal").hidden) return;
    history.back();
}

// ── Weekly Summary ────────────────────────────────────────────────────────

let _weeklyData = null;

const _sparkleIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z"/></svg>`;

function _weeklyDateRange(ws, we) {
    if (!ws || !we) return "";
    const a = new Date(ws + "T12:00:00");
    const b = new Date(we + "T12:00:00");
    return `${a.toLocaleDateString("es-AR", { day: "numeric", month: "short" })} – ${b.toLocaleDateString("es-AR", { day: "numeric", month: "short", year: "numeric" })}`;
}

function _setWeeklyHeader(ws, we) {
    const el = $("#weekly-date-range");
    if (!el || !ws || !we) return;
    const fmt = (iso) => {
        const d = new Date(iso + "T12:00:00");
        return d.toLocaleDateString("es-AR", { weekday: "long", day: "numeric", month: "long" });
    };
    const s = fmt(ws), e = fmt(we);
    el.textContent = `${s.charAt(0).toUpperCase() + s.slice(1)} — ${e.charAt(0).toUpperCase() + e.slice(1)}, ${new Date(we + "T12:00:00").getFullYear()}`;
}

function _formatGeneratedAt(isoStr) {
    if (!isoStr) return "";
    const d = new Date(isoStr);
    if (isNaN(d)) return "";
    return d.toLocaleDateString("es-AR", { day: "numeric", month: "short" })
        + ", " + d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
}

function _setAttr(state, ws, we, provider, generatedAt) {
    const attr = $("#weekly-ai-attribution");
    if (!attr) return;
    const range = _weeklyDateRange(ws, we);
    const sep = range ? `<span class="weekly-ai-sep">·</span><span class="weekly-ai-dates">${range}</span>` : "";
    const pw = provider ? `<span class="weekly-ai-sep">·</span><span class="ai-provider">Powered by ${escHtml(provider)}</span>` : "";
    const genLabel = _formatGeneratedAt(generatedAt);
    const gen = genLabel ? `<span class="weekly-ai-sep">·</span><span class="ai-generated-at" title="Fecha de generación">Generado: ${genLabel}</span>` : "";

    if (state === "loading") {
        attr.className = "weekly-ai-attribution weekly-ai-loading";
        attr.innerHTML = `<div class="ai-pulse-dot-sm"></div><span>Generando con IA</span>${sep}`;
        attr.hidden = false;
    } else if (state === "done") {
        attr.className = "weekly-ai-attribution weekly-ai-done";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span><span class="weekly-ai-check">Listo</span>${pw}${sep}${gen}`;
        attr.hidden = false;
        setTimeout(() => {
            const check = attr.querySelector(".weekly-ai-check");
            if (check) check.classList.add("weekly-ai-check-hide");
        }, 2500);
    } else {
        attr.className = "weekly-ai-attribution";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span>${pw}${sep}${gen}`;
        attr.hidden = false;
    }
}

async function loadWeeklySummary() {
    if (_weeklyData) {
        renderWeeklySummary(_weeklyData);
        return;
    }

    const loading = $("#weekly-loading");
    const error = $("#weekly-error");
    const content = $("#weekly-content");
    const footer = $("#weekly-footer");

    if (error) error.hidden = true;
    if (content) content.innerHTML = "";
    if (footer) { footer.hidden = true; footer.innerHTML = ""; }

    let ws = null, we = null;
    try {
        const r = await fetch(`${API}/api/weekly-range`);
        if (r.ok) { const d = await r.json(); ws = d.week_start; we = d.week_end; _setWeeklyHeader(ws, we); }
    } catch (_) { /* no bloquear */ }

    _setAttr("loading", ws, we);
    if (loading) loading.hidden = false;

    let timer;
    try {
        const ctrl = new AbortController();
        timer = setTimeout(() => ctrl.abort(), 180000);
        const resp = await fetch(`${API}/api/weekly-summary`, { signal: ctrl.signal });
        clearTimeout(timer); timer = null;

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (loading) loading.hidden = true;

        if (data.ai_available && data.themes?.length) {
            _weeklyData = data;
            renderWeeklySummary(data);
        } else if (data.ai_available) {
            _setAttr("static", data.week_start || ws, data.week_end || we);
            if (error) { error.innerHTML = `<p>No hay suficientes noticias esta semana para generar un resumen.</p>`; error.hidden = false; }
        } else {
            _setAttr("static", data.week_start || ws, data.week_end || we);
            if (error) { error.innerHTML = `<p>La IA no está disponible en este momento. Intentá de nuevo más tarde.</p>`; error.hidden = false; }
        }
    } catch (err) {
        if (loading) loading.hidden = true;
        const msg = err.name === "AbortError"
            ? "La generación tardó demasiado."
            : "Error al generar el resumen.";
        if (error) { error.innerHTML = `<p>${msg}</p><button class="btn-retry" onclick="_weeklyData=null;loadWeeklySummary()">Reintentar</button>`; error.hidden = false; }
        _setAttr("static", ws, we);
        console.error("Weekly summary failed:", err);
    } finally {
        if (timer) clearTimeout(timer);
    }
}

function renderWeeklySummary(data) {
    const loading = $("#weekly-loading");
    const content = $("#weekly-content");
    const footer = $("#weekly-footer");
    if (loading) loading.hidden = true;

    _setWeeklyHeader(data.week_start, data.week_end);
    _setAttr("done", data.week_start, data.week_end, data.ai_provider, data.generated_at);

    const themes = (data.themes || []).filter(t => t && typeof t === "object");
    if (!themes.length) {
        content.innerHTML = `<div class="empty-state"><h3>Sin temas esta semana</h3></div>`;
        return;
    }

    const hero = themes[0];
    const rest = themes.slice(1);

    const heroHtml = `
        <article class="weekly-hero">
            ${hero.image
                ? `<img class="weekly-hero-image" src="${escHtml(hero.image)}" alt="" loading="lazy" onerror="this.style.display='none'">`
                : `<div class="weekly-hero-image-placeholder"></div>`
            }
            <div class="weekly-hero-body">
                <span class="weekly-theme-emoji weekly-theme-emoji-lg">${hero.emoji || ""}</span>
                <h3 class="weekly-hero-title">${escHtml(hero.label)}</h3>
                <p class="weekly-hero-summary">${escHtml(hero.summary)}</p>
                <div class="weekly-theme-sources">
                    ${(hero.sources || []).map(s => `<span class="weekly-source-badge">${escHtml(s)}</span>`).join("")}
                </div>
            </div>
        </article>`;

    const cardsHtml = rest.map(theme => `
        <article class="weekly-theme-card">
            ${theme.image
                ? `<img class="weekly-card-image" src="${escHtml(theme.image)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\\'weekly-card-image-placeholder\\'></div>'">`
                : `<div class="weekly-card-image-placeholder"></div>`
            }
            <div class="weekly-card-body">
                <span class="weekly-theme-emoji">${theme.emoji || ""}</span>
                <h4 class="weekly-card-title">${escHtml(theme.label)}</h4>
                <p class="weekly-card-summary">${escHtml(theme.summary)}</p>
                <div class="weekly-theme-sources">
                    ${(theme.sources || []).map(s => `<span class="weekly-source-badge">${escHtml(s)}</span>`).join("")}
                </div>
            </div>
        </article>
    `).join("");

    content.innerHTML = heroHtml + `<div class="weekly-themes-grid">${cardsHtml}</div>`;

    const allSources = new Set();
    themes.forEach(t => (t.sources || []).forEach(s => allSources.add(s)));
    if (footer && allSources.size) {
        footer.innerHTML = `Fuentes analizadas: ${[...allSources].sort().join(" · ")}`;
        footer.hidden = false;
    }
}

// ── Top Story ─────────────────────────────────────────────────────────────

let _topStoryData = null;

function _setTopStoryAttr(attrState, dateStr, provider, generatedAt) {
    const attr = $("#topstory-ai-attr");
    if (!attr) return;
    const datePart = dateStr
        ? `<span class="weekly-ai-sep">·</span><span class="weekly-ai-dates">${dateStr}</span>`
        : "";
    const pw = provider ? `<span class="weekly-ai-sep">·</span><span class="ai-provider">Powered by ${escHtml(provider)}</span>` : "";
    const genLabel = _formatGeneratedAt(generatedAt);
    const gen = genLabel ? `<span class="weekly-ai-sep">·</span><span class="ai-generated-at" title="Fecha de generación">Generado: ${genLabel}</span>` : "";

    if (attrState === "loading") {
        attr.className = "weekly-ai-attribution weekly-ai-loading";
        attr.innerHTML = `<div class="ai-pulse-dot-sm"></div><span>Generando con IA</span>${datePart}`;
        attr.hidden = false;
    } else if (attrState === "done") {
        attr.className = "weekly-ai-attribution weekly-ai-done";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span><span class="weekly-ai-check">Listo</span>${pw}${datePart}${gen}`;
        attr.hidden = false;
        setTimeout(() => {
            const check = attr.querySelector(".weekly-ai-check");
            if (check) check.classList.add("weekly-ai-check-hide");
        }, 2500);
    } else {
        attr.className = "weekly-ai-attribution";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span>${pw}${datePart}${gen}`;
        attr.hidden = false;
    }
}

function _todayLabel() {
    const d = new Date();
    return d.toLocaleDateString("es-AR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

async function loadTopStory() {
    if (_topStoryData) {
        renderTopStory(_topStoryData);
        return;
    }

    const loading = $("#topstory-loading");
    const error = $("#topstory-error");
    const content = $("#topstory-content");
    const dateEl = $("#topstory-date");

    if (error) error.hidden = true;
    if (content) content.innerHTML = "";

    const label = _todayLabel();
    if (dateEl) dateEl.textContent = label.charAt(0).toUpperCase() + label.slice(1);

    _setTopStoryAttr("loading", new Date().toLocaleDateString("es-AR", { day: "numeric", month: "short", year: "numeric" }));
    if (loading) loading.hidden = false;

    let timer;
    try {
        const ctrl = new AbortController();
        timer = setTimeout(() => ctrl.abort(), 120000);
        const resp = await fetch(`${API}/api/top-story`, { signal: ctrl.signal });
        clearTimeout(timer); timer = null;

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (loading) loading.hidden = true;

        if (data.ai_available && data.story) {
            _topStoryData = data;
            renderTopStory(data);
        } else if (data.ai_available && !data.story) {
            _setTopStoryAttr("static", "");
            if (error) { error.innerHTML = `<p>No hay suficientes noticias hoy para destacar una.</p>`; error.hidden = false; }
        } else {
            _setTopStoryAttr("static", "");
            if (error) { error.innerHTML = `<p>La IA no está disponible en este momento. Intentá de nuevo más tarde.</p>`; error.hidden = false; }
        }
    } catch (err) {
        if (loading) loading.hidden = true;
        const msg = err.name === "AbortError"
            ? "La generación tardó demasiado."
            : "Error al generar el análisis.";
        if (error) { error.innerHTML = `<p>${msg}</p><button class="btn-retry" onclick="_topStoryData=null;loadTopStory()">Reintentar</button>`; error.hidden = false; }
        _setTopStoryAttr("static", "");
        console.error("Top story failed:", err);
    } finally {
        if (timer) clearTimeout(timer);
    }
}

function renderTopStory(data) {
    const loading = $("#topstory-loading");
    const content = $("#topstory-content");
    if (loading) loading.hidden = true;

    const s = data.story;
    const dateLabel = new Date().toLocaleDateString("es-AR", { day: "numeric", month: "short", year: "numeric" });
    _setTopStoryAttr("done", dateLabel, data.ai_provider, data.generated_at);

    const heroImg = s.image
        ? `<img class="topstory-hero-image" src="${escHtml(s.image)}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : `<div class="topstory-hero-image-placeholder"></div>`;

    const kpHtml = (s.key_points || []).length
        ? `<ul class="topstory-key-points">
            ${s.key_points.map(kp => `<li>${escHtml(kp)}</li>`).join("")}
           </ul>`
        : "";

    const sourceBadges = (s.sources || []).map(src =>
        `<span class="weekly-source-badge">${escHtml(src)}</span>`
    ).join("");

    const articlesHtml = (s.articles || []).map(a => `
        <a href="${escHtml(a.link)}" target="_blank" rel="noopener" class="topstory-article-link">
            <span class="topstory-article-source" style="color:${escHtml(a.source_color || '#888')}">${escHtml(a.source)}</span>
            <span class="topstory-article-title">${escHtml(a.title)}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
        </a>
    `).join("");

    const publishedStr = s.published
        ? formatDate(new Date(s.published))
        : "";

    content.innerHTML = `
        <article class="topstory-hero">
            ${heroImg}
            <div class="topstory-hero-body">
                <div class="topstory-meta">
                    <span class="topstory-category">${escHtml(s.category)}</span>
                    <span class="topstory-source-count">${s.source_count} fuentes cubrieron esta noticia</span>
                    ${publishedStr ? `<span class="topstory-time">${publishedStr}</span>` : ""}
                </div>
                <span class="topstory-emoji">${s.emoji || ""}</span>
                <h3 class="topstory-headline">${escHtml(s.title)}</h3>
                <p class="topstory-original-title">Título original: "${escHtml(s.original_title)}"</p>
                <div class="topstory-summary">${escHtml(s.summary).replace(/\n/g, "<br>")}</div>
                ${kpHtml}
                <div class="topstory-sources-section">
                    <h4 class="topstory-section-label">Fuentes que la cubrieron</h4>
                    <div class="weekly-theme-sources">${sourceBadges}</div>
                </div>
                <div class="topstory-articles-section">
                    <h4 class="topstory-section-label">Leer la nota en cada medio</h4>
                    <div class="topstory-articles-list">${articlesHtml}</div>
                </div>
            </div>
        </article>`;
}

// ── Resumen de Temas ──────────────────────────────────────────────────────

let _temasSubview = "topics"; // "topics" | "detail"

function _setTemasAttr(attrState, extra, provider) {
    const attr = $("#temas-ai-attr");
    if (!attr) return;
    const datePart = extra
        ? `<span class="weekly-ai-sep">·</span><span class="weekly-ai-dates">${escHtml(extra)}</span>`
        : "";
    const pw = provider ? `<span class="weekly-ai-sep">·</span><span class="ai-provider">Powered by ${escHtml(provider)}</span>` : "";

    if (attrState === "loading") {
        attr.className = "weekly-ai-attribution weekly-ai-loading";
        attr.innerHTML = `<div class="ai-pulse-dot-sm"></div><span>Buscando con IA</span>${datePart}`;
        attr.hidden = false;
    } else if (attrState === "done") {
        attr.className = "weekly-ai-attribution weekly-ai-done";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span><span class="weekly-ai-check">Listo</span>${pw}${datePart}`;
        attr.hidden = false;
        setTimeout(() => {
            const check = attr.querySelector(".weekly-ai-check");
            if (check) check.classList.add("weekly-ai-check-hide");
        }, 2500);
    } else {
        attr.className = "weekly-ai-attribution";
        attr.innerHTML = `${_sparkleIcon}<span>Generado con IA</span>${pw}${datePart}`;
        attr.hidden = false;
    }
}

function _updateTemasBackBtn() {
    const label = $("#btn-back-temas-label");
    if (label) label.textContent = _temasSubview === "detail" ? "Volver a temas" : "Volver a noticias";
}

async function loadTemasView() {
    _temasSubview = "topics";
    _updateTemasBackBtn();

    const loading = $("#temas-loading");
    const error = $("#temas-error");
    const content = $("#temas-content");
    const subtitle = $("#temas-subtitle");

    if (error) error.hidden = true;
    if (content) content.innerHTML = "";
    if (subtitle) subtitle.textContent = "Los temas más importantes del día";

    if (_isTopicsCacheValid() && _topicsCache?.length) {
        renderTemasCards(_topicsCache);
        _setTemasAttr("static", "");
        return;
    }

    if (loading) loading.hidden = false;
    _setTemasAttr("loading", "");

    const maxAttempts = 2;
    let lastErr;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        try {
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), 45000);
            const resp = await fetch(`${API}/api/topics`, { signal: ctrl.signal });
            clearTimeout(timer);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();

            if (loading) loading.hidden = true;

            if (data.ai_available && data.topics?.length) {
                _topicsCache = data.topics;
                _topicsCacheTs = Date.now();
                _topicsSearchCached = new Set(data.search_cached || []);
                renderTemasCards(data.topics);
                _setTemasAttr("done", "", data.ai_provider);
            } else if (data.ai_available) {
                _setTemasAttr("static", "");
                if (error) { error.innerHTML = "<p>No hay suficientes noticias para extraer temas.</p>"; error.hidden = false; }
            } else {
                _setTemasAttr("static", "");
                if (error) { error.innerHTML = "<p>La IA no está disponible en este momento.</p>"; error.hidden = false; }
            }
            return;
        } catch (err) {
            lastErr = err;
            if (attempt < maxAttempts) {
                console.warn(`Temas attempt ${attempt} failed, retrying…`, err);
                await new Promise(r => setTimeout(r, 2000));
            }
        }
    }

    if (loading) loading.hidden = true;
    _setTemasAttr("static", "");
    if (error) {
        error.innerHTML = `<p>Error al cargar los temas.</p><button class="btn-retry" onclick="loadTemasView()">Reintentar</button>`;
        error.hidden = false;
    }
    console.error("Temas load failed after retries:", lastErr);
}

function renderTemasCards(topics) {
    const content = $("#temas-content");
    if (!content) return;

    const cardsHtml = topics.map(t => `
        <button class="tema-card" data-query="${escHtml(t.label)}">
            <span class="tema-card-emoji">${t.emoji}</span>
            <span class="tema-card-label">${escHtml(t.label)}</span>
            <span class="tema-card-hint">Explorar noticias</span>
        </button>
    `).join("");

    content.innerHTML = `<div class="temas-grid">${cardsHtml}</div>`;

    content.querySelectorAll(".tema-card").forEach(card => {
        card.addEventListener("click", () => loadTemaDetail(card.dataset.query));
    });
}

async function loadTemaDetail(label) {
    _temasSubview = "detail";
    _updateTemasBackBtn();

    const loading = $("#temas-loading");
    const error = $("#temas-error");
    const content = $("#temas-content");
    const subtitle = $("#temas-subtitle");

    if (error) error.hidden = true;
    if (content) content.innerHTML = "";
    if (subtitle) subtitle.textContent = label;

    _setTemasAttr("loading", label);
    if (loading) loading.hidden = false;

    let timer;
    try {
        const ctrl = new AbortController();
        timer = setTimeout(() => ctrl.abort(), 60000);
        const resp = await fetch(`${API}/api/search?q=${encodeURIComponent(label)}`, { signal: ctrl.signal });
        clearTimeout(timer); timer = null;

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (loading) loading.hidden = true;

        if (data.ai_available && data.relevant_group_ids?.length) {
            const matched = data.matched_groups?.length
                ? data.matched_groups
                : state.groups.filter(g => new Set(data.relevant_group_ids).has(g.group_id));

            const summaryHtml = data.summary
                ? `<div class="temas-detail-summary">
                    <p>${escHtml(data.summary)}</p>
                   </div>`
                : "";

            const countHtml = `<div class="temas-detail-count">${matched.length} noticia${matched.length !== 1 ? "s" : ""} encontrada${matched.length !== 1 ? "s" : ""}</div>`;

            const newsHtml = matched.length
                ? matched.map(g => renderCard(g)).join("")
                : `<div class="empty-state"><h3>No se encontraron noticias para este tema</h3></div>`;

            content.innerHTML = summaryHtml + countHtml + `<div class="news-grid">${newsHtml}</div>`;

            content.querySelectorAll(".news-card").forEach((card, i) => {
                card.addEventListener("click", () => openComparison(matched[i]));
            });

            _setTemasAttr("done", label, data.ai_provider);
        } else if (data.ai_available) {
            _setTemasAttr("static", label);
            if (error) { error.innerHTML = "<p>No se encontraron noticias para este tema.</p>"; error.hidden = false; }
        } else {
            _setTemasAttr("static", "");
            if (error) { error.innerHTML = "<p>La IA no está disponible en este momento.</p>"; error.hidden = false; }
        }
    } catch (err) {
        if (loading) loading.hidden = true;
        const msg = err.name === "AbortError"
            ? "La búsqueda tardó demasiado. Intentá de nuevo."
            : "Error al buscar noticias. Intentá de nuevo.";
        if (error) { error.innerHTML = `<p>${msg}</p>`; error.hidden = false; }
        _setTemasAttr("static", "");
        console.error("Tema detail failed:", err);
    } finally {
        if (timer) clearTimeout(timer);
    }

    window.scrollTo({ top: 0, behavior: "smooth" });
}

// ── Tracking ──────────────────────────────────────────────────────────────

const _trackQueue = [];
const _sessionId = sessionStorage.getItem("vs_sid") || crypto.randomUUID();
sessionStorage.setItem("vs_sid", _sessionId);

function track(type, data = {}) {
    _trackQueue.push({ type, data, ts: new Date().toISOString() });
}

function flushTrack() {
    if (!_trackQueue.length) return;
    const batch = _trackQueue.splice(0);
    const payload = JSON.stringify({ session_id: _sessionId, events: batch });
    if (navigator.sendBeacon) {
        navigator.sendBeacon(`${API}/api/track`, new Blob([payload], { type: "application/json" }));
    } else {
        fetch(`${API}/api/track`, { method: "POST", body: payload, headers: { "Content-Type": "application/json" }, keepalive: true }).catch(() => {});
    }
}

setInterval(flushTrack, 10000);
window.addEventListener("beforeunload", flushTrack);
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushTrack();
});

// ── Auth ──────────────────────────────────────────────────────────────────

async function initAuth() {
    try {
        const resp = await fetch(`${API}/auth/me`);
        const data = await resp.json();
        state.user = data.user || null;
    } catch {
        state.user = null;
    }
    renderAuthUI();

    if (state.user) {
        const pending = localStorage.getItem("pending_view");
        if (pending) {
            localStorage.removeItem("pending_view");
            switchView(pending);
        }
    }
}

function setupAuth() {
    const btnLogin = $("#btn-login");
    const loginModal = $("#login-modal");
    const loginClose = $("#login-modal-close");
    const menuToggle = $("#user-menu-toggle");
    const dropdown = $("#user-dropdown");
    const btnLogout = $("#btn-logout");
    const magicForm = $("#magic-link-form");

    if (btnLogin) btnLogin.addEventListener("click", () => openLoginModal());
    if (loginClose) loginClose.addEventListener("click", () => closeLoginModal());
    if (loginModal) loginModal.addEventListener("click", (e) => {
        if (e.target === loginModal) closeLoginModal();
    });

    if (menuToggle) menuToggle.addEventListener("click", () => {
        dropdown.hidden = !dropdown.hidden;
    });

    document.addEventListener("click", (e) => {
        if (dropdown && !dropdown.hidden && !e.target.closest(".user-menu")) {
            dropdown.hidden = true;
        }
    });

    if (btnLogout) btnLogout.addEventListener("click", async () => {
        await fetch(`${API}/auth/logout`, { method: "POST" });
        state.user = null;
        renderAuthUI();
        dropdown.hidden = true;
    });

    if (magicForm) magicForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const emailInput = $("#magic-email");
        const btn = $("#magic-link-btn");
        const statusEl = $("#magic-link-status");
        const email = emailInput.value.trim();
        if (!email) return;

        btn.disabled = true;
        btn.textContent = "Enviando...";
        statusEl.hidden = true;

        try {
            const resp = await fetch(`${API}/auth/magic/request`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email }),
            });
            const data = await resp.json();
            if (resp.ok) {
                statusEl.className = "magic-link-status success";
                statusEl.textContent = "Revisá tu email — te enviamos un link para iniciar sesión.";
            } else {
                statusEl.className = "magic-link-status error";
                statusEl.textContent = data.detail || "Error al enviar el link.";
            }
        } catch {
            statusEl.className = "magic-link-status error";
            statusEl.textContent = "Error de conexión. Intentá de nuevo.";
        }
        statusEl.hidden = false;
        btn.disabled = false;
        btn.textContent = "Enviar link de acceso";
    });

    const params = new URLSearchParams(location.search);
    if (params.has("auth_error")) {
        const err = params.get("auth_error");
        const msgs = {
            google: "Error al iniciar sesión con Google.",
            token: "Error al obtener el token de Google.",
            exchange: "Error de comunicación con Google.",
            no_email: "No se pudo obtener el email.",
            expired: "El link de acceso expiró. Pedí uno nuevo.",
            invalid: "Link de acceso inválido.",
            no_token: "Link de acceso inválido.",
        };
        showToast(msgs[err] || "Error de autenticación.");
        history.replaceState(null, "", location.pathname + location.hash);
    }
}

function renderAuthUI() {
    const btnLogin = $("#btn-login");
    const userMenu = $("#user-menu");
    const adminCard = $("#hero-admin-card");
    const adminLink = $("#btn-admin-link");

    if (state.user) {
        if (btnLogin) btnLogin.hidden = true;
        if (userMenu) {
            userMenu.hidden = false;
            const avatar = $("#user-avatar");
            const nameEl = $("#user-name");
            if (avatar) avatar.src = state.user.picture || "";
            if (nameEl) nameEl.textContent = state.user.name || state.user.email.split("@")[0];
        }
        if (adminCard) adminCard.hidden = state.user.role !== "admin";
        if (adminLink) adminLink.hidden = state.user.role !== "admin";
    } else {
        if (btnLogin) btnLogin.hidden = false;
        if (userMenu) userMenu.hidden = true;
        if (adminCard) adminCard.hidden = true;
        if (adminLink) adminLink.hidden = true;
    }
}

function openLoginModal() {
    const modal = $("#login-modal");
    if (modal) {
        modal.hidden = false;
        document.body.style.overflow = "hidden";
    }
}

function closeLoginModal() {
    const modal = $("#login-modal");
    if (modal) {
        modal.hidden = true;
        document.body.style.overflow = "";
    }
    const statusEl = $("#magic-link-status");
    if (statusEl) statusEl.hidden = true;
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
