# Comparador de Noticias — Argentina

Plataforma web (**Vs News**) que reúne noticias de los principales medios argentinos, las agrupa por tema y permite comparar cómo cada medio cubre la misma historia.

## Fuentes

Las fuentes y feeds RSS están definidas en `app/config.py` (`SOURCES`). Resumen:

| Medio | Tipo | Secciones (feeds RSS) |
|-------|------|------------------------|
| Infobae | RSS | Portada, Política, Economía, Sociedad, Deportes |
| Clarín | RSS | Portada, Política, Economía, Sociedad, Deportes |
| La Nación | RSS | Portada, Política, Economía, Sociedad, Deportes |
| Página 12 | RSS | Portada, Política, Economía, Sociedad, Deportes |
| Ámbito Financiero | RSS | Portada, Política, Economía, Sociedad (nacional), Deportes |
| Perfil | RSS | Portada, Política, Economía, Sociedad, Deportes |
| Buenos Aires Times | RSS | Portada |

**Nota:** Infobae filtra por URL artículos de ediciones internacionales (México, Colombia, etc.); el resto de fuentes usa solo los feeds indicados.

## Categorías

Coinciden con las claves de cada feed en `SOURCES` y con `CATEGORIES` en `app/config.py`:

`portada`, `politica`, `economia`, `sociedad`, `deportes`

## Funcionalidades

- **Agregación automática**: noticias vía RSS cada 10 minutos (scheduler en `app/main.py`).
- **Persistencia**: SQLite para noticias/grupos, métricas de agenda y usuarios (ver módulos `app/news_store.py`, `app/metrics_store.py`, `app/user_store.py`).
- **Agrupamiento**: detecta cuando varios medios cubren la misma noticia (fuzzy matching en `app/article_grouper.py`).
- **Comparación**: análisis lado a lado y diferencias de texto (`app/comparator.py`).
- **Búsqueda y resúmenes con IA**: búsqueda semántica, temas del día, noticia destacada y resumen semanal (`app/ai_search.py`; proveedores Gemini / Groq según configuración).
- **Word cloud**: términos frecuentes en títulos (`/api/wordcloud`).
- **Métricas de agenda**: historial en `/api/metricas`.
- **Seguimiento de uso**: eventos desde el frontend (`POST /api/track`).
- **Filtros en UI**: categoría, fuente, rango de fechas, vista multi-fuente.

## Autenticación

Configuración en `app/config.py` y rutas en `app/auth.py`:

| Mecanismo | Descripción |
|-----------|-------------|
| **JWT** | Sesión en cookie `vs_token` tras login correcto. `JWT_SECRET` obligatorio en producción; `JWT_EXPIRE_HOURS` por defecto 72. |
| **Google OAuth** | `GET /auth/google/login` → callback en `/auth/google/callback`. Requiere `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` y `BASE_URL` acorde al redirect URI. |
| **Magic link** | `POST /auth/magic/request` envía email vía Resend si `RESEND_API_KEY` está definido; sin API key el enlace se registra solo en logs (desarrollo). Verificación: `GET /auth/magic/verify`. |
| **Sesión actual** | `GET /auth/me`, `POST /auth/logout`. |
| **Administradores** | Los emails listados en `ADMIN_EMAILS` reciben rol `admin` al iniciar sesión (ver `app/user_store.py`). |

## Requisitos

- Python 3.11+

## Instalación

```bash
pip install -r requirements.txt
```

Variables de entorno habituales: `JWT_SECRET`, `BASE_URL`, y opcionalmente `GOOGLE_*`, `RESEND_API_KEY`, `ADMIN_EMAILS`, claves de IA según `app/ai_search.py` / `.env` de ejemplo si existe.

## Uso

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abrir http://localhost:8000 en el navegador. Panel admin: `/admin` (solo usuarios con rol `admin`).

## API (resumen)

### Noticias y comparación

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/noticias` | GET | Listado (`categoria`, `fuente`, `limit`, `offset`) |
| `/api/grupos` | GET | Grupos (`categoria`, `solo_multifuente`, `desde`, `hasta`, `limit`, `offset`) |
| `/api/grupo/{id}` | GET | Detalle de un grupo |
| `/api/comparar/{id}` | GET | Comparación detallada |
| `/api/fuentes` | GET | Fuentes con colores, logos y categorías por medio |
| `/api/categorias` | GET | Lista de categorías |
| `/api/status` | GET | Estado, totales y estado de feeds (`desde`, `hasta` opcionales) |
| `/api/metricas` | GET | Métricas de agenda (`desde`, `hasta`) |
| `/api/refresh` | POST | Forzar actualización de noticias |

### IA y word cloud

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/search` | GET | Búsqueda asistida por IA (`q`) |
| `/api/topics` | GET | Temas del día (IA, cache) |
| `/api/weekly-range` | GET | Rango lunes–hoy (ART) |
| `/api/weekly-summary` | GET | Resumen semanal (IA) |
| `/api/top-story` | GET | Noticia destacada del día (IA) |
| `/api/wordcloud` | GET | Términos frecuentes (últimas 24 h) |

### Tracking

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/track` | POST | Lote de eventos de uso (opcional usuario autenticado) |

### Admin (JWT + rol `admin`)

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/admin/dashboard` | GET | Resumen de uso y engagement |
| `/api/admin/users` | GET | Listado de usuarios |
| `/api/admin/popular-searches` | GET | Búsquedas populares |
| `/api/admin/top-content` | GET | Contenido más visto |
| `/api/admin/daily-activity` | GET | Actividad por día |
| `/api/admin/hourly` | GET | Distribución horaria |
| `/api/admin/anonymous` | GET | Métricas de visitantes anónimos |
| `/api/admin/debug-headers` | GET | Depuración de cabeceras / IP |
| `/api/admin/purge-proxy-events` | POST | Purga eventos con IP de proxy |

### Auth (prefijo `/auth`)

Ver sección **Autenticación**: `/auth/google/login`, `/auth/google/callback`, `/auth/magic/request`, `/auth/magic/verify`, `/auth/me`, `/auth/logout`.

### Otros

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/` | GET | SPA principal |
| `/admin` | GET | Panel admin (HTML) |
| `/privacy`, `/terms` | GET | Páginas estáticas |

## Estructura

```
ComparadorNoticias/
├── app/
│   ├── main.py              # FastAPI, scheduler, endpoints
│   ├── config.py            # Fuentes RSS, auth, categorías
│   ├── auth.py              # Google OAuth, magic links, JWT
│   ├── feed_reader.py       # Lectura de feeds RSS
│   ├── article_grouper.py   # Agrupamiento por similitud
│   ├── comparator.py        # Comparación de textos
│   ├── ai_search.py         # Búsqueda y resúmenes con IA
│   ├── models.py            # Modelos Pydantic
│   ├── news_store.py        # Persistencia de noticias
│   ├── metrics_store.py     # Métricas de agenda
│   ├── user_store.py        # Usuarios
│   └── tracking_store.py    # Eventos de uso
├── static/                  # index.html, admin, CSS, JS
├── requirements.txt
└── pyproject.toml
```
