from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


NORMALIZATION_VERSION = "derivative-contract-metadata-v1"


def positive_decimal(value: object) -> str | None:
    """Return one canonical positive decimal string, or None for unsafe input."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def with_contract_evidence(raw: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Attach normalized-source evidence without replacing provider payload fields."""
    result = dict(raw)
    metadata = dict(result.get("_metadata") or {})
    metadata["CONTRACT_METADATA"] = evidence
    result["_metadata"] = metadata
    return result
