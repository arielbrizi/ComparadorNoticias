"""Feature flags — habilitar/deshabilitar funcionalidades del usuario.

Almacenamiento en la tabla ``ai_runtime_config`` (clave/valor) que ya existe
para configuración runtime. Cada flag se guarda con la clave
``feature_flag.<name>`` y valor ``"1"`` / ``"0"``. Esto evita una migración
nueva y reusa el cache de 30s ya implementado en ``ai_store``.

Para agregar un flag nuevo basta con sumar una entrada al diccionario
``FEATURE_FLAGS``: la admin UI los renderiza solos, el endpoint público los
expone, y el frontend solo necesita reaccionar a la clave correspondiente.
"""

from __future__ import annotations

import logging

from app.ai_store import _get_runtime_value, _set_runtime_value

logger = logging.getLogger(__name__)

_KEY_PREFIX = "feature_flag."


# ── Registro de flags ────────────────────────────────────────────────────
#
# Cada entrada describe un flag: ``label`` y ``description`` se muestran en
# el panel admin; ``default`` es el valor cuando todavía no se persistió
# nada en DB (preserva el comportamiento previo al introducir el flag).
FEATURE_FLAGS: dict[str, dict] = {
    "hero_search": {
        "label": "Búsqueda principal (¿Qué querés comparar hoy?)",
        "description": (
            "Muestra u oculta el título y campo de búsqueda del hero en la "
            "home. Cuando está deshabilitado, los Temas del día siguen "
            "visibles tanto en mobile como en desktop."
        ),
        "default": True,
    },
}


def _storage_key(name: str) -> str:
    return f"{_KEY_PREFIX}{name}"


def _parse_bool(raw: str | None) -> bool | None:
    """Convert a stored value to bool. Returns None when unparseable."""
    if raw is None:
        return None
    norm = raw.strip().lower()
    if norm in ("1", "true", "yes", "on"):
        return True
    if norm in ("0", "false", "no", "off"):
        return False
    return None


def is_known_flag(name: str) -> bool:
    return name in FEATURE_FLAGS


def get_flag(name: str) -> bool:
    """Return the current value of *name*, falling back to its default.

    Unknown flag names raise ``KeyError`` so callers can't silently typo into
    an always-false branch.
    """
    if name not in FEATURE_FLAGS:
        raise KeyError(f"Unknown feature flag: {name}")
    raw = _get_runtime_value(_storage_key(name))
    parsed = _parse_bool(raw)
    if parsed is None:
        return bool(FEATURE_FLAGS[name].get("default", True))
    return parsed


def set_flag(name: str, enabled: bool) -> bool:
    """Persist a new value for *name*. Returns True on success."""
    if name not in FEATURE_FLAGS:
        logger.warning("Refusing to set unknown feature flag: %s", name)
        return False
    if not isinstance(enabled, bool):
        logger.warning(
            "Refusing to set feature flag %s with non-bool value: %r",
            name, enabled,
        )
        return False
    ok = _set_runtime_value(_storage_key(name), "1" if enabled else "0")
    if ok:
        logger.info("Feature flag updated: %s = %s", name, enabled)
    return ok


def get_all_flags() -> dict[str, bool]:
    """Return ``{name: enabled}`` for every registered flag."""
    return {name: get_flag(name) for name in FEATURE_FLAGS}


def describe_flags() -> list[dict]:
    """Return a list shaped for the admin UI: one row per flag."""
    rows = []
    for name, meta in FEATURE_FLAGS.items():
        rows.append(
            {
                "name": name,
                "label": meta.get("label", name),
                "description": meta.get("description", ""),
                "enabled": get_flag(name),
                "default": bool(meta.get("default", True)),
            }
        )
    return rows
