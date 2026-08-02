"""Stable, permission-preserving extension surface for AI Tavern."""

from .hooks import HookRegistry
from .public import TavernPublicAPI
from .registry import ExtensionRegistry

__all__ = ["ExtensionRegistry", "HookRegistry", "TavernPublicAPI"]
