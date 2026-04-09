# Backlog de mejoras — Comparador de Noticias

Lista priorizable de mejoras para el proyecto.

## 1. Integración con X

Definir alcance: ingestión de hilos o enlaces desde cuentas de medios (API de X, con costos y límites), solo “compartir en X” desde la UI, o enlaces/embeds a posts relacionados con una noticia. Incluye: credenciales seguras, límites de rate, y cómo integrar esos ítems con el modelo `Article` y el agrupador.

## 2. Más fuentes o tipos de entrada

Otros medios, newsletters, o fuentes sin RSS estable (evaluar scraping con cuidado legal y técnico).

## 3. Producto / UX

Búsqueda full-text, favoritos, alertas, modo claro, accesibilidad, PWA u offline.

## 4. Robustez del comparador / agrupamiento

Revisar casos límite (títulos muy distintos, duplicados, cambios en `comparator.py`) y mantener la suite de tests al día.

## 5. Operación

Backups de datos, healthchecks más ricos que `/api/status`, límites en `/api/refresh` frente a abuso.

## 6. Observabilidad y métricas

Formalizar qué se mide, retención, dashboard o export. Evitar commitear bases de datos locales de desarrollo.

## 7. Documentación alineada con el código — **hecho**

El `README` refleja `app/config.py` (fuentes, categorías), autenticación (`app/auth.py`) y los endpoints principales de `app/main.py`.
