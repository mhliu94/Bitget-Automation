import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.errors import BrokerError, OutcomeUnknown, UnsupportedOrder
from common.registry import adapter_for


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)


def command(**changes):
    return {**{"id": "wb-1", "symbol": "AAPL", "side": "buy", "quantity": "2",
              "order_type": "limit", "time_in_force": "day", "limit_price": "123.45"}, **changes}


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


def group(order_id, placed="2026-10-25T12:00:00Z", status="FILLED"):
    return {"client_order_id": "group-" + order_id, "combo_type": "NORMAL", "orders": [
        {"order_id": order_id, "client_order_id": "client-" + order_id, "place_time_at": placed, "status": status}]}


class WebullTests(unittest.TestCase):
    def adapter(self):
        return adapter_for("webull", {"account_id": "test-account", "query_interval": 0})

    def test_dry_run_and_native_event_fok(self):
        adapter = self.adapter()
        result = adapter.build_order(command(asset_class="event", symbol="EVENT-CONTRACT", limit_price="0.4",
                                             time_in_force="fok", options={"event_outcome": "yes"}))
        self.assertEqual(result["instrument_type"], "EVENT")
        self.assertEqual(result["time_in_force"], "FOK")
        self.assertEqual(len(result["client_order_id"]), 32)
        self.assertIsNone(adapter._trade)

    def test_fok_cannot_be_silently_changed_for_stock_crypto_or_futures(self):
        for asset in ("equity", "crypto", "futures"):
            with self.subTest(asset=asset), self.assertRaises(UnsupportedOrder):
                self.adapter().build_order(command(asset_class=asset, time_in_force="fok"))

    def test_market_and_ioc_capabilities(self):
        result = self.adapter().build_order(command(order_type="market", limit_price=None))
        self.assertEqual(result["order_type"], "MARKET")
        self.assertNotIn("limit_price", result)
        result = self.adapter().build_order(command(asset_class="crypto", order_type="market", limit_price=None,
                                                   time_in_force="ioc", symbol="BTCUSD"))
        self.assertEqual(result["time_in_force"], "IOC")

    def test_crypto_market_and_limit_have_different_tif_contracts(self):
        for kind, tif in (("market", "day"), ("market", "gtc"), ("limit", "ioc")):
            with self.subTest(kind=kind, tif=tif), self.assertRaises(UnsupportedOrder):
                self.adapter().build_order(command(asset_class="crypto", symbol="BTCUSD", order_type=kind,
                                                   limit_price=None if kind == "market" else "100", time_in_force=tif))
        for tif in ("day", "gtc"):
            result = self.adapter().build_order(command(asset_class="crypto", symbol="BTCUSD", time_in_force=tif))
            self.assertEqual(result["time_in_force"], tif.upper())

    def test_invalid_event_and_market_options_fail(self):
        for update in ({"asset_class": "event", "options": {"event_outcome": "yes"}},
                       {"asset_class": "event", "limit_price": "0.5"},
                       {"asset_class": "option"}, {"options": {"routing": "LULD"}},
                       {"asset_class": "futures", "quantity": "0.5"},
                       {"order_type": "market", "limit_price": None, "options": {"support_trading_session": "ALL"}}):
            with self.subTest(update=update), self.assertRaises(UnsupportedOrder):
                self.adapter().build_order(command(**update))

    def test_history_open_pagination_and_group_deduplication(self):
        adapter = self.adapter()
        history = Mock(side_effect=[response({"data": [group("old", "2026-10-24T23:59:59Z"), group("one")], "pagination_key": "h1"}),
                                    response({"data": [group("duplicate")], "pagination_key": None})])
        opened = Mock(side_effect=[response({"data": [group("duplicate", status="SUBMITTED")], "pagination_key": "o1"}),
                                   response({"data": [group("two"), group("end", "2026-10-26T00:00:00Z")]})])
        adapter._trade = SimpleNamespace(order_v3=SimpleNamespace(list_order_history=history, list_order_open=opened))
        result = adapter.orders_today(START, END)
        self.assertEqual({row["order_id"] for row in result}, {"one", "duplicate", "two"})
        self.assertEqual(next(row["status"] for row in result if row["order_id"] == "duplicate"), "SUBMITTED")
        self.assertEqual(history.call_args.kwargs["pagination_key"], "h1")
        self.assertEqual(opened.call_args.kwargs["pagination_key"], "o1")
        self.assertEqual(history.call_args.kwargs["start_time"], "2026-10-25T00:00:00.000Z")

    def test_repeated_cursor_and_missing_timestamp_fail(self):
        adapter = self.adapter()
        method = Mock(return_value=response({"data": [], "pagination_key": "same"}))
        with self.assertRaises(BrokerError):
            list(adapter._pages(method))
        bad = group("bad")
        del bad["orders"][0]["place_time_at"]
        adapter._trade = SimpleNamespace(order_v3=SimpleNamespace(
            list_order_history=Mock(return_value=response({"data": [bad]})),
            list_order_open=Mock(return_value=response({"data": []}))))
        with self.assertRaises(BrokerError):
            adapter.orders_today(START, END)

    def test_positions_are_account_wide(self):
        adapter = self.adapter()
        method = Mock(return_value=response({"positions": [{"symbol": "AAPL"}, {"symbol": "BTCUSD"}]}))
        adapter._trade = SimpleNamespace(account_v2=SimpleNamespace(get_account_position=method))
        self.assertEqual(len(adapter.positions()["positions"]), 2)
        method.assert_called_once_with(account_id="test-account")

    def test_unknown_mutation_outcome_is_not_retried(self):
        adapter = self.adapter()
        method = Mock(side_effect=TimeoutError("secret"))
        adapter._trade = SimpleNamespace(order_v3=SimpleNamespace(place_order=method))
        with self.assertRaises(OutcomeUnknown):
            adapter.place_order(command())
        self.assertEqual(method.call_count, 1)

    def test_stock_tradability_distinguishes_close_only_and_halts_unknown(self):
        for status, tradable, close_only in (("OC", True, False), ("NT", False, False), ("CO", None, True), ("OTHER", None, False)):
            adapter = self.adapter()
            adapter._stock_instruments = Mock(return_value=[{"symbol": "AAPL", "status": status}])
            result = adapter.tradability("AAPL")
            self.assertEqual(result["tradable"], tradable)
            self.assertEqual(result["close_only"], close_only)
            self.assertIsNone(result["halted"])

    def test_extended_alias_and_luld_session_are_consistent(self):
        result = self.adapter().build_order(command(options={"extended_hours": True}))
        self.assertEqual(result["support_trading_session"], "ALL")
        with self.assertRaises(UnsupportedOrder):
            self.adapter().build_order(command(auction_intent="luld_reopening", options={"support_trading_session": "NIGHT"}))

    def test_borrow_flags_are_not_inventory_or_locate_confirmation(self):
        adapter = self.adapter()
        adapter._stock_instruments = Mock(return_value={"data": [{"symbol": "AAPL", "shortable": True, "easy_to_borrow": False}]})
        result = adapter.borrow_availability("AAPL")
        self.assertTrue(result["shortable"])
        self.assertFalse(result["easy_to_borrow"])
        self.assertFalse(result["inventory_confirmed"])
        for key in ("available_quantity", "fee_rate", "locate_required"):
            self.assertIsNone(result[key])

    def test_missing_or_malformed_borrow_flags_remain_unknown(self):
        adapter = self.adapter()
        for profile in ({"symbol": "AAPL"}, {"symbol": "AAPL", "shortable": "false", "easy_to_borrow": 1}):
            adapter._stock_instruments = Mock(return_value=[profile])
            result = adapter.borrow_availability("AAPL")
            self.assertIsNone(result["shortable"])
            self.assertIsNone(result["easy_to_borrow"])

    def test_official_sdk_offline_request_construction(self):
        try:
            from webull.core.client import ApiClient
            from webull.core.http.initializer.client_initializer import ClientInitializer
        except ImportError:
            self.skipTest("Optional official Webull SDK is not installed")
        adapter = adapter_for("webull", {"account_id": "test-account", "app_key": "test-key", "app_secret": "test-secret"})
        # Block authentication initialization and replace the complete network method.
        with patch.object(ClientInitializer, "initializer"), patch.object(ApiClient, "get_response", autospec=True) as send:
            send.return_value = response([{"symbol": "AAPL", "status": "OC"}])
            self.assertTrue(adapter.tradability("AAPL")["tradable"])
            request = send.call_args.kwargs["api_request"]
            self.assertEqual(request.get_action_name(), "/trading/instruments/stocks/profiles/list")
            self.assertEqual(request.get_query_params(), {"symbols": "AAPL", "category": "US_STOCK"})
            self.assertEqual(request.get_method(), "GET")
            self.assertFalse(adapter._api._auto_retry)
            send.return_value = response({"client_order_id": "test"})
            adapter.place_order(command())
            submitted = send.call_args.args[1]
            self.assertEqual(submitted.get_action_name(), "/trading/orders/place")
            self.assertEqual(submitted.get_body_params()["new_orders"][0]["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main()
