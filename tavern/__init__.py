"""Core services for astrbot_plugin_tavern.

Public exports are loaded lazily so reading the current configuration boundary does
not initialize the database/compiler graph as an import side effect.
"""

from importlib import import_module

__all__ = [
    "TavernConfig",
    "TavernDatabase",
    "TavernEngine",
    "EmergencyService",
    "ExtensionRegistry",
    "HookRegistry",
    "TavernPublicAPI",
]


_EXPORTS = {
    "TavernConfig": (".config", "TavernConfig"),
    "TavernDatabase": (".database", "TavernDatabase"),
    "TavernEngine": (".engine", "TavernEngine"),
    "EmergencyService": (".emergency", "EmergencyService"),
    "ExtensionRegistry": (".api", "ExtensionRegistry"),
    "HookRegistry": (".api", "HookRegistry"),
    "TavernPublicAPI": (".api", "TavernPublicAPI"),
}


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
