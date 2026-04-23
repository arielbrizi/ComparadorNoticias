# Backlog de mejoras — Comparador de Noticias

Lista priorizable de mejoras para el proyecto. Ver el [README](./README.md)
para el estado actual del producto.

## 1. Integración con X (ex-Twitter)

Definir alcance: ingesta de hilos o enlaces desde cuentas de medios (API de X,
con costos y límites), sólo "compartir en X" desde la UI, o enlaces/embeds a
posts relacionados con una noticia. Incluye: credenciales seguras, límites de
rate y cómo integrar esos ítems con el modelo `Article` y el agrupador.

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
