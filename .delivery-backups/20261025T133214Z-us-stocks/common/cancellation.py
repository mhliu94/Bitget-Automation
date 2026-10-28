"""Account-scoped cancellation requests. No write is retried or called a fill result.

The caller owns durable command claiming and explicit execution authorization.
Open-order discovery has no creation-date filter; it is deliberately separate
from orders_today(). Acknowledgement is not final cancellation confirmation.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote, urlencode

from .errors import BrokerError, CommandError, OutcomeUnknown


LONG_OPEN = {
    "NotReported", "ReplacedNotReported", "ProtectedNotReported", "VarietiesNotReported",
    "WaitToNew", "NewStatus", "WaitToReplace", "PendingReplaceStatus", "ReplacedStatus",
    "PartialFilledStatus",
}
LONG_CLOSED = {"FilledStatus", "RejectedStatus", "CanceledStatus", "ExpiredStatus", "PartialWithdrawal"}
LONG_PENDING = {"WaitToCancel", "PendingCancelStatus"}
TS_OPEN = {
    "ACK", "ASS", "BRO", "CHG", "CND", "COR", "DIS", "DON", "EXE", "FPR", "LAT",
    "OPN", "OSO", "PLA", "RJC", "RPD", "RSN", "STP", "STT", "SUS",
}
TS_CLOSED = {"BRC", "BRF", "DOA", "CAN", "EXP", "OUT", "RJR", "SCN", "TSC", "UCH", "REJ", "FLL", "FLP"}
TS_PENDING = {"CSN", "UCN", "ECN", "REC"}


def _id(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256 or any(
        char.isspace() or ord(char) < 32 or ord(char) >= 127 for char in value
    ):
        raise CommandError("Cancellation requires a nonempty string order_id without whitespace")
    return value


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BrokerError(f"Invalid {label} order list; no cancellations sent")
    return value


def _is_open(row, field, opened, closed, pending=()):
    state = row.get(field)
    if state in opened:
        return True
    if state in closed or state in pending:
        return False
    raise BrokerError(f"Unrecognized cancellation state {state!r}; no cancellations sent")


def _unique(rows, key):
    unique = {}
    for row in rows:
        identity = _id(row.get(key))
        if identity in unique and unique[identity] != row:
            raise BrokerError("Conflicting duplicate open orders; no cancellations sent")
        unique[identity] = row
    return list(unique.values())


def _result(broker, requests, scope=None):
    states = {row["status"] for row in requests}
    status = ("outcome_unknown" if "outcome_unknown" in states else
              "cancel_rejected" if "cancel_rejected" in states else
              "cancel_requested" if requests else "no_action")
    return {"broker": broker, "status": status, "cancellation_confirmed": False,
            "requests": requests,
            "cancellations_requested": sum(row["status"] == "cancel_requested" for row in requests),
            "scope": scope or "Configured account; open orders from all creation dates",
            "message": "Requests are acknowledgements only. Read order status to establish the final result."}


def _requests(broker, rows, send):
    """Retain earlier results on a partial failure; never resend an uncertain write."""
    results = []
    for row in rows:
        identity = row["order_id"]
        try:
            result = send(row)
            results.append({"order_id": identity, "status": "cancel_requested", "raw": result})
        except OutcomeUnknown as exc:
            results.append({"order_id": identity, "status": "outcome_unknown", "error": str(exc)})
            break
        except BrokerError as exc:
            results.append({"order_id": identity, "status": "cancel_rejected", "error": str(exc)})
            break
    response = _result(broker, results)
    response["not_requested_order_ids"] = [row["order_id"] for row in rows[len(results):]]
    return response


def _rh_orders(adapter):
    account = adapter._account_params()["account_number"]
    rows = []
    for state in ("open", "pending"):
        rows.extend(adapter._pages(adapter.prefix + "orders/", {"account_number": account, "state": state}))
    for row in _rows(rows, "Robinhood"):
        if row.get("account_number") != account:
            raise BrokerError("Robinhood cancellation read returned another account")
    rows = _unique(rows, "id")
    return [{**row, "order_id": row["id"]} for row in rows
            if _is_open(row, "state", {"open", "pending"}, {"filled", "canceled", "failed"})]


def _ts_orders(adapter):
    accounts = adapter._accounts()
    rows = []
    for offset in range(0, len(accounts), 10):
        segment = ",".join(quote(account, safe="") for account in accounts[offset:offset + 10])
        rows.extend(adapter._orders(f"/brokerage/accounts/{segment}/orders", {"status": "Open"}))
    for row in _rows(rows, "TradeStation"):
        if row.get("AccountID") not in accounts:
            raise BrokerError("TradeStation cancellation read returned another account")
    return [{**row, "order_id": row["OrderID"]} for row in _unique(rows, "OrderID")
            if _is_open(row, "Status", TS_OPEN, TS_CLOSED, TS_PENDING)]


def _long_orders(adapter):
    # Today is not documented to include older GTC orders. Explicit epoch-to-now
    # history filters avoid the endpoint's implicit 90-day lookback. has_more and
    # the 1000-record ceiling require interval subdivision, before any writes.
    calls = 0
    def history(lower, upper):
        nonlocal calls
        calls += 1
        if calls > int(adapter.config.get("max_pages", 10000)):
            raise BrokerError("Longbridge cancellation discovery limit reached; no cancellations sent")
        params = [("start_at", str(lower)), ("end_at", str(upper))]
        params += [("status", state) for state in sorted(LONG_OPEN)]
        response = adapter._request("get", "/v1/trade/order/history?" + urlencode(params))
        if not isinstance(response, dict) or type(response.get("has_more", False)) is not bool:
            raise BrokerError("Invalid Longbridge cancellation history response")
        rows = _rows(response.get("orders"), "Longbridge")
        if response.get("has_more") or len(rows) >= 1000:
            if upper - lower <= 1:
                raise BrokerError("Longbridge open orders exceed the API ceiling in one second; no cancellations sent")
            midpoint = (lower + upper) // 2
            return history(lower, midpoint) + history(midpoint, upper)
        return rows
    rows = history(0, int(time.time()))
    today = adapter._request("get", "/v1/trade/order/today")
    if not isinstance(today, dict) or today.get("has_more"):
        raise BrokerError("Incomplete Longbridge current-order response")
    # Today's state is newer and overrides a historical state of the same ID.
    by_id = {_id(row.get("order_id")): row for row in rows}
    for row in _rows(today.get("orders"), "Longbridge"):
        by_id[_id(row.get("order_id"))] = row
    return [row for row in by_id.values()
            if _is_open(row, "status", LONG_OPEN, LONG_CLOSED, LONG_PENDING)]


def _webull_orders(adapter):
    account = adapter._account_id()
    groups = list(adapter._pages(adapter._client().order_v3.list_order_open, account_id=account))
    rows = []
    for group in _rows(groups, "Webull"):
        if group.get("account_id", account) != account:
            raise BrokerError("Webull cancellation read returned another account")
        for row in _rows(group.get("orders"), "Webull"):
            if row.get("account_id", account) != account:
                raise BrokerError("Webull cancellation read returned another account")
            if _is_open(row, "status", {"PENDING", "SUBMITTED", "PARTIAL_FILLED", "PARTIAL FILLED"},
                        {"FILLED", "CANCELLED", "FAILED", "EXPIRED", "REJECTED"}, {"PENDING_CANCEL"}):
                # SDK v3 accepts client_order_id, never the broker order_id.
                _id(row.get("client_order_id"))
                rows.append({**row, "order_id": row.get("order_id") or row["client_order_id"]})
    return _unique(rows, "order_id")


def _bitget_orders(adapter, command=None):
    rows = []
    for kind in ("normal", "tpsl"):
        params = {"tpslType": kind}
        if command:
            params.update(symbol=command["symbol"], orderId=command["order_id"])
        batch = list(adapter._order_pages("/api/v2/spot/trade/unfilled-orders", params))
        for row in _rows(batch, "Bitget"):
            if _is_open(row, "status", {"live", "partially_filled"}, {"filled", "cancelled"}):
                symbol = row.get("symbol")
                if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+", symbol):
                    raise BrokerError("Bitget open order lacks its symbol")
                rows.append({**row, "order_id": _id(row.get("orderId")), "tpslType": kind})
    return _unique(rows, "order_id")


def _send(adapter, broker, row):
    identity = row["order_id"]
    if broker == "robinhood":
        result = adapter._request("POST", adapter.prefix + "orders/" + quote(identity, safe="") + "/cancel/")
        if not isinstance(result, dict) or result.get("id") != identity:
            raise OutcomeUnknown("Robinhood cancellation acknowledgement omitted the matching order ID")
        return result
    if broker == "tradestation":
        result = adapter._request("DELETE", "/orderexecution/orders/" + quote(identity, safe=""))
        if result.get("OrderID") != identity:
            raise OutcomeUnknown("TradeStation cancellation acknowledgement omitted the matching order ID")
        return result
    if broker == "longbridge":
        result = adapter._request("delete", "/v1/trade/order?" + urlencode({"order_id": identity}))
        if result not in ({}, None):
            raise OutcomeUnknown("Unexpected Longbridge cancellation acknowledgement")
        return result
    if broker == "webull":
        result = adapter._call(adapter._client().order_v3.cancel_order, mutation=True,
                               account_id=adapter._account_id(), client_order_id=row["client_order_id"])
        if isinstance(result, dict) and (result.get("error") or result.get("error_code")):
            raise BrokerError("Webull cancellation returned a broker error")
        if not isinstance(result, dict) or not (
            result.get("client_order_id") == row["client_order_id"] or result.get("order_id") == identity
        ):
            raise OutcomeUnknown("Webull cancellation acknowledgement omitted the matching order ID")
        return result
    if broker == "bitget":
        result = adapter._request("POST", "/api/v2/spot/trade/cancel-order",
                                  payload={"symbol": row["symbol"], "orderId": identity,
                                           "tpslType": row["tpslType"]})
        if not isinstance(result, dict) or result.get("orderId") != identity:
            raise OutcomeUnknown("Bitget cancellation acknowledgement omitted the matching order ID")
        return result
    raise CommandError("Unknown cancellation broker")


def _bitget_bulk(adapter, rows):
    """Native batches of exact normal-order IDs; TP/SL uses its typed endpoint."""
    normal = [row for row in rows if row["tpslType"] == "normal"]
    conditional = [row for row in rows if row["tpslType"] == "tpsl"]
    results = []
    for offset in range(0, len(normal), 50):
        batch = normal[offset:offset + 50]
        expected = {row["order_id"] for row in batch}
        try:
            result = adapter._request("POST", "/api/v2/spot/trade/batch-cancel-order", payload={
                "batchMode": "multiple", "orderList": [
                    {"symbol": row["symbol"], "orderId": row["order_id"]} for row in batch]})
            if not isinstance(result, dict):
                raise OutcomeUnknown("Invalid Bitget batch cancellation acknowledgement")
            success, failure = result.get("successList"), result.get("failureList")
            if not isinstance(success, list) or not isinstance(failure, list) or any(
                not isinstance(row, dict) for row in success + failure
            ):
                raise OutcomeUnknown("Bitget batch acknowledgement omitted per-order outcomes")
            actual = [row.get("orderId") for row in success + failure]
            if any(not isinstance(value, str) for value in actual) or len(actual) != len(expected) or set(actual) != expected:
                raise OutcomeUnknown("Bitget batch acknowledgement has missing or conflicting order IDs")
            outcomes = [{"order_id": row["orderId"], "status": state, "raw": row}
                        for items, state in ((success, "cancel_requested"), (failure, "cancel_rejected"))
                        for row in items]
            results.extend(outcomes)
            if failure:
                break
        except (OutcomeUnknown, BrokerError) as exc:
            state = "outcome_unknown" if isinstance(exc, OutcomeUnknown) else "cancel_rejected"
            results.extend({"order_id": row["order_id"], "status": state, "error": str(exc)} for row in batch)
            break
    if all(row["status"] == "cancel_requested" for row in results):
        results.extend(_requests("bitget", conditional, lambda row: _send(adapter, "bitget", row))["requests"])
    response = _result("bitget", results)
    attempted = {row["order_id"] for row in results}
    response["not_requested_order_ids"] = [row["order_id"] for row in rows if row["order_id"] not in attempted]
    return response


def cancel_order(adapter, broker, command):
    """Cancel a specific broker order ID (Webull also accepts client_order_id)."""
    identity = _id(command.get("order_id"))
    if broker == "zhuorui":
        return _zhuorui(adapter, "cancel_order", command)
    if broker == "alpaca":
        path = "/v2/orders/" + quote(identity, safe="")
        row = adapter.http.request("GET", path)
        if not isinstance(row, dict) or row.get("id") != identity:
            raise BrokerError("Alpaca cancellation read omitted the matching order ID")
        opened = {"new", "partially_filled", "accepted", "pending_new", "accepted_for_bidding", "stopped", "suspended", "held", "pending_replace"}
        closed = {"filled", "canceled", "expired", "replaced", "rejected", "done_for_day", "calculated"}
        if not _is_open(row, "status", opened, closed, {"pending_cancel"}):
            return _result(broker, [])
        result = adapter.http.request("DELETE", path)
        return _result(broker, [{"order_id": identity, "status": "cancel_requested", "raw": result}])
    if broker == "bitget":
        symbol = command.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+", symbol):
            raise CommandError("Bitget cancellation requires the exact pair symbol, for example BTCUSDT")
        rows = _bitget_orders(adapter, command)
        rows = [row for row in rows if row["symbol"] == symbol]
    elif broker == "robinhood":
        account = adapter._account_params()["account_number"]
        row = adapter._request("GET", adapter.prefix + "orders/" + quote(identity, safe="") + "/",
                               {"account_number": account})
        if not isinstance(row, dict) or row.get("id") != identity or row.get("account_number") != account:
            raise BrokerError("Robinhood order does not match the configured account and ID")
        rows = [{**row, "order_id": identity}] if _is_open(row, "state", {"open", "pending"}, {"filled", "canceled", "failed"}) else []
    elif broker == "tradestation":
        rows = _ts_orders(adapter)
    elif broker == "longbridge":
        row = adapter._request("get", "/v1/trade/order?" + urlencode({"order_id": identity}))
        if not isinstance(row, dict) or row.get("order_id") != identity:
            raise BrokerError("Longbridge cancellation read omitted matching order ID")
        rows = [row] if _is_open(row, "status", LONG_OPEN, LONG_CLOSED, LONG_PENDING) else []
    elif broker == "webull":
        rows = _webull_orders(adapter)
    else:
        raise CommandError("Unknown cancellation broker")
    rows = [row for row in rows if identity in {row["order_id"], row.get("client_order_id")}]
    if len(rows) > 1:
        raise BrokerError("Cancellation ID ambiguously matches multiple orders")
    return _requests(broker, rows, lambda row: _send(adapter, broker, row))


def _zhuorui(adapter, operation, command):
    if not command or not command.get("id"):
        raise CommandError("Zhuorui cancellation requires its durable command ID")
    result = adapter._bridge(operation, {"command_id": command["id"], "order_id": command.get("order_id")})
    if not isinstance(result, dict):
        raise OutcomeUnknown("Zhuorui cancellation response is not an object")
    if result.get("status") in {"unknown", "cancel_unknown"}:
        raise OutcomeUnknown(result.get("message", "Zhuorui cancellation outcome unknown"))
    if result.get("status") in {"rejected", "blocked", "disabled", "cancel_rejected"}:
        raise BrokerError(result.get("message", "Zhuorui cancellation not accepted"))
    if result.get("status") not in {"cancel_requested", "no_action", "duplicate"}:
        raise OutcomeUnknown("Zhuorui cancellation returned an unrecognized status")
    if result.get("status") == "duplicate":
        # The upstream journal did not resend this command. An older unknown or
        # rejected outcome cannot become an acknowledgement in the new runner.
        previous = result.get("previous_state")
        if previous == "cancel_requested":
            result = {**result, "status": "cancel_requested", "duplicate": True}
        elif previous == "no_action":
            result = {**result, "status": "no_action", "duplicate": True}
        elif previous in {"rejected", "blocked", "disabled"}:
            raise BrokerError("Previously recorded Zhuorui cancellation was not accepted")
        else:
            raise OutcomeUnknown("Previously recorded Zhuorui cancellation requires reconciliation")
    return {**result, "broker": "zhuorui", "cancellation_confirmed": False,
            "scope": "Configured account's US orders returned by the upstream current-orders endpoint"}


def cancel_open_orders(adapter, broker, command=None):
    """Request cancellation of open orders, retaining each request's outcome."""
    if broker == "zhuorui":
        return _zhuorui(adapter, "cancel_open_orders", command)
    if broker == "alpaca":
        result = adapter.http.request("DELETE", "/v2/orders")
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise OutcomeUnknown("Invalid Alpaca cancel-all acknowledgement; reconcile before retrying")
        rows = result
        requests = []
        for row in rows:
            status = row.get("status")
            if type(status) is not int or not row.get("id"):
                raise OutcomeUnknown("Alpaca cancel-all acknowledgement omitted per-order results")
            requests.append({"order_id": row["id"], "status": "cancel_requested" if 200 <= status < 300 else
                             "outcome_unknown" if status >= 500 else "cancel_rejected", "raw": row})
        return _result(broker, requests)
    loaders = {"robinhood": _rh_orders, "tradestation": _ts_orders,
               "longbridge": _long_orders, "webull": _webull_orders, "bitget": _bitget_orders}
    if broker not in loaders:
        raise CommandError("Unknown cancellation broker")
    rows = loaders[broker](adapter)
    if broker == "bitget":
        return _bitget_bulk(adapter, rows)
    return _requests(broker, rows, lambda row: _send(adapter, broker, row))
