"""US-stock restrictions apply below the CLI and to mixed-asset native reads."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alpaca.adapter import Adapter as Alpaca
from tradestation.adapter import Adapter as TradeStation
from longbridge.adapter import Adapter as Longbridge
from common.registry import adapter_for
Webull = type(adapter_for("webull", {}))
from zhuorui.adapter import Adapter as Zhuorui
from zhuorui.bridge_worker import NativeMarketClient, read_orders, stock_holdings
from common.equity_scope import require_us_equity, us_stock_symbol
from common.errors import BrokerError, UnsupportedOrder


ADAPTERS = [Alpaca, TradeStation, Longbridge, Webull, Zhuorui]
START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)


def command(**fields):
    return {"id": "us-scope", "symbol": "AAPL", "side": "buy", "quantity": "1",
            "order_type": "limit", "time_in_force": "day", "limit_price": "100", **fields}


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("asset", ["crypto", "us_option", "option", "futures", "event", "fund", "warrant", "hk_equity"])
def test_direct_adapter_rejects_other_asset_classes(adapter, asset):
    with pytest.raises(UnsupportedOrder):
        adapter({}).build_order(command(asset_class=asset))
    with pytest.raises(UnsupportedOrder):
        adapter({"asset_class": asset}).build_order(command())


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("symbol", ["AAPL261218C00200000", "AAPL261218C00200000.US", "700.HK", "ABC.HK.US", "BMW.DE", "AAPL.US.US", "BTC/USD", "@ES", "/ES", "0700"])
def test_nonstock_symbols_cannot_be_mislabeled_as_equities(adapter, symbol):
    with pytest.raises(UnsupportedOrder):
        adapter({}).build_order(command(symbol=symbol, asset_class="us_equity"))
    with pytest.raises(UnsupportedOrder):
        adapter({}).tradability(symbol)
    with pytest.raises(UnsupportedOrder):
        adapter({}).borrow_availability(symbol)


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_direct_adapter_rejects_foreign_market_selector(adapter):
    with pytest.raises(UnsupportedOrder):
        adapter({}).build_order(command(market="HK"))


def test_normalization_handles_class_shares_and_longbridge_qualification():
    assert us_stock_symbol(" brk.b.us ") == "BRK.B"
    assert us_stock_symbol("brk-b", qualified=True) == "BRK-B.US"
    assert require_us_equity({}) == "us_equity"
    assert Longbridge({}).build_order(command(symbol="aapl"))["symbol"] == "AAPL.US"


@pytest.mark.parametrize("adapter,stock,nonstock", [
    (Alpaca, {"symbol": "AAPL", "asset_class": "us_equity"}, {"symbol": "BTCUSD", "asset_class": "crypto"}),
    (TradeStation, {"Legs": [{"Symbol": "AAPL", "AssetType": "STOCK"}]}, {"Legs": [{"Symbol": "AAPL", "AssetType": "STOCKOPTION"}]}),
    (Longbridge, {"symbol": "AAPL.US", "market": "US"}, {"symbol": "700.HK", "market": "HK"}),
    (Webull, {"symbol": "AAPL", "market": "US", "instrument_type": "EQUITY"}, {"symbol": "BTCUSD", "instrument_type": "CRYPTO"}),
    (Zhuorui, {"code": "AAPL", "ts": "US"}, {"code": "700", "ts": "HK"}),
])
def test_cancellation_classifier_excludes_explicit_nonstocks(adapter, stock, nonstock):
    instance = adapter({})
    assert instance.is_us_stock_order(stock) is True
    assert instance.is_us_stock_order(nonstock) is False
    with pytest.raises(BrokerError):
        instance.is_us_stock_order({})


def test_alpaca_unknown_class_requires_matching_instrument_metadata():
    adapter = Alpaca({})
    adapter._http = Mock()
    adapter._http.request.return_value = {"symbol": "AAPL", "class": "us_equity"}
    assert adapter.is_us_stock_order({"symbol": "AAPL"}) is True
    adapter._http.request.assert_called_once_with("GET", "/v2/assets/AAPL")
    adapter._http.request.return_value = {"symbol": "AAPL", "class": "crypto"}
    assert adapter.is_us_stock_order({"symbol": "AAPL"}) is False
    adapter._http.request.return_value = {"symbol": "OTHER", "class": "us_equity"}
    with pytest.raises(BrokerError):
        adapter.is_us_stock_order({"symbol": "AAPL"})


def test_tradestation_unknown_class_requires_symbol_metadata():
    adapter = TradeStation({})
    adapter._request = Mock(return_value={"Symbols": [{"Symbol": "AAPL", "AssetType": "STOCK"}]})
    assert adapter.is_us_stock_order({"Symbol": "AAPL"}) is True
    adapter._request.return_value = {"Symbols": [{"Symbol": "AAPL", "AssetType": "FUTURE"}]}
    assert adapter.is_us_stock_order({"Symbol": "AAPL"}) is False
    with pytest.raises(UnsupportedOrder):
        adapter.borrow_availability("AAPL")


def test_webull_unknown_class_requires_us_stock_profile():
    adapter = Webull({})
    adapter._stock_instruments = Mock(return_value=[{"symbol": "AAPL"}])
    assert adapter.is_us_stock_order({"symbol": "AAPL"}) is True
    adapter._stock_instruments.assert_called_once_with("AAPL")
    adapter._stock_instruments.return_value = []
    with pytest.raises(BrokerError):
        adapter.is_us_stock_order({"symbol": "AAPL"})


def test_longbridge_never_returns_hk_or_option_or_fund_positions():
    adapter = Longbridge({})
    adapter._request = Mock(return_value={"list": [{"account_channel": "lb", "stock_info": [
        {"symbol": "AAPL.US"}, {"symbol": "700.HK"}, {"symbol": "AAPL261218C00200000.US"},
        {"symbol": "FUND.US", "asset_class": "fund"}]}]})
    result = adapter.positions()
    assert result["stocks"]["list"][0]["stock_info"] == [{"symbol": "AAPL.US"}]
    assert result["funds"] == {"list": []}
    adapter._request.assert_called_once_with("get", "/v1/asset/stock")


def test_webull_native_array_position_filter_preserves_wrapper_contract():
    adapter = Webull({"account_id": "account"})
    response = Mock(status_code=200)
    response.json.return_value = [{"symbol": "AAPL", "instrument_type": "EQUITY", "market": "US"},
                                  {"symbol": "AAPL261218C00200000", "instrument_type": "OPTION", "market": "US"}]
    adapter._trade = SimpleNamespace(account_v2=SimpleNamespace(get_account_position=Mock(return_value=response)))
    assert len(adapter.positions()) == 1
    assert adapter.positions()[0]["symbol"] == "AAPL"


def test_zhuorui_worker_filters_holdings_orders_and_upstream_cancellation_reads():
    stock = {"code": "AAPL", "ts": "US", "orderTxnReference": "a", "entrustTime": int(START.timestamp() * 1000)}
    foreign = {"code": "700", "ts": "HK", "orderTxnReference": "b", "entrustTime": stock["entrustTime"]}
    response = {"code": "000000", "data": [stock, foreign]}
    client = Mock()
    client.query.return_value = response
    client.query_orders_for_date.return_value = response
    assert read_orders(client, START, END) == [stock]
    guarded = NativeMarketClient(client, False, ValueError)
    assert guarded.query("orders")["data"] == [stock]
    client.query_order_detail.return_value = {"code": "000000", "data": foreign}
    with pytest.raises(ValueError):
        guarded.query_order_detail("b", stock["entrustTime"])
    holdings = {"code": "000000", "data": {"holdList": [stock, foreign]}}
    assert stock_holdings(holdings)["data"]["holdList"] == [stock]
    with pytest.raises(ValueError):
        stock_holdings({"code": "000000", "data": {"holdList": [{"code": "AAPL"}]}})


def test_alpaca_checks_actual_instrument_class_before_order_submission():
    adapter = Alpaca({})
    adapter._http = Mock()
    adapter._http.request.return_value = {"symbol": "BTCUSD", "class": "crypto"}
    with pytest.raises(UnsupportedOrder):
        adapter.place_order(command(symbol="BTCUSD"))
    adapter._http.request.assert_called_once_with("GET", "/v2/assets/BTCUSD")
    adapter._http.request.reset_mock()
    adapter._http.request.side_effect = [{"symbol": "AAPL", "class": "us_equity"}, {"id": "order"}]
    assert adapter.place_order(command()) == {"id": "order"}
    assert adapter._http.request.call_args.args == ("POST", "/v2/orders")


def test_alpaca_rejects_otc_fok_after_instrument_lookup():
    adapter = Alpaca({})
    adapter._http = Mock()
    adapter._http.request.return_value = {"symbol": "TCEHY", "class": "us_equity", "exchange": "OTC"}
    with pytest.raises(UnsupportedOrder):
        adapter.place_order(command(symbol="TCEHY", time_in_force="fok"))
    assert adapter._http.request.call_count == 1


def test_tradestation_checks_actual_type_before_order_submission():
    adapter = TradeStation({"account_id": "account"})
    adapter._request = Mock(return_value={"Symbols": [{"Symbol": "FUND", "AssetType": "MUTUALFUND"}]})
    with pytest.raises(UnsupportedOrder):
        adapter.place_order(command(symbol="FUND"))
    adapter._request.assert_called_once_with("GET", "/marketdata/symbols/FUND")


@pytest.mark.parametrize("query", ["tradability", "borrow_availability"])
def test_webull_rejects_conflicting_profile_metadata(query):
    adapter = Webull({})
    adapter._stock_instruments = Mock(return_value=[{"symbol": "AAPL", "market": "HK", "instrument_type": "EQUITY"}])
    with pytest.raises(UnsupportedOrder):
        getattr(adapter, query)("AAPL")


def test_alpaca_today_excludes_crypto_and_option_records():
    adapter = Alpaca({})
    adapter._http = Mock()
    adapter._http.request.return_value = [
        {"id": "stock", "submitted_at": START.isoformat(), "symbol": "AAPL", "asset_class": "us_equity"},
        {"id": "crypto", "submitted_at": START.isoformat(), "symbol": "BTCUSD", "asset_class": "crypto"},
        {"id": "option", "submitted_at": START.isoformat(), "symbol": "AAPL261218C00200000", "asset_class": "us_equity"},
    ]
    assert [row["id"] for row in adapter.orders_today(START, END)] == ["stock"]


def test_tradestation_mixed_positions_and_orders_filter_native_asset_types():
    adapter = TradeStation({"account_id": "account"})
    stock = {"Symbol": "AAPL", "AssetType": "STOCK"}
    future = {"Symbol": "ESH27", "AssetType": "FUTURE"}
    adapter._request = Mock(return_value={"Positions": [stock, future]})
    assert adapter.positions() == [stock]
    adapter._orders = Mock(return_value=[
        {"OrderID": "stock", "OpenedDateTime": START.isoformat(), "Legs": [stock]},
        {"OrderID": "future", "OpenedDateTime": START.isoformat(), "Legs": [future]},
    ])
    assert [row["OrderID"] for row in adapter.orders_today(START, END)] == ["stock"]


def test_longbridge_mixed_today_excludes_hk_and_option_contracts():
    adapter = Longbridge({})
    adapter._history = Mock(return_value=[
        {"order_id": "stock", "submitted_at": START.timestamp(), "symbol": "AAPL.US"},
        {"order_id": "hk", "submitted_at": START.timestamp(), "symbol": "700.HK"},
        {"order_id": "option", "submitted_at": START.timestamp(), "symbol": "AAPL261218C00200000.US"},
    ])
    adapter._request = Mock(return_value={"orders": []})
    assert [row["order_id"] for row in adapter.orders_today(START, END)] == ["stock"]


def test_webull_mixed_today_excludes_options_and_event_contracts():
    adapter = Webull({"account_id": "account", "query_interval": 0})
    adapter._trade = SimpleNamespace(order_v3=SimpleNamespace(list_order_history=Mock(), list_order_open=Mock()))
    rows = [{"order_id": name, "place_time_at": START.isoformat(), "symbol": "AAPL", "market": "US", "instrument_type": kind}
            for name, kind in [("stock", "EQUITY"), ("option", "OPTION"), ("event", "EVENT")]]
    adapter._pages = Mock(side_effect=[[{"orders": rows, "combo_type": "NORMAL"}], []])
    assert [row["order_id"] for row in adapter.orders_today(START, END)] == ["stock"]
