from __future__ import annotations


PRODUCT_VALUES = ("SPOT", "PERP", "DATED")
CONTRACT_DIRECTION_VALUES = ("LINEAR", "INVERSE", "QUANTO")
EXPIRY_CYCLE_VALUES = ("W", "BW", "TW", "M", "BM", "Q", "BQ", "TQ")
STATUS_VALUES = (
    "PRELAUNCH",
    "TRADING",
    "BUY_ONLY",
    "SELL_ONLY",
    "CLOSE_ONLY",
    "API_RESTRICTED",
    "PAUSED",
    "DELISTING",
    "DELIVERING",
    "SETTLING",
    "CLOSED",
    "MISSING",
    "UNKNOWN",
)


_CONTRACT_TYPES = {
    "SPOT": "SPOT",
    "PERP": "PERP",
    "PERPETUAL": "PERP",
    "TRADIFI_PERPETUAL": "PERP",
    "PERPETUAL_DELIVERING": "PERP",
    "DATED": "DATED",
    "CURRENT_MONTH": "DATED",
    "NEXT_MONTH": "DATED",
    "CURRENT_QUARTER": "DATED",
    "NEXT_QUARTER": "DATED",
    "CQ": "DATED",
    "NQ": "DATED",
    "LINEARFUTURES": "DATED",
    "INVERSEFUTURES": "DATED",
    "DELIVERY": "DATED",
}


_STATUSES = {
    "PRELAUNCH": "PRELAUNCH",
    "PREOPEN": "PRELAUNCH",
    "PENDING_TRADING": "PRELAUNCH",
    "LISTED": "PRELAUNCH",
    "TRADING": "TRADING",
    "LIVE": "TRADING",
    "OPEN": "TRADING",
    "TRADABLE": "TRADING",
    "ENABLED": "TRADING",
    "ONLINE": "TRADING",
    "NORMAL": "TRADING",
    "BUYABLE": "BUY_ONLY",
    "SELLABLE": "SELL_ONLY",
    "LIMIT_OPEN": "CLOSE_ONLY",
    "CLOSE_ONLY": "CLOSE_ONLY",
    "RESTRICTEDAPI": "API_RESTRICTED",
    "API_RESTRICTED": "API_RESTRICTED",
    "BREAK": "PAUSED",
    "HALT": "PAUSED",
    "PAUSED": "PAUSED",
    "SUSPEND": "PAUSED",
    "UNTRADABLE": "PAUSED",
    "MAINTAIN": "PAUSED",
    "TRADING_HALT": "PAUSED",
    "DELISTING": "DELISTING",
    "PRE_DELIVERING": "DELIVERING",
    "DELIVERING": "DELIVERING",
    "DELIVERY": "DELIVERING",
    "PRE_SETTLE": "SETTLING",
    "SETTLING": "SETTLING",
    "COMPLETED": "CLOSED",
    "DELIVERED": "CLOSED",
    "CLOSE": "CLOSED",
    "CLOSED": "CLOSED",
    "OFF": "CLOSED",
    "OFFLINE": "CLOSED",
    "DELISTED": "CLOSED",
    "DISABLED": "CLOSED",
    "MISSING_FROM_COMPLETE_SNAPSHOT": "MISSING",
    "MISSING": "MISSING",
    "UNKNOWN": "UNKNOWN",
}


def normalize_contract_type(value: object, *, market_type: str = "FUTURE") -> str:
    if str(market_type).upper() == "SPOT":
        return "SPOT"
    normalized = str(value or "").strip().upper().replace("_", "")
    if normalized in {"LINEARPERPETUAL", "INVERSEPERPETUAL"}:
        return "PERP"
    try:
        return _CONTRACT_TYPES[str(value or "").strip().upper()]
    except KeyError as exc:
        raise ValueError(f"unknown contract type {value!r}") from exc


def normalize_product(market_type: str, contract_type: str) -> str:
    if str(market_type).upper() == "SPOT":
        return "SPOT"
    normalized = normalize_contract_type(contract_type, market_type=market_type)
    return "PERP" if normalized == "PERP" else "DATED"


def normalize_status(value: object) -> str:
    normalized = str(value or "UNKNOWN").strip().upper()
    return _STATUSES.get(normalized, "UNKNOWN")


def contract_direction(
    *,
    market_type: str,
    base_symbol: str,
    quote_symbol: str,
    settle_symbol: str | None,
) -> str | None:
    if str(market_type).upper() == "SPOT":
        return None
    settle = str(settle_symbol or "").upper()
    if settle == str(base_symbol).upper():
        return "INVERSE"
    if settle == str(quote_symbol).upper():
        return "LINEAR"
    return "QUANTO"


def legacy_expiry_cycle(contract_type: object) -> str | None:
    return {"CQ": "Q", "NQ": "BQ"}.get(str(contract_type or "").upper())
