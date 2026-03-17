"""
Configuración de fuentes de noticias argentinas.
Cada fuente tiene feeds RSS organizados por categoría.
"""

SOURCES = {
    "Infobae": {
        "color": "#e63946",
        "logo": "",
        "feeds": {
            "portada": "https://www.infobae.com/arc/outboundfeeds/rss/",
        },
    },
    "Clarín": {
        "color": "#1a73e8",
        "logo": "",
        "feeds": {
            "portada": "https://www.clarin.com/rss/lo-ultimo/",
            "politica": "https://www.clarin.com/rss/politica/",
            "economia": "https://www.clarin.com/rss/economia/",
            "sociedad": "https://www.clarin.com/rss/sociedad/",
            "deportes": "https://www.clarin.com/rss/deportes/",
        },
    },
    "La Nación": {
        "color": "#2d6a4f",
        "logo": "",
        "feeds": {
            "portada": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/",
            "politica": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/politica/",
            "economia": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/economia/",
            "sociedad": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/sociedad/",
            "deportes": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/deportes/",
        },
    },
    "Página 12": {
        "color": "#e76f51",
        "logo": "",
        "feeds": {
            "portada": "https://www.pagina12.com.ar/rss/portada",
        },
    },
    "Ámbito Financiero": {
        "color": "#f4a261",
        "logo": "",
        "feeds": {
            "economia": "https://www.ambito.com/rss/economia.xml",
            "politica": "https://www.ambito.com/rss/politica.xml",
        },
    },
    "Perfil": {
        "color": "#7209b7",
        "logo": "",
        "feeds": {
            "portada": "https://www.perfil.com/feed",
        },
    },
    "Buenos Aires Times": {
        "color": "#3a86a8",
        "logo": "",
        "feeds": {
            "portada": "https://www.batimes.com.ar/feed",
        },
    },
}

CATEGORIES = ["portada", "politica", "economia", "sociedad", "deportes"]

SIMILARITY_THRESHOLD = 55

MAX_ARTICLES_PER_FEED = 30

FETCH_TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
