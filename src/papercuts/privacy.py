"""Deterministic caller-input privacy and scalar validation."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata
from typing import Iterable, Pattern


PATH_PREFIX = r"(^|[ \t\"'\u0060(<\[{=])"
URI_PREFIX = r"(^|[ \t\"'\u0060(<\[{=:])"

HOME_ESC = re.escape(str(Path.home().resolve()))
HOME_PATH = rf"(?P<value>(?:{HOME_ESC}|/(?:home|Users)/[A-Za-z0-9._-]+)(?:/[^\s]*)?)"
DRIVE_PATH = r"(?P<value>[A-Za-z]:[\\/][^\s]*)"
UNC_PATH = r"(?P<value>(?:\\\\|//)[^\\/\s]+[\\/][^\\/\s]+(?:[\\/][^\s]*)?)"
URI_USERINFO = r"(?P<value>[a-z][a-z0-9+.-]*://[^\s/?#@]+@[^\s/?#]+)"
PEM_HEADER = r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
GITHUB_TOKEN = r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9])"
SK_TOKEN = r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
AWS_KEY = r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"
SLACK_TOKEN = r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])"


class PrivacyError(ValueError):
    """A stable, non-disclosing caller-input validation error."""

    def __init__(self, rule_id: str, field: str | None):
        self.rule_id = rule_id
        self.field = field
        super().__init__(rule_id)

    def __str__(self) -> str:
        if self.field is None:
            return f"papercuts: {self.rule_id}"
        return f"papercuts: {self.rule_id} field={self.field}"


_SENSITIVE_RULES: tuple[tuple[str, Pattern[str]], ...] = (
    ("URI_USERINFO", re.compile(URI_PREFIX + URI_USERINFO, re.ASCII | re.IGNORECASE)),
    ("PEM_HEADER", re.compile(PEM_HEADER, re.ASCII)),
    ("GITHUB_TOKEN", re.compile(GITHUB_TOKEN, re.ASCII)),
    ("SK_TOKEN", re.compile(SK_TOKEN, re.ASCII)),
    ("AWS_KEY", re.compile(AWS_KEY, re.ASCII)),
    ("SLACK_TOKEN", re.compile(SLACK_TOKEN, re.ASCII)),
    ("HOME_PATH", re.compile(PATH_PREFIX + HOME_PATH, re.ASCII)),
    ("DRIVE_PATH", re.compile(PATH_PREFIX + DRIVE_PATH, re.ASCII)),
    ("UNC_PATH", re.compile(PATH_PREFIX + UNC_PATH, re.ASCII)),
)

_RECURRENCE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*", re.ASCII)
_FIELD_MAXIMUMS = {
    "summary": 120,
    "expected": 500,
    "observed": 500,
    "resolution": 500,
    "recurrence_key": 80,
}


def _choose_sensitive_rule(
    value: str,
    rules: Iterable[tuple[str, Pattern[str]]] = _SENSITIVE_RULES,
) -> str | None:
    """Return the earliest sensitive rule, preserving declaration order on ties."""

    selected: tuple[int, int, str] | None = None
    for order, (name, pattern) in enumerate(rules):
        match = next(pattern.finditer(value), None)
        if match is None:
            continue
        candidate = (match.start(), order, name)
        if selected is None or candidate[:2] < selected[:2]:
            selected = candidate
    return None if selected is None else selected[2]


def _reject(rule: str, field: str) -> None:
    raise PrivacyError(f"E_PRIVACY_{rule}", field)


def validate_caller_field(value: object, field: str) -> str | None:
    """Validate one named caller scalar without disclosing its value."""

    if type(field) is not str or field not in _FIELD_MAXIMUMS:
        raise PrivacyError("E_PRIVACY_FIELD", None)
    if field == "recurrence_key" and value is None:
        return None
    if type(value) is not str:
        _reject("TYPE", field)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _reject("UTF8", field)
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in value):
        _reject("CATEGORY", field)
    maximum = _FIELD_MAXIMUMS[field]
    if field == "recurrence_key":
        if len(value) > maximum:
            _reject("BOUNDS", field)
    elif not 1 <= len(value) <= maximum:
        _reject("BOUNDS", field)
    if value != value.strip():
        _reject("WHITESPACE", field)
    if field == "recurrence_key" and _RECURRENCE_RE.fullmatch(value) is None:
        _reject("RECURRENCE_KEY", field)
    sensitive_rule = _choose_sensitive_rule(value)
    if sensitive_rule is not None:
        _reject(sensitive_rule, field)
    return value


validate_caller_value = validate_caller_field
validate_scalar = validate_caller_field


__all__ = [
    "PrivacyError",
    "validate_caller_field",
    "validate_caller_value",
    "validate_scalar",
]
