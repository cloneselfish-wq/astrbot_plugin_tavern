from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import ExtensionRegistry, HookRegistry, TavernPublicAPI
from .database import TavernDatabase
from .engine import TavernEngine
from .events import EventBroker
from .modules import PluginModuleManager
from .protocol import TwpPackageService
from .web_console import TavernWebConsole


@dataclass(slots=True)
class TavernRuntime:
    database: TavernDatabase
    broker: EventBroker
    engine: TavernEngine
    web_console: TavernWebConsole
    hooks: HookRegistry
    extensions: ExtensionRegistry
    public_api: TavernPublicAPI
    modules: PluginModuleManager
    world_twp: TwpPackageService


def build_runtime(
    *,
    context: Any,
    plugin_config: Any,
    data_dir: Path,
    config_provider: Any,
    logger: Any,
    allow_group: Any,
    config_lock: Any,
) -> TavernRuntime:
    hooks = HookRegistry()
    extensions = ExtensionRegistry()
    modules = PluginModuleManager(state_path=Path(data_dir) / "plugin_modules.json")
    world_twp = TwpPackageService(data_dir)
    broker = EventBroker(hooks=hooks)
    database = TavernDatabase(data_dir)
    engine = TavernEngine(
        context=context,
        database=database,
        config_provider=config_provider,
        broker=broker,
        extensions=extensions,
    )
    web_console = TavernWebConsole(
        context=context,
        plugin_config=plugin_config,
        database=database,
        broker=broker,
        data_dir=data_dir,
        logger=logger,
        allow_group=allow_group,
        config_lock=config_lock,
        extensions=extensions,
        hooks=hooks,
        engine=engine,
        modules=modules,
        world_twp=world_twp,
    )
    return TavernRuntime(
        database=database,
        broker=broker,
        engine=engine,
        web_console=web_console,
        hooks=hooks,
        extensions=extensions,
        public_api=TavernPublicAPI(database, hooks, extensions, engine),
        modules=modules,
        world_twp=world_twp,
    )
