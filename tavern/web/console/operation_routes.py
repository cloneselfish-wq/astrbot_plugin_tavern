from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


from .operation_queries import OperationQueriesMixin
from .operation_actions import OperationActionsMixin

class ConsoleOperationRouteMethods(OperationQueriesMixin, OperationActionsMixin):
    pass
