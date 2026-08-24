from __future__ import annotations

from .web_console_shared import *
from .web.console.registry import ConsoleRegistryMethods
from .web.console.context import ConsoleContextMethods
from .web.console.world_routes import ConsoleWorldRouteMethods
from .web.console.runtime_routes import ConsoleRuntimeRouteMethods
from .web.console.operation_routes import ConsoleOperationRouteMethods
from .web.console.system_routes import ConsoleSystemRouteMethods
from .web.console.files import ConsoleFileMethods
from .web.console.errors import ConsoleErrorMethods
from .web.console.overview_routes import ConsoleOverviewRouteMethods


class TavernWebConsole(ConsoleOverviewRouteMethods, ConsoleErrorMethods, ConsoleFileMethods, ConsoleSystemRouteMethods, ConsoleOperationRouteMethods, ConsoleRuntimeRouteMethods, ConsoleWorldRouteMethods, ConsoleContextMethods, ConsoleRegistryMethods):
    'AstrBot Web console.'
