"""Fixtures use public Longbridge/Webull field contracts, never account data."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from common.errors import BrokerError
from common.registry import adapter_for
from common.snapshot_asia import collect_account, collect_orders, map_order


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)
OBSERVED = "2026-10-25T16:00:00Z"


def lb_order(**changes):
    return {**{"order_id": "828940451093708800", "status": "PartialWithdrawal", "quantity": "10",
               "executed_quantity": "3", "executed_price": "164.66", "price": "200",
               "submitted_at": "1792929600", "updated_at": "1792933200", "side": "Buy",
               "symbol": "AAPL.US", "order_type": "LO", "time_in_force": "Day", "currency": "USD"}, **changes}


def wb_order(**changes):
    return {**{"order_id": "0352U72LQI6DT0KF41GK000000", "client_order_id": "client-1", "status": "PARTIAL_FILLED",
               "total_quantity": "10", "filled_quantity": "3", "filled_price": "164.66", "limit_price": "200",
               "place_time_at": "2026-10-25T12:00:00Z", "filled_time_at": "2026-10-25T13:00:00Z",
               "side": "BUY", "symbol": "AAPL", "order_type": "LIMIT", "time_in_force": "DAY"}, **changes}


def response(value):
    result = Mock(status_code=200)
    result.json.return_value = value
    return result


def lb_account():
    adapter = adapter_for("longbridge", {})
    adapter._request = Mock(return_value={"list": [{"currency": "HKD", "total_cash": "8000", "cash_infos": [
        {"currency": "USD", "available_cash": "100", "frozen_cash": "20", "settling_cash": "-5"},
        {"currency": "HKD", "available_cash": "0", "frozen_cash": "0", "settling_cash": "0"}]}]})
    adapter.positions = Mock(return_value={"stocks": {"list": [{"account_channel": "lb", "stock_info": [
        {"symbol": "AAPL.US", "quantity": "2", "cost_price": "101.25", "currency": "USD"},
        {"symbol": "700.HK", "quantity": "-100", "cost_price": "300", "currency": "HKD", "available_quantity": "-100"}]}]},
        "funds": {"list": [{"account_channel": "lb", "fund_info": [{"counter_id": "UT/FD/HK0000447943",
            "holding_units": "5.123", "cost_net_asset_value": "2.1", "current_net_asset_value": "2.2",
            "currency": "USD", "net_asset_value_day": "1792886400"}]}]}})
    return adapter


def test_longbridge_preserves_cash_basis_stocks_funds_channels_and_short_quantity():
    adapter = lb_account()
    result = collect_account(adapter, "longbridge")
    assert result["cash_by_currency"] == {"USD": 100, "HKD": 0}
    assert result["cash_basis"] == "available_cash"
    assert result["reported_account_totals"][0]["total_cash"] == "8000"
    assert [row["symbol"] for row in result["positions"]] == ["AAPL", "700.HK", "HK0000447943"]
    assert result["positions"][1]["qty"] == -100
    assert result["positions"][1]["available_qty"] == -100
    assert "available_quantity" not in result["positions"][1]
    assert result["positions"][2]["marketPrice"] == 2.2
    assert "marketPrice" not in result["positions"][0]
    adapter._request.assert_called_once_with("get", "/v1/asset/account")


def test_missing_cash_or_position_cost_cannot_become_zero():
    adapter = lb_account()
    del adapter._request.return_value["list"][0]["cash_infos"][0]["available_cash"]
    with pytest.raises(BrokerError, match="available_cash"):
        collect_account(adapter, "longbridge")
    adapter = lb_account()
    del adapter.positions.return_value["stocks"]["list"][0]["stock_info"][0]["cost_price"]
    with pytest.raises(BrokerError, match="position cost"):
        collect_account(adapter, "longbridge")


def test_longbridge_repeated_consolidated_views_do_not_double_cash():
    adapter = lb_account()
    adapter._request.return_value["list"].append(copy.deepcopy(adapter._request.return_value["list"][0]))
    assert collect_account(adapter, "longbridge")["cash_by_currency"]["USD"] == 100
    adapter._request.return_value["list"][1]["cash_infos"][0]["available_cash"] = "101"
    with pytest.raises(BrokerError, match="conflicting"):
        collect_account(adapter, "longbridge")


def test_webull_account_uses_bare_positions_list_and_cash_balance_not_buying_power():
    adapter = adapter_for("webull", {"account_id": "account"})
    balance = Mock(return_value=response({"account_currency_assets": [{"currency": "USD", "cash_balance": "42.5", "buying_power": "1000"}]}))
    position = Mock(return_value=response([{"symbol": "AAPL", "quantity": "-2", "cost_price": "100", "last_price": "90",
                                           "currency": "USD", "instrument_type": "EQUITY", "unrealized_profit_loss": "20"}]))
    adapter._trade = SimpleNamespace(account_v2=SimpleNamespace(get_account_balance=balance, get_account_position=position))
    result = collect_account(adapter, "webull")
    assert result["cash_by_currency"] == {"USD": 42.5}
    assert result["positions"][0]["qty"] == -2
    assert result["positions"][0]["unrealizedProfitLoss"] == 20
    assert "marketValue" not in result["positions"][0]
    balance.assert_called_once_with(account_id="account")
    position.assert_called_once_with(account_id="account")


def test_webull_events_unavailable_cash_does_not_publish_zero():
    adapter = adapter_for("webull", {"account_id": "account"})
    adapter._trade = SimpleNamespace(account_v2=SimpleNamespace(
        get_account_balance=Mock(return_value=response({"account_currency_assets": [{"currency": "USD", "cash_balance": ""}]})),
        get_account_position=Mock(return_value=response([]))))
    with pytest.raises(BrokerError, match="cash balance"):
        collect_account(adapter, "webull")


def test_longbridge_partial_cancellation_uses_execution_price_not_limit():
    result = map_order("longbridge", lb_order(), OBSERVED)
    assert result["order_id"] == "828940451093708800"
    assert result["symbol"] == "AAPL"
    assert result["status"] == "CANCELED"
    assert result["filled_quantity"] == 3
    assert result["average_fill_price"] == 164.66
    assert result["limit_price"] == 200


def test_webull_cancellation_timestamp_is_observation_not_last_fill():
    result = map_order("webull", wb_order(status="CANCELLED", side="SHORT"), OBSERVED)
    assert result["status"] == "CANCELED"
    assert result["side"] == "SELL"
    assert result["updated_at"] == OBSERVED
    assert result["filled_quantity"] == 3


@pytest.mark.parametrize("changes", [{"filled_quantity": None}, {"filled_price": None}, {"filled_quantity": "11"},
                                     {"status": "FILLED"}, {"total_quantity": 0}, {"filled_quantity": True}])
def test_invalid_webull_economic_data_is_not_fabricated(changes):
    with pytest.raises(BrokerError):
        map_order("webull", wb_order(**changes), OBSERVED)


def test_unfilled_prices_stay_null_and_external_stop_orders_are_mapped():
    result = map_order("webull", wb_order(status="SUBMITTED", filled_quantity="0", filled_price=None,
                                          order_type="STOP_LOSS_LIMIT", stop_price="190"), OBSERVED)
    assert result["average_fill_price"] is None
    assert result["order_type"] == "STOP_LIMIT"
    assert result["stop_price"] == 190


def test_webull_missing_fill_uses_matching_full_detail_without_stale_merge():
    adapter = adapter_for("webull", {"account_id": "account", "query_interval": 0})
    adapter.orders_today = Mock(return_value=[wb_order(filled_quantity=None)])
    detail = Mock(return_value=response({"orders": [wb_order(status="FILLED", filled_quantity="10", filled_price="165")]}))
    adapter._trade = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=detail))
    with patch("common.snapshot_asia.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 25, 16, tzinfo=timezone.utc)
        result = collect_orders(adapter, "webull", START, END)
    assert result[0]["status"] == "FILLED"
    assert result[0]["filled_quantity"] == 10
    assert result[0]["average_fill_price"] == 165
    detail.assert_called_once_with(account_id="account", client_order_id="client-1")


def test_longbridge_detail_is_read_only_and_order_id_must_match():
    adapter = adapter_for("longbridge", {})
    adapter.orders_today = Mock(return_value=[lb_order(executed_price=None)])
    adapter._request = Mock(return_value=lb_order())
    assert collect_orders(adapter, "longbridge", START, END)[0]["average_fill_price"] == 164.66
    adapter._request.assert_called_once_with("get", "/v1/trade/order?order_id=828940451093708800")
    adapter._request.return_value = lb_order(order_id="wrong")
    with pytest.raises(BrokerError, match="does not match"):
        collect_orders(adapter, "longbridge", START, END)


def test_webull_sdk_builds_actual_balance_and_position_requests_offline():
    pytest.importorskip("webull.core.client")
    from webull.core.client import ApiClient
    from webull.core.http.initializer.client_initializer import ClientInitializer
    adapter = adapter_for("webull", {"account_id": "test-account", "app_key": "test-key", "app_secret": "test-secret"})
    seen = []
    def request(_api, api_request):
        seen.append((api_request.get_action_name(), api_request.get_query_params()))
        if api_request.get_action_name() == "/trading/assets/balances/get":
            return response({"account_currency_assets": [{"currency": "USD", "cash_balance": "0"}]})
        return response([])
    with patch.object(ClientInitializer, "initializer"), patch.object(ApiClient, "get_response", autospec=True, side_effect=request):
        result = collect_account(adapter, "webull")
    assert result["cash_by_currency"] == {"USD": 0}
    assert seen == [("/trading/assets/balances/get", {"account_id": "test-account"}),
                    ("/trading/assets/positions/list", {"account_id": "test-account"})]
