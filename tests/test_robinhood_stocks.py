from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import uuid

import pytest

from common.errors import BrokerError, UnsupportedOrder, OutcomeUnknown
from robinhood.adapter import Adapter


ACCOUNT = {"account_number": "TEST123", "agentic_allowed": True, "affiliate": "RHF", "state": "active",
           "brokerage_account_type": "individual", "deactivated": False, "permanently_deactivated": False}
INSTRUMENT = {"symbol": "AAPL", "tradeable": True, "state": "active", "country": "US",
              "fractional_tradability": "tradable", "short_selling_tradability": "tradable",
              "account_type_tradabilities": [{"account_type": "individual", "account_type_tradability": "tradable"}]}


def command(**updates):
    result = {"id": "stock-command-1", "broker": "robinhood", "action": "place_order", "symbol": "AAPL",
              "side": "buy", "order_type": "limit", "quantity": "2", "limit_price": "100", "time_in_force": "day"}
    result.update(updates)
    return result


def order(**updates):
    result = {"id": "order-1", "symbol": "AAPL", "side": "buy", "type": "limit", "state": "confirmed", "quantity": "2",
              "price": "100", "created_at": "2026-10-25T15:00:00Z", "cumulative_quantity": "0", "average_price": None,
              "trigger": "immediate", "time_in_force": "gfd"}
    result.update(updates)
    return result


def adapter(**config):
    result = Adapter({"account_number": "TEST123", **config})
    def call(name, arguments, **kwargs):
        if name == "get_accounts":
            return {"accounts": [dict(ACCOUNT)]}
        if name == "search":
            return {"results": [{"symbol": arguments["query"], "instrument_id": "instrument-" + arguments["query"], "name": arguments["query"]}]}
        if name == "get_equity_tradability":
            return {"results": [dict(INSTRUMENT)], "not_found": []}
        if name == "get_equity_positions":
            return {"positions": [{"symbol": "AAPL", "quantity": "3", "shares_available_for_sells": "3", "average_buy_price": "90"}], "next": ""}
        if name == "review_equity_order":
            return {**arguments, "order_checks": {}}
        if name == "place_equity_order":
            return {"order": order(ref_id=arguments["ref_id"], symbol=arguments["symbol"], side=arguments["side"])}
        if name == "cancel_equity_order":
            return {"accepted": True}
        if name == "get_equity_orders":
            return {"orders": [order()], "next": ""}
        if name == "get_portfolio":
            return {"cash": "1200.50", "buying_power": {"buying_power": "2401", "unleveraged_buying_power": "1200.50", "display_currency": "USD"}, "currency": "USD"}
        raise AssertionError(name)
    result._call_tool = Mock(side_effect=call)
    return result


def test_stock_market_and_limit_day_gtc_decimal_identity():
    result = Adapter({}).build_order(command())
    assert result["quantity"] == "2"
    assert result["limit_price"] == "100"
    assert result["time_in_force"] == "gfd"
    assert result["market_hours"] == "regular_hours"
    assert str(uuid.UUID(result["ref_id"])) == result["ref_id"]
    assert result == Adapter({}).build_order(command())
    market = Adapter({}).build_order(command(order_type="market", quantity="0.125", limit_price=None, time_in_force="gtc"))
    assert market["time_in_force"] == "gtc"
    assert "limit_price" not in market


@pytest.mark.parametrize("updates", [{"time_in_force": "fok"}, {"time_in_force": "ioc"}, {"asset_class": "crypto"},
    {"symbol": "BTC-USD"}, {"symbol": "AAPL260101C00010000"}, {"side": "sell_short"}, {"extended_hours": True},
    {"quantity": "0.5"}, {"order_type": "market", "limit_price": None, "quantity": "0.0000001"}])
def test_rejects_unpreservable_or_non_stock_requests(updates):
    with pytest.raises(UnsupportedOrder):
        Adapter({}).build_order(command(**updates))


def test_buy_checks_account_listing_review_then_native_placement():
    subject = adapter()
    result = subject.place_order(command())
    assert result["id"] == "order-1"
    calls = subject._call_tool.call_args_list
    assert calls[-2].args[0] == "review_equity_order"
    assert calls[-1].args[0] == "place_equity_order"
    assert "ref_id" not in calls[-2].args[1]
    assert calls[-1].kwargs == {"mutation": True}
    assert calls[-1].args[1]["account_number"] == "TEST123"


