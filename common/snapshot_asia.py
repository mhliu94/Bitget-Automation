"""Longbridge/Webull read-only mappings for account-details and order-status v1.

Contracts: Longbridge docs/trade/asset/{account,stock,fund} and
docs/trade/order/{today_orders,order_detail}; Webull reference/{account-balance,
account-position,order-history,order-detail}. Values are broker observations,
not order-ticket estimates. Missing economically meaningful fields fail rather
than becoming a zero balance, zero fill, or a fabricated price.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import time
from urllib.parse import urlencode

from .errors import AuthenticationError, BrokerError
from .utils import iso_utc, parse_time


def _number(value, name, *, nonnegative=False, positive=False):
    if value is None or isinstance(value, bool) or value == "":
        raise BrokerError(f"Snapshot is missing a valid {name}")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (nonnegative and result < 0) or (positive and result <= 0):
            raise ValueError()
        number = int(result) if result == result.to_integral_value() else float(result)
        if not math.isfinite(number):
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, OverflowError):
        raise BrokerError(f"Snapshot has an invalid {name}") from None


def _text(value, name):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise BrokerError(f"Snapshot is missing {name}")
    return str(value).strip()


def _rows(value, name):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BrokerError(f"Unexpected {name} response")
    return value


def _currency(value):
    text = _text(value, "currency").upper()
    if len(text) != 3 or not text.isascii() or not text.isalpha():
        raise BrokerError("Snapshot has an invalid currency")
    return text


def _symbol(broker, value):
    text = _text(value, "symbol")
    if broker == "longbridge" and text.endswith(".US"):
        text = text[:-3]
    if not text or len(text) > 64 or any(character.isspace() for character in text):
        raise BrokerError("Snapshot has an invalid symbol")
    return text


def _stamp(value, name):
    try:
        if value in (None, "", "0", 0):
            raise ValueError()
        return iso_utc(parse_time(value))
    except (ValueError, TypeError, OverflowError, OSError):
        raise BrokerError(f"Snapshot has an invalid {name}") from None


def _longbridge_account(adapter):
    balance = adapter._request("get", "/v1/asset/account")
    holdings = adapter.positions()
    if not isinstance(balance, dict) or not isinstance(holdings, dict):
        raise BrokerError("Unexpected Longbridge account response")
    accounts = _rows(balance.get("list"), "Longbridge balances")
    if not accounts:
        raise BrokerError("Longbridge returned no cash balances")
    cash = {}
    cash_details = []
    for account in accounts:
        # total_cash is a consolidated value in the reporting currency. Never
        # treat multiple currency-converted totals as distinct cash holdings.
        for row in _rows(account.get("cash_infos"), "Longbridge cash details"):
            currency = _currency(row.get("currency"))
            available = _number(row.get("available_cash"), "available_cash")
            if currency in cash and cash[currency] != available:
                raise BrokerError("Longbridge returned conflicting per-currency cash balances")
            cash[currency] = available
            cash_details.append(dict(row))
    if not cash:
        raise BrokerError("Longbridge omitted per-currency available cash")
    positions = []
    for section, list_key in (("stocks", "stock_info"), ("funds", "fund_info")):
        container = holdings.get(section)
        if not isinstance(container, dict):
            raise BrokerError("Longbridge omitted a position category")
        # Both lists are optional in the broker's schema; an omitted list in
        # a successful category response is an empty account channel/list.
        for channel in _rows(container.get("list", []), "Longbridge position channels"):
            for row in _rows(channel.get(list_key, []), "Longbridge positions"):
                fund = section == "funds"
                native = _text(row.get("counter_id" if fund else "symbol"), "position symbol")
                symbol = native.rsplit("/", 1)[-1] if fund else _symbol("longbridge", native)
                item = {
                    "symbol": symbol, "broker_symbol": native,
                    "qty": _number(row.get("holding_units" if fund else "quantity"), "position quantity"),
                    "avg_price": _number(row.get("cost_net_asset_value" if fund else "cost_price"), "position cost"),
                    "currency": _currency(row.get("currency")),
                    "account_channel": channel.get("account_channel"),
                    "cost_basis": "net_asset_value" if fund else "broker_account_cost_setting",
                    "asset_class": "fund" if fund else "security",
                }
                if fund and row.get("current_net_asset_value") not in (None, ""):
                    item["marketPrice"] = _number(row["current_net_asset_value"], "fund NAV", nonnegative=True)
                    item["price_basis"] = "net_asset_value"
                    item["price_observed_at"] = row.get("net_asset_value_day")
                if not fund and row.get("available_quantity") not in (None, ""):
                    item["available_qty"] = _number(row["available_quantity"], "available position quantity")
                positions.append(item)
    return {"cash_by_currency": cash, "positions": positions,
            "cash_basis": "available_cash", "cash_details": cash_details,
            "reported_account_totals": accounts}


def _webull_account(adapter):
    account = adapter._account_id()
    balance = adapter._call(adapter._client().account_v2.get_account_balance, account_id=account)
    positions = _rows(adapter.positions(), "Webull positions")
    if not isinstance(balance, dict):
        raise BrokerError("Unexpected Webull balance response")
    cash = {}
    for row in _rows(balance.get("account_currency_assets"), "Webull currency balances"):
        currency = _currency(row.get("currency"))
        if currency in cash:
            raise BrokerError("Webull returned duplicate currency balances")
        # EVENTS_CASH may return an empty cash field. That is unavailable,
        # not a zero balance, and cannot form a numeric cash snapshot.
        cash[currency] = _number(row.get("cash_balance"), "Webull cash balance")
    if not cash:
        raise BrokerError("Webull omitted per-currency cash balances")
    mapped = []
    for row in positions:
        item = {"symbol": _symbol("webull", row.get("symbol")),
                "qty": _number(row.get("quantity"), "position quantity"),
                "avg_price": _number(row.get("cost_price"), "position cost"),
                "currency": _currency(row.get("currency")),
                "position_id": row.get("position_id"), "asset_class": row.get("instrument_type")}
        if row.get("last_price") not in (None, ""):
            item["marketPrice"] = _number(row["last_price"], "last price")
        if row.get("unrealized_profit_loss") not in (None, ""):
            item["unrealizedProfitLoss"] = _number(row["unrealized_profit_loss"], "unrealized profit/loss")
        # Do not compute market value from quantity*price: option/futures
        # contracts have multipliers and multi-leg positions have net prices.
        for field in ("option_strategy", "legs", "event_outcome"):
            if field in row:
                item[field] = row[field]
        mapped.append(item)
    return {"cash_by_currency": cash, "positions": mapped, "cash_basis": "cash_balance",
            "reported_account_totals": balance}


def collect_account(adapter, broker):
    """Collect every native position and explicit per-currency cash value."""
    if broker == "longbridge":
        return _longbridge_account(adapter)
    if broker == "webull":
        return _webull_account(adapter)
    raise BrokerError("No Asia account mapping for this broker")


_LB_STATUS = {
    **dict.fromkeys(("NotReported", "ReplacedNotReported", "ProtectedNotReported", "VarietiesNotReported",
                     "DelayedNotReported", "WaitToNew"), "PENDING_NEW"),
    **dict.fromkeys(("NewStatus", "WaitToReplace", "PendingReplaceStatus", "ReplacedStatus"), "NEW"),
    "PartialFilledStatus": "PARTIALLY_FILLED", "WaitToCancel": "PENDING_CANCEL",
    "PendingCancelStatus": "PENDING_CANCEL", "FilledStatus": "FILLED", "CanceledStatus": "CANCELED",
    "PartialWithdrawal": "CANCELED", "RejectedStatus": "REJECTED", "ExpiredStatus": "EXPIRED",
}
_WB_STATUS = {"PENDING": "PENDING_NEW", "SUBMITTED": "NEW", "CANCELLED": "CANCELED",
              "FILLED": "FILLED", "FAILED": "REJECTED", "PARTIAL_FILLED": "PARTIALLY_FILLED"}


def map_order(broker, row, observed_at):
    """Map one complete broker observation; routing/sequence belong to publisher."""
    if not isinstance(row, dict):
        raise BrokerError("Unexpected order snapshot")
    if broker == "longbridge":
        created = row.get("submitted_at")
        updated = row.get("updated_at") or observed_at
        kind = {"MO": "MARKET", "LO": "LIMIT", "ELO": "LIMIT", "SLO": "LIMIT",
                "ALO": "LIMIT"}.get(row.get("order_type"), "OTHER")
        status = _LB_STATUS.get(row.get("status"), "UNKNOWN")
        quantity, filled, average = row.get("quantity"), row.get("executed_quantity"), row.get("executed_price")
        side = {"Buy": "BUY", "Sell": "SELL"}.get(row.get("side"))
        limit = row.get("price")
    elif broker == "webull":
        created = row.get("place_time_at") or row.get("place_time")
        # Last-fill time does not timestamp later cancellation/status changes.
        # The list/detail contract has no general update timestamp.
        updated = observed_at
        kind = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP_LOSS": "STOP",
                "STOP_LOSS_LIMIT": "STOP_LIMIT", "TRAILING_STOP_LOSS": "TRAILING_STOP",
                "MARKET_ON_OPEN": "MARKET", "MARKET_ON_CLOSE": "MARKET", "LIMIT_ON_OPEN": "LIMIT"}.get(row.get("order_type"), "OTHER")
        status = _WB_STATUS.get(row.get("status"), "UNKNOWN")
        quantity, filled, average = row.get("total_quantity"), row.get("filled_quantity"), row.get("filled_price")
        side = {"BUY": "BUY", "SELL": "SELL", "SHORT": "SELL"}.get(row.get("side"))
        limit = row.get("limit_price")
    else:
        raise BrokerError("No Asia order mapping for this broker")
    if side is None:
        raise BrokerError("Order has an unsupported side")
    qty = _number(quantity, "order quantity", positive=True)
    done = _number(filled, "filled quantity", nonnegative=True)
    if done > qty or (status == "FILLED" and done != qty) or (status == "PARTIALLY_FILLED" and not 0 < done < qty):
        raise BrokerError("Order has inconsistent cumulative fills")
    price = _number(average, "average fill price", nonnegative=True) if done else None
    result = {
        "order_id": _text(row.get("order_id"), "order ID"),
        "created_at": _stamp(created, "creation time"), "updated_at": _stamp(updated, "update time"),
        "symbol": _symbol(broker, row.get("symbol")), "side": side, "order_type": kind, "status": status,
        "quantity": qty, "filled_quantity": done, "average_fill_price": price,
        "message": f"{broker} status={row.get('status')}; order_type={row.get('order_type')}; side={row.get('side')}",
    }
    if parse_time(result["updated_at"]) < parse_time(result["created_at"]):
        raise BrokerError("Order update precedes creation")
    if kind in {"LIMIT", "STOP_LIMIT"}:
        result["limit_price"] = _number(limit, "limit price", nonnegative=True)
    if kind in {"STOP", "STOP_LIMIT"}:
        result["stop_price"] = _number(row.get("stop_price"), "stop price", nonnegative=True)
    if row.get("currency"):
        result["currency"] = _currency(row["currency"])
    for field in ("client_order_id", "time_in_force"):
        if row.get(field):
            result[field] = _text(row[field], field)
    if row.get("msg"):
        result["message"] += "; " + str(row["msg"])
    result["message"] = result["message"][:2000]
    return result


def collect_orders(adapter, broker, start, end, errors=None):
    """Collect today's orders, optionally retaining healthy siblings on errors.

    The account-wide broker read must succeed before per-order isolation starts.
    Omitting ``errors`` preserves strict failure for direct callers.
    """
    if errors is not None and not isinstance(errors, list):
        raise BrokerError("Order mapping errors must be a list")
    rows = adapter.orders_today(start, end)
    if not isinstance(rows, list):
        raise BrokerError(f"Unexpected {broker} orders response")
    observed = iso_utc(datetime.now(timezone.utc))
    mapped = []
    for row in rows:
        original = row
        try:
            if not isinstance(row, dict):
                raise BrokerError("Unexpected order record")
            fill_key, price_key = ("executed_quantity", "executed_price") if broker == "longbridge" else ("filled_quantity", "filled_price")
            missing = row.get(fill_key) in (None, "")
            if not missing:
                missing = _number(row[fill_key], "filled quantity", nonnegative=True) > 0 and row.get(price_key) in (None, "")
            if missing:
                if broker == "longbridge":
                    detail = adapter._request("get", "/v1/trade/order?" + urlencode({"order_id": _text(row.get("order_id"), "order ID")}))
                elif broker == "webull":
                    client_id = _text(row.get("client_order_id"), "client order ID for detail lookup")
                    time.sleep(float(adapter.config.get("query_interval", 1.1)))
                    group = adapter._call(adapter._client().order_v3.get_order_detail,
                                          account_id=adapter._account_id(), client_order_id=client_id)
                    if not isinstance(group, dict):
                        raise BrokerError("Unexpected Webull order detail")
                    matches = [item for item in _rows(group.get("orders"), "Webull order detail")
                               if str(item.get("order_id")) == str(row.get("order_id"))]
                    if len(matches) != 1:
                        raise BrokerError("Webull detail did not identify exactly one requested order")
                    detail = matches[0]
                else:
                    raise BrokerError("No Asia order mapping for this broker")
                if not isinstance(detail, dict) or str(detail.get("order_id")) != str(row.get("order_id")):
                    raise BrokerError("Order detail does not match requested order")
                # Detail is a full new observation, not a mixture of old/new fills.
                row = detail
            mapped.append(map_order(broker, row, observed))
        except AuthenticationError:
            raise
        except Exception as exc:
            if errors is None:
                raise
            identity = original.get("order_id") if isinstance(original, dict) else None
            errors.append({"order_id": str(identity) if isinstance(identity, (str, int)) and not isinstance(identity, bool) else None,
                           "error_type": type(exc).__name__,
                           "error": str(exc) if isinstance(exc, BrokerError) else "Order snapshot mapping or detail lookup failed"})
    return mapped
