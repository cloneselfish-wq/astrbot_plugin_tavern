from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


from .registry_routes import RegistryRoutesMixin
from .registry_assets import RegistryAssetsMixin

class ConsoleRegistryMethods(RegistryRoutesMixin, RegistryAssetsMixin):


    _RUNTIME_CACHE_TTL_SECONDS = 600.0