def test_non_agentic_account_stops_submission():
    subject = adapter()
    subject._call_tool.side_effect = lambda *a, **k: {"accounts": [{**ACCOUNT, "agentic_allowed": False}]}
    with pytest.raises(UnsupportedOrder):
        subject.place_order(command())
    assert subject._call_tool.call_count == 1


@pytest.mark.parametrize("update", [{"state": "inactive"}, {"tradeable": False},
    {"account_type_tradabilities": []}])
def test_unknown_or_non_us_eligibility_blocks_submission(update):
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"results": [{**INSTRUMENT, **update}]} if name == "get_equity_tradability" else base(name, *a, **k)
    with pytest.raises(UnsupportedOrder):
        subject.place_order(command())
    assert "place_equity_order" not in [call.args[0] for call in subject._call_tool.call_args_list]


def test_closing_only_is_sell_only_and_halt_missing_is_unknown():
    subject = adapter()
    base = subject._call_tool.side_effect
    closed = {**INSTRUMENT, "account_type_tradabilities": [{"account_type": "individual", "account_type_tradability": "position_closing_only"}]}
    subject._call_tool.side_effect = lambda name, *a, **k: {"results": [closed]} if name == "get_equity_tradability" else base(name, *a, **k)
    result = subject.tradability("AAPL")
    assert result["buy_tradable"] is False
    assert result["sell_tradable"] is True
    assert result["halted"] is None
    assert result["tradable"] is True


def test_sell_requires_available_long_shares():
    subject = adapter()
    with pytest.raises(UnsupportedOrder, match="available long shares"):
        subject.place_order(command(side="sell", quantity="4"))
    assert "place_equity_order" not in [call.args[0] for call in subject._call_tool.call_args_list]
    assert subject.place_order(command(side="sell", quantity="2"))["side"] == "sell"


@pytest.mark.parametrize("checks", [{"alert_type": "REGULATORY_HALT"}, None])
def test_nonempty_or_missing_preview_blocks_order(checks):
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"order_checks": checks} if name == "review_equity_order" else base(name, *a, **k)
    with pytest.raises(UnsupportedOrder, match="review"):
        subject.place_order(command())
    assert "place_equity_order" not in [call.args[0] for call in subject._call_tool.call_args_list]


def test_preview_ticket_mismatch_blocks_order():
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"order_checks": {}, "symbol": "MSFT"} if name == "review_equity_order" else base(name, *a, **k)
    with pytest.raises(BrokerError, match="ticket"):
        subject.place_order(command())


def test_preview_can_be_explicitly_disabled_without_skipping_eligibility():
    subject = adapter(review_before_order=False)
    subject.place_order(command())
    names = [call.args[0] for call in subject._call_tool.call_args_list]
    assert "review_equity_order" not in names
    assert "get_equity_tradability" in names


@pytest.mark.parametrize("result", [{}, {"order": None}, {"order": order(ref_id="wrong")}])
def test_no_order_or_approval_response_is_uncertain(result):
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: result if name == "place_equity_order" else base(name, *a, **k)
    with pytest.raises(OutcomeUnknown):
        subject.place_order(command())
    assert [call.args[0] for call in subject._call_tool.call_args_list].count("place_equity_order") == 1


