# Comparador de Noticias — Argentina

Plataforma web que reúne noticias de los principales medios argentinos, las agrupa por tema y permite comparar cómo cada medio cubre la misma historia.

## Fuentes

| Medio | Tipo | Secciones |
|-------|------|-----------|
| Infobae | RSS | Portada |
| Clarín | RSS | Portada, Política, Economía, Sociedad, Deportes |
| La Nación | RSS | Portada, Política, Economía, Sociedad, Deportes |
| Página 12 | RSS | Portada |
| Ámbito Financiero | RSS | Economía, Política |
| Perfil | RSS | Portada |
| Buenos Aires Times | RSS | Portada |

## Funcionalidades

- **Agregación automática**: Obtiene noticias de 7+ medios vía RSS cada 10 minutos
- **Agrupamiento inteligente**: Detecta cuándo diferentes medios cubren la misma noticia usando fuzzy matching
- **Comparación lado a lado**: Modal que muestra los títulos, resúmenes y enlaces de cada medio
- **Resaltado de diferencias**: Marca las palabras que difieren entre las versiones de cada fuente
- **Filtros**: Por categoría (Política, Economía, Sociedad, Deportes) y por fuente
- **Vista multi-fuente**: Toggle para mostrar solo noticias cubiertas por 2+ medios

## Requisitos

- Python 3.11+

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abrir http://localhost:8000 en el navegador.

## API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/noticias` | GET | Todas las noticias (con filtros `categoria`, `fuente`, `limit`, `offset`) |
| `/api/grupos` | GET | Noticias agrupadas por tema (`categoria`, `solo_multifuente`, `limit`) |
| `/api/grupo/{id}` | GET | Detalle de un grupo |
| `/api/comparar/{id}` | GET | Comparación detallada de un grupo |
| `/api/fuentes` | GET | Fuentes configuradas |
| `/api/categorias` | GET | Categorías disponibles |
| `/api/status` | GET | Estado del sistema y feeds |
| `/api/refresh` | POST | Forzar actualización de noticias |

## Estructura

```
ComparadorNoticias/
├── app/
│   ├── main.py              # FastAPI + endpoints
│   ├── config.py            # Fuentes RSS y configuración
│   ├── feed_reader.py       # Lectura de feeds RSS
│   ├── article_grouper.py   # Agrupamiento por similitud
│   └── models.py            # Modelos Pydantic
├── static/
│   ├── index.html           # Frontend
│   ├── css/styles.css       # Estilos (dark theme)
│   └── js/app.js            # Lógica del frontend
└── requirements.txt
```
