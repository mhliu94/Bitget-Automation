"""US stock scope guards. Ticker syntax is not proof of instrument eligibility."""
import re

from common.errors import UnsupportedOrder


US_EQUITY_ALIASES = {"equity", "equities", "stock", "stocks", "us_stock", "us_stocks", "us_equity"}


def validate_asset(value=None):
    if value is None:
        return "us_equity"
    if not isinstance(value, str) or value.strip().lower() not in US_EQUITY_ALIASES:
        raise UnsupportedOrder("Only US stocks (asset_class=us_equity) are supported")
    return "us_equity"


def us_stock_symbol(value, *, qualified=False):
    """Normalize ticker notation; actual broker metadata establishes eligibility."""
    if not isinstance(value, str):
        raise UnsupportedOrder("A US stock ticker is required")
    symbol = value.strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[:-3]
    elif symbol.endswith((".HK", ".SG", ".SH", ".SS", ".SZ", ".JP", ".T", ".TO", ".L", ".AX")):
        raise UnsupportedOrder("Only US stock tickers are supported")
    if not re.fullmatch(r"[A-Z]{1,6}(?:[.-][A-Z]{1,2})?", symbol):
        raise UnsupportedOrder("A US stock ticker is required; derivative and crypto-pair symbols are unsupported")
    return symbol + ".US" if qualified else symbol


stock_symbol = us_stock_symbol


def require_us_equity(command, config=None):
    """Validate explicit asset and market selectors even in direct adapter calls."""
    for source in (command, config or {}):
        validate_asset(source.get("asset_class"))
        for key in ("market", "country", "country_code"):
            if source.get(key) is not None and str(source[key]).strip().upper() not in {"US", "USA", "UNITED STATES"}:
                raise UnsupportedOrder("Only the US stock market is supported")
    return "us_equity"