def test_positions_and_orders_paginate_without_fetching_cursor_as_url():
    subject = adapter()
    subject._account_record = Mock(return_value=ACCOUNT)
    subject._listing_record = Mock(side_effect=lambda symbol: {"symbol": symbol, "instrument_id": "id-" + symbol})
    subject._call_tool.side_effect = [{"positions": [{"symbol": "AAPL"}], "next": "https://evil.example/not-a-fetch"},
                                      {"positions": [{"symbol": "MSFT"}], "next": ""}]
    assert len(subject.positions()) == 2
    assert subject._call_tool.call_args.args[1]["cursor"] == "https://evil.example/not-a-fetch"
    start = datetime(2026, 10, 25, 4, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    subject._call_tool.side_effect = [
        {"orders": [order(id="early", created_at="2026-10-25T04:00:00Z")], "next": "abc"},
        {"orders": [order(id="late", created_at="2026-10-26T03:59:59Z"), order(id="boundary", created_at="2026-10-26T04:00:00Z")], "next": ""},
    ]
    assert [row["id"] for row in subject.orders_today(start, end)] == ["early", "late"]
    assert subject._call_tool.call_args.args[1] == {"account_number": "TEST123", "created_at_gte": "2026-10-25T04:00:00Z", "cursor": "abc"}


@pytest.mark.parametrize("data", [{"positions": []}, {"positions": [], "next": None}, {"positions": [], "next": 42}])
def test_missing_or_invalid_pagination_never_claims_complete(data):
    subject = adapter()
    subject._account_record = Mock(return_value=ACCOUNT)
    subject._call_tool.return_value = data
    subject._call_tool.side_effect = None
    with pytest.raises(BrokerError):
        subject.positions()


def test_open_discovery_has_no_today_filter_and_cancel_is_async():
    subject = adapter()
    assert subject.open_stock_orders()[0]["order_id"] == "order-1"
    args = [c.args[1] for c in subject._call_tool.call_args_list if c.args[0] == "get_equity_orders"]
    assert args == [{"account_number": "TEST123"}]
    result = subject.cancel_stock_order("order-1", "AAPL")
    assert result == {"accepted": True, "order_id": "order-1", "cancellation_confirmed": False}
    assert subject._call_tool.call_args.args[0] == "cancel_equity_order"


def test_unknown_state_and_mismatched_cancel_never_write():
    subject = adapter()
    subject._orders = Mock(return_value=[order(state="new-unrecognized-state")])
    with pytest.raises(BrokerError, match="unrecognized"):
        subject.open_stock_orders()
    subject._orders.return_value = [order(symbol="MSFT")]
    with pytest.raises(BrokerError, match="symbol"):
        subject.cancel_stock_order("order-1", "AAPL")
    assert "cancel_equity_order" not in [c.args[0] for c in subject._call_tool.call_args_list]


def test_borrow_eligibility_is_not_inventory_or_short_order_support():
    result = adapter().borrow_availability("AAPL")
    assert result["shortable"] is True
    assert result["supported"] is False
    assert result["short_order_supported"] is False
    assert result["available_quantity"] is None
    assert result["fee_rate"] is None
    assert result["locate_required"] is None


def test_cash_and_buying_power_are_not_conflated_and_fill_is_native():
    subject = adapter()
    account = subject.snapshot_account()
    assert account["cash"] == 1200.5
    assert account["buying_power_by_currency"] == {"USD": 2401}
    assert account["positions"][0]["qty"] == 3
    result = subject.snapshot_order(order(state="partially_filled_rest_cancelled", cumulative_quantity="1", average_price="99.5"), datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert result["status"] == "CANCELED"
    assert result["filled_quantity"] == 1
    assert result["average_fill_price"] == 99.5
    assert result["limit_price"] == 100


def test_capabilities_are_us_equities_with_explicit_limitations():
    result = Adapter({}).capabilities()
    assert result["luld_applicable"] is True
    assert result["native_fok"] is False
    assert result["luld_auction_participation"] is None
    assert result["luld_auction_status"] == "unverified"
    assert result["order_types"] == ["market", "limit"]


def test_review_decimal_format_does_not_change_amount():
    subject = adapter()
    base = subject._call_tool.side_effect
    def call(name, arguments, **kwargs):
        if name == "review_equity_order":
            return {**arguments, "quantity": "2.000000", "limit_price": "100.00", "order_checks": {}}
        return base(name, arguments, **kwargs)
    subject._call_tool.side_effect = call
    assert subject.place_order(command())["id"] == "order-1"


def test_acknowledged_wrong_quantity_is_not_accepted():
    subject = adapter()
    base = subject._call_tool.side_effect
    def call(name, arguments, **kwargs):
        result = base(name, arguments, **kwargs)
        if name == "place_equity_order":
            result["order"]["quantity"] = "20"
        return result
    subject._call_tool.side_effect = call
    with pytest.raises(OutcomeUnknown, match="quantity"):
        subject.place_order(command())


def test_missing_buying_power_is_unknown_without_fabricating_cash():
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"cash": "20", "buying_power": None, "currency": "USD"} if name == "get_portfolio" else base(name, *a, **k)
    result = subject.snapshot_account()
    assert result["cash"] == 20
    assert result["buying_power_by_currency"] == {}
    assert result["cash_basis"] == "available_cash_including_early_access"


@pytest.mark.parametrize("symbol", ["AAPL.HK", "00700.HK", "TSM.TW", "AAPL260101C00010000", "BTC-USD"])
def test_direct_order_and_query_foreign_or_derivative_tickers_fail_before_network(symbol):
    subject = adapter()
    with pytest.raises(UnsupportedOrder):
        subject.build_order(command(symbol=symbol))
    with pytest.raises(UnsupportedOrder):
        subject.tradability(symbol)
    subject._call_tool.assert_not_called()


@pytest.mark.parametrize("config", [{"asset_class": "crypto"}, {"asset_class": "option"}, {"market": "HK"}, {"country": "CA"}])
def test_direct_adapter_configuration_cannot_override_us_scope(config):
    from common.errors import ConfigurationError
    with pytest.raises(ConfigurationError, match="US stocks"):
        Adapter(config)


@pytest.mark.parametrize("changes", [{"asset_class": "option"}, {"market": "HK"}, {"country_code": "CA"}])
def test_direct_command_market_metadata_is_rejected(changes):
    with pytest.raises(UnsupportedOrder):
        Adapter({}).build_order(command(**changes))


@pytest.mark.parametrize("symbol,country", [("TSM", "TW"), ("BABA", "CN"), ("AAPL", None)])
def test_us_listing_search_accepts_adrs_without_domicile_gate(symbol, country):
    subject = adapter()
    base = subject._call_tool.side_effect
    def call(name, arguments, **kwargs):
        if name == "get_equity_tradability":
            return {"results": [{**INSTRUMENT, "symbol": symbol, "country": country}]}
        return base(name, arguments, **kwargs)
    subject._call_tool.side_effect = call
    result = subject.tradability(symbol)
    assert result["buy_tradable"] is True
    assert result["us_listing_verified"] is True
    assert result["instrument_id"] == "instrument-" + symbol
    search = [call for call in subject._call_tool.call_args_list if call.args[0] == "search"]
    assert search[0].args[1] == {"query": symbol, "asset_type": "instrument", "limit": 20}


def test_country_us_cannot_replace_exact_listing_resolution():
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"results": [{"symbol": "AAP", "instrument_id": "wrong"}]} if name == "search" else base(name, *a, **k)
    result = subject.tradability("AAPL")
    assert result["us_listing_verified"] is None
    assert result["buy_tradable"] is False
    with pytest.raises(UnsupportedOrder):
        subject.place_order(command())


