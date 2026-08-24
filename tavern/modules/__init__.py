"""1.0 plugin-domain module orchestration."""

from .catalog import PLUGIN_MODULES, PluginModuleSpec
from .manager import ModuleDependencyError, PluginModuleManager

__all__ = [
    "PLUGIN_MODULES",
    "ModuleDependencyError",
    "PluginModuleManager",
    "PluginModuleSpec",
]
