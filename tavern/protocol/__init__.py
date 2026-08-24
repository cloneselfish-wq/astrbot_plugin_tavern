from .constants import (
    STANDARD_MODULES,
    THIRTEENTH_SEAT_CONTENT_VERSION,
    THIRTEENTH_SEAT_NAMESPACE,
    THIRTEENTH_SEAT_PACKAGE_ID,
    TWP_CORE_VERSION,
    TWP_PACKAGE_FORMAT,
    TWP_VERSION,
)
from .errors import TwpPackageError, TwpValidationIssue

_LAZY_EXPORTS = {
    "build_plan": (".commands", "build_plan"),
    "command_catalog": (".commands", "command_catalog"),
    "normalize_envelope": (".commands", "normalize_envelope"),
    "inspect_twp_archive": (".references", "inspect_twp_archive"),
    "project_runtime": (".projections", "project_runtime"),
    "flatten_runtime": (".runtime", "flatten_runtime"),
    "hydrate_runtime": (".runtime", "hydrate_runtime"),
    "runtime_from_state": (".runtime", "runtime_from_state"),
    "store_runtime": (".runtime", "store_runtime"),
    "TwpPackageService": (".service", "TwpPackageService"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    from importlib import import_module

    value = getattr(import_module(target[0], __name__), target[1])
    globals()[name] = value
    return value

__all__ = [
    "STANDARD_MODULES",
    "THIRTEENTH_SEAT_CONTENT_VERSION",
    "THIRTEENTH_SEAT_NAMESPACE",
    "THIRTEENTH_SEAT_PACKAGE_ID",
    "TWP_CORE_VERSION",
    "TWP_PACKAGE_FORMAT",
    "TWP_VERSION",
    "TwpPackageError",
    "TwpPackageService",
    "TwpValidationIssue",
    "build_plan",
    "command_catalog",
    "flatten_runtime",
    "hydrate_runtime",
    "inspect_twp_archive",
    "normalize_envelope",
    "project_runtime",
    "runtime_from_state",
    "store_runtime",
]