@pytest.mark.parametrize("operation", ["positions", "orders"])
def test_queries_filter_explicit_nonus_and_derivative_records(operation):
    subject = adapter()
    base = subject._call_tool.side_effect
    tool = "get_equity_positions" if operation == "positions" else "get_equity_orders"
    collection = "positions" if operation == "positions" else "orders"
    rows = [order(id="us", symbol="TSM", country="TW", instrument_id="instrument-TSM"),
            order(id="hk", symbol="AAPL.HK"), order(id="option", symbol="AAPL260101C00010000"),
            order(id="explicit", symbol="AAPL", instrument_type="OPTION"), order(id="foreign", symbol="SHOP", market="CA")]
    subject._call_tool.side_effect = lambda name, *a, **k: {collection: rows, "next": ""} if name == tool else base(name, *a, **k)
    result = subject.positions() if operation == "positions" else subject._orders()
    assert [row["symbol"] for row in result] == ["TSM"]
    assert [call.args[1]["query"] for call in subject._call_tool.call_args_list if call.args[0] == "search"] == ["TSM"]


def test_order_instrument_identity_mismatch_blocks_cancellation():
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"orders": [order(instrument_id="wrong")], "next": ""} if name == "get_equity_orders" else base(name, *a, **k)
    with pytest.raises(BrokerError, match="identity"):
        subject.cancel_stock_order("order-1")
    assert "cancel_equity_order" not in [call.args[0] for call in subject._call_tool.call_args_list]


def test_unknown_listing_does_not_claim_complete_positions():
    subject = adapter()
    base = subject._call_tool.side_effect
    subject._call_tool.side_effect = lambda name, *a, **k: {"results": []} if name == "search" else base(name, *a, **k)
    with pytest.raises(BrokerError, match="not complete"):
        subject.positions()
