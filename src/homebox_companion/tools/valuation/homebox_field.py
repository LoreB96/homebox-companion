"""The approved used value, stored in a Homebox custom field.

Homebox has no "current value" field: purchase price is what was paid and
sold price is for items already sold. The value goes in a text custom field
(``HBC_VALUATION_FIELD_NAME``, default "Valore usato") formatted as
``"120.00 EUR (2026-10-27)"``, readable in the Homebox UI and parsed back here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...core import config
from .store import Valuation

_NUMBER = re.compile(r"\d[\d.,]*")
_CURRENCY = re.compile(r"\b([A-Z]{3})\b|(€)")


@dataclass(frozen=True)
class HomeboxValue:
    value: float
    currency: str | None
    text: str


def _parse_number(raw: str) -> float | None:
    """Parse "1234.5", "1.234,50" or "1,234.50"; the last separator is the decimal one."""
    raw = raw.rstrip(".,")
    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal = "." if last_dot > last_comma else ","
        thousands = "," if decimal == "." else "."
        raw = raw.replace(thousands, "").replace(decimal, ".")
    else:
        raw = raw.replace(",", ".")
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _field_matches(field: dict[str, Any]) -> bool:
    return str(field.get("name") or "").strip().casefold() == config.settings.valuation_field_name.casefold()


def read_homebox_value(item: dict[str, Any]) -> HomeboxValue | None:
    """The used value stored in an item detail response, if any."""
    for field in item.get("fields") or []:
        if not _field_matches(field):
            continue
        text = str(field.get("textValue") or "").strip()
        number = field.get("numberValue")
        match = _NUMBER.search(text)
        value = _parse_number(match.group()) if match else None
        if value is None and isinstance(number, int | float) and number > 0:
            value = float(number)
        if value is None:
            return None
        currency_match = _CURRENCY.search(text)
        currency = None
        if currency_match:
            currency = currency_match.group(1) or "EUR"
        return HomeboxValue(value=value, currency=currency, text=text)
    return None


def format_value(valuation: Valuation) -> str:
    return f"{valuation.value:.2f} {valuation.currency} ({valuation.valued_at.date().isoformat()})"


def with_value_field(fields: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Set the value field in a PUT ``fields`` list, keeping every other field untouched.

    An existing field keeps its ``id`` so Homebox updates it instead of
    deleting and recreating it.
    """
    updated: list[dict[str, Any]] = []
    replaced = False
    for field in fields:
        if not replaced and _field_matches(field):
            updated.append({**field, "type": "text", "textValue": text})
            replaced = True
        else:
            updated.append(field)
    if not replaced:
        updated.append({"name": config.settings.valuation_field_name, "type": "text", "textValue": text})
    return updated
