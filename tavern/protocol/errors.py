from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TwpValidationIssue:
    code: str
    message: str
    path: str = ""
    severity: str = "error"
    hint: str = ""

    def export(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
            "hint": self.hint,
        }


class TwpPackageError(ValueError):
    def __init__(self, issue: TwpValidationIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue
        self.issues = (issue,)

__all__ = [name for name in globals() if not name.startswith('__')]

