# Backlog de mejoras — Comparador de Noticias

Lista priorizable de mejoras para el proyecto. Ver el [README](./README.md)
para el estado actual del producto.

## 1. Integración con X (ex-Twitter) — **hecho**

Se implementó la integración de salida con la API de X v2 para publicar
5 tipos de campañas configurables desde el panel admin
([tab "Campañas X"](./README.md#campañas-x)):

- **Nube del día** — PNG renderizado del wordcloud + tweet.
- **Noticia del día** — tweet con el top story del día (reutiliza `ai_top_story`).
- **Resumen semanal** — hilo breve con los temas editoriales (reutiliza `ai_weekly_summary`).
- **Temas del día** — hilo con los trending topics (reutiliza `ai_topics`).
- **Breaking news** — disparo reactivo cuando aparece un grupo con ≥ N fuentes.

La auth es OAuth 2.0 de cuenta única, con tokens iniciales en env vars y
refresh automático persistido en la tabla `x_oauth_state` (los tokens rotados
sobreviven a redeploys). El admin configura el **tier contratado**
(Free / Basic / Pro / Custom) y los caps diarios/mensuales se enforcean en
`x_store.check_cap` antes de cada posteo.

Módulos:

- [`app/x_store.py`](./app/x_store.py) — DB, tier, CRUD de campañas, log de uso.
- [`app/x_client.py`](./app/x_client.py) — HTTP sobre X API v2 + refresh automático en 401.
- [`app/x_campaigns.py`](./app/x_campaigns.py) — runners de cada tipo de post.
- [`static/admin.html`](./static/admin.html) + [`static/js/admin.js`](./static/js/admin.js) — tab "Campañas X".

Follow-ups posibles (nuevos ítems): polls, DMs automáticos, múltiples cuentas,
embed de tweets de medios argentinos en la UI de grupos.

## 2. Más fuentes o tipos de entrada

Otros medios argentinos, newsletters, o fuentes sin RSS estable (evaluar
scraping con cuidado legal y técnico). También: medios regionales por
provincia, medios especializados (deportes, economía) y agencias (Télam
sucesor, Noticias Argentinas).

## 3. Producto / UX

- Búsqueda full-text nativa (Postgres FTS) como complemento a la IA.
- Favoritos y alertas por tema.
- Modo claro / ajuste de contraste.
- Accesibilidad (ARIA, navegación por teclado).
- PWA / instalable / offline básico.

## 4. Robustez del comparador y del agrupamiento

Revisar casos límite (títulos muy distintos para la misma nota, duplicados,
cambios en `comparator.py`, expiración de grupos). Mantener la suite de tests
al día cuando se toque `app/article_grouper.py`.

## 5. Operación

- Backups automáticos de Postgres (Railway snapshots o `pg_dump` a storage).
- Healthchecks más ricos que `/health` (ej. un `/ready` que compruebe DB,
  último `refresh_news` exitoso, y estado de providers de IA).
- Rate limiting real en `/api/refresh` y `/api/search` para evitar abuso.

## 6. Observabilidad y métricas

- Retención formal por tabla (hoy se purgan noticias, eventos y process
  events; falta definir retención explícita para `ai_usage_log` e
  `infra_cost_snapshots`).
- Dashboard o export para uso histórico (hoy está en el panel admin).
- Nunca commitear bases de datos locales (`data/metrics.db` ya está en
  `.gitignore`).

## 7. Documentación alineada con el código — **hecho**

El [`README.md`](./README.md) refleja el estado actual del proyecto:
arquitectura, stack, fuentes (`app/config.py`), pipeline de procesamiento,
proveedores de IA (`app/ai_search.py` + `app/ai_store.py`), scheduler
(`app/main.py`), modelo de datos, autenticación (`app/auth.py`), panel
admin, API completa, variables de entorno y deploy en Railway.

Para mantenerlo sincronizado, la regla
[`.cursor/rules/update-docs.mdc`](./.cursor/rules/update-docs.mdc) define el
protocolo: antes de cada push que toque código relevante, el agente revisa
el checklist y actualiza README / BACKLOG / reglas en el mismo commit.
