from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from common.errors import BrokerError
from common.registry import adapter_for
from common.snapshot_asia import collect_orders as collect_asia_orders
from common.snapshots import collect_orders
from zhuorui.bridge_worker import collect_order_snapshots


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 10, 25, 18, tzinfo=timezone.utc)


def alpaca_order(identity="healthy", **changes):
    return {"id": identity, "symbol": "AAPL", "submitted_at": "2026-10-25T12:00:00Z",
            "updated_at": "2026-10-25T13:00:00Z", "type": "limit", "side": "buy", "status": "new",
            "qty": "2", "filled_qty": "0", "filled_avg_price": None, "limit_price": "100", **changes}


def lb_order(identity="healthy", **changes):
    return {"order_id": identity, "symbol": "AAPL.US", "submitted_at": "1792929600",
            "updated_at": "1792933200", "order_type": "LO", "side": "Buy", "status": "NewStatus",
            "quantity": "2", "executed_quantity": "0", "executed_price": None, "price": "100", **changes}


def test_alpaca_bad_order_does_not_block_healthy_manual_order_and_strict_still_raises():
    adapter = Mock()
    adapter.orders_today.return_value = [alpaca_order("bad", filled_qty=None), alpaca_order()]
    errors = []
    result = collect_orders(adapter, "alpaca", START, END, now=OBSERVED, errors=errors)
    assert [row["order_id"] for row in result] == ["healthy"]
    assert errors[0]["order_id"] == "bad"
    assert errors[0]["error_type"] == "BrokerError"
    with pytest.raises(BrokerError):
        collect_orders(adapter, "alpaca", START, END, now=OBSERVED)


@pytest.mark.parametrize("broker", ["alpaca", "longbridge", "webull"])
def test_top_level_read_failure_stays_failure_and_does_not_become_empty(broker):
    adapter = Mock()
    adapter.orders_today.side_effect = BrokerError("Account-wide read failed")
    errors = []
    with pytest.raises(BrokerError, match="Account-wide"):
        collect_orders(adapter, broker, START, END, errors=errors)
    assert errors == []


def test_longbridge_failed_detail_and_malformed_record_do_not_hide_valid_sibling():
    adapter = adapter_for("longbridge", {})
    adapter.orders_today = Mock(return_value=[lb_order("bad-detail", executed_quantity="1"), None, lb_order()])
    adapter._request = Mock(side_effect=RuntimeError("private-token-must-not-appear"))
    errors = []
    result = collect_asia_orders(adapter, "longbridge", START, END, errors=errors)
    assert [row["order_id"] for row in result] == ["healthy"]
    assert [row["order_id"] for row in errors] == ["bad-detail", None]
    assert "private-token" not in str(errors)
    with pytest.raises(RuntimeError):
        collect_asia_orders(adapter, "longbridge", START, END)


def test_bitget_stock_mapping_error_isolated_but_complete_external_order_mapped():
    adapter=adapter_for("bitget",{})
    valid={"orderId":"2","currency":"USD","orderType":"LO","symbol":"AAPL.US","side":"Sell","status":"New","quantity":"2","executedQuantity":"0","executedPrice":None,"price":"100","submittedAt":"2026-10-25T12:00:00Z"}
    adapter.orders_today=Mock(return_value=[{**valid,"orderId":"1","executedQuantity":None},valid])
    errors=[]
    result=collect_orders(adapter,"bitget",START,END,now=OBSERVED,errors=errors)
    assert [row["order_id"] for row in result]==["2"]
    assert errors[0]["order_id"]=="1"


def zhuorui_client():
    rows = [{"code": "AAPL", "ts": "US", "orderTxnReference": identity, "entrustTime": "1792929600000"}
            for identity in ("bad-map", "bad-detail", "healthy")]
    client = Mock()
    client.query.return_value = {"code": "000000", "data": rows}
    client.query_orders_for_date.return_value = {"code": "000000", "data": rows}
    client.query_order_detail.side_effect = RuntimeError("private-session-value")
    return client


def bridge_mapper(config, response, now):
    row = response["data"][0]
    identity = row["orderTxnReference"]
    if identity == "bad-map":
        raise ValueError("private-unexpected-value")
    return [{"order_id": identity, "filled_quantity": 1 if identity == "bad-detail" else 0,
             "average_fill_price": None}]


def test_zhuorui_bridge_isolates_mapper_and_detail_failure_without_exposing_values():
    payload = {"start": START.isoformat(), "end": END.isoformat(), "report_errors": True}
    result = collect_order_snapshots({}, zhuorui_client(), payload, bridge_mapper, Mock(), now=OBSERVED)
    assert [row["order_id"] for row in result["orders"]] == ["healthy"]
    assert {row["order_id"] for row in result["errors"]} == {"bad-map", "bad-detail"}
    assert "private-" not in str(result)
    del payload["report_errors"]
    with pytest.raises((ValueError, RuntimeError)):
        collect_order_snapshots({}, zhuorui_client(), payload, bridge_mapper, Mock(), now=OBSERVED)


def test_zhuorui_failed_list_query_is_not_swallowed_by_record_isolation():
    client = zhuorui_client()
    client.query.side_effect = RuntimeError("account query failed")
    with pytest.raises(RuntimeError, match="account query failed"):
        collect_order_snapshots({}, client, {"start": START.isoformat(), "end": END.isoformat(), "report_errors": True},
                                bridge_mapper, Mock(), now=OBSERVED)


def test_outer_zhuorui_collector_retains_bridge_errors_and_isolates_numeric_mapping():
    adapter = Mock(config={})
    bridge_error = {"order_id": "upstream-bad", "error_type": "ValueError", "error": "Snapshot mapping failed"}
    adapter._bridge.return_value = {"orders": [{"order_id": "bad-number", "quantity": "NaN"},
                                               {"order_id": "healthy", "quantity": "2", "filled_quantity": "0", "average_fill_price": None}],
                                    "errors": [bridge_error]}
    errors = []
    rows = collect_orders(adapter, "zhuorui", START, END, errors=errors)
    assert [row["order_id"] for row in rows] == ["healthy"]
    assert rows[0]["quantity"] == 2
    assert [row["order_id"] for row in errors] == ["upstream-bad", "bad-number"]
    assert adapter._bridge.call_args.args[1]["report_errors"] is True


def test_outer_zhuorui_direct_collection_keeps_legacy_bridge_shape():
    adapter = Mock(config={})
    adapter._bridge.return_value = [{"order_id": "healthy", "quantity": "2", "filled_quantity": "0", "average_fill_price": None}]
    rows = collect_orders(adapter, "zhuorui", START, END)
    assert rows[0]["quantity"] == 2
    assert "report_errors" not in adapter._bridge.call_args.args[1]
