import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from alpaca.adapter import Adapter
from common.errors import BrokerError, UnsupportedOrder


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)


def command(**changes):
    base = {"id": "alpaca-test-1", "broker": "alpaca", "action": "place_order",
            "symbol": "AAPL", "side": "buy", "quantity": "2",
            "order_type": "limit", "time_in_force": "fok", "limit_price": "150.25"}
    return {**base, **changes}


class AlpacaTests(unittest.TestCase):
    def test_native_fok_and_stable_client_identifier(self):
        adapter = Adapter({})
        order = adapter.build_order(command())
        self.assertEqual(order["time_in_force"], "fok")
        self.assertEqual(order["limit_price"], "150.25")
        self.assertEqual(order["client_order_id"], adapter.build_order(command())["client_order_id"])
        self.assertIsNone(adapter._http)

    def test_market_order_has_no_limit_price(self):
        order = Adapter({}).build_order(command(order_type="market", limit_price=None))
        self.assertEqual(order["type"], "market")
        self.assertNotIn("limit_price", order)

    def test_reject_unsupported_fok_combinations(self):
        for fields in ({"quantity": "0.5"}, {"asset_class": "crypto", "symbol": "BTC/USD"},
                       {"asset_class": "us_option"}, {"options": {"extended_hours": True}}):
            with self.subTest(fields=fields), self.assertRaises(BrokerError):
                Adapter({}).build_order(command(**fields))

    def test_reject_market_limit_price_and_bad_precision(self):
        for fields in ({"order_type": "market"}, {"limit_price": "1.001"}, {"limit_price": "-1"},
                       {"options": {"extended_hours": "false"}}, {"options": {"invented": True}}):
            with self.subTest(fields=fields), self.assertRaises(BrokerError):
                Adapter({}).build_order(command(**fields))

    def test_allowed_crypto_and_fractional_day(self):
        crypto = Adapter({}).build_order(command(symbol="BTC/USD", quantity="0.01", time_in_force="ioc"))
        self.assertEqual(crypto["time_in_force"], "ioc")
        fraction = Adapter({}).build_order(command(quantity="0.25", time_in_force="day"))
        self.assertEqual(fraction["qty"], "0.25")

    def test_positions_are_unfiltered(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = [{"symbol": "AAPL"}, {"symbol": "BTCUSD"}]
        self.assertEqual(len(adapter.positions()), 2)
        adapter._http.request.assert_called_once_with("GET", "/v2/positions")

    def test_order_pagination_uses_ids_preserving_tied_timestamps(self):
        adapter = Adapter({})
        adapter._http = Mock()
        first = [{"id": str(i), "submitted_at": "2026-10-25T12:00:00Z", "status": "filled"}
                 for i in range(500)]
        second = [{"id": "500", "submitted_at": "2026-10-25T12:00:00Z", "status": "new"},
                  {"id": "midnight", "submitted_at": "2026-10-25T00:00:00Z"},
                  {"id": "old", "submitted_at": "2026-10-24T23:59:59Z"}]
        adapter._http.request.side_effect = [first, second]
        result = adapter.orders_today(START, END)
        self.assertEqual(len(result), 502)
        self.assertEqual(result[-1]["id"], "midnight")
        calls = adapter._http.request.call_args_list
        self.assertEqual(calls[0].kwargs["params"]["status"], "all")
        self.assertEqual(calls[1].kwargs["params"]["before_order_id"], "499")
        self.assertNotIn("after", calls[1].kwargs["params"])
        self.assertNotIn("until", calls[1].kwargs["params"])

    def test_repeated_cursor_fails_explicitly(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = [
            {"id": str(i), "submitted_at": "2026-10-25T12:00:00Z"} for i in range(500)]
        with self.assertRaises(BrokerError):
            adapter.orders_today(START, END)

    def test_order_submission_not_retried(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.side_effect = BrokerError("timeout")
        with self.assertRaises(BrokerError):
            adapter.place_order(command())
        self.assertEqual(adapter._http.request.call_count, 1)

    def test_capabilities_are_network_free_and_do_not_promise_auction_access(self):
        adapter = Adapter({})
        result = adapter.capabilities()
        self.assertIn("fok", result["time_in_force"]["whole_share_equity"])
        self.assertNotIn("fok", result["time_in_force"]["crypto"])
        self.assertFalse(result["luld"]["auction_participation_confirmed"])
        self.assertIsNone(adapter._http)

    def test_tradability_does_not_confuse_eligibility_with_current_availability(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = {"symbol": "AAPL", "tradable": True, "status": "active"}
        result = adapter.tradability("AAPL")
        self.assertTrue(result["instrument_tradable"])
        self.assertIsNone(result["tradable"])
        self.assertIsNone(result["halted"])
        adapter._http.request.return_value = {"symbol": "AAPL", "tradable": False, "status": "inactive"}
        self.assertFalse(adapter.tradability("AAPL")["tradable"])

    def test_option_tradability_uses_contract_endpoint(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = {"tradable": True, "status": "active"}
        adapter.tradability("AAPL261218C00200000")
        self.assertIn("/v2/options/contracts/", adapter._http.request.call_args.args[1])

    def test_borrow_uses_current_status_field_without_inventing_inventory(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = {"symbol": "AAPL", "class": "us_equity", "shortable": True,
                                             "borrow_status": "easy_to_borrow", "easy_to_borrow": False}
        result = adapter.borrow_availability("AAPL")
        self.assertTrue(result["easy_to_borrow"])
        self.assertFalse(result["locate_required"])
        self.assertIsNone(result["quantity"])
        self.assertIsNone(result["fee_rate"])
        self.assertEqual(adapter._http.request.call_count, 1)

    def test_live_htb_quote_distinguishes_locate_price_from_borrow_rate(self):
        adapter = Adapter({"base_url": "https://api.alpaca.markets"})
        adapter._http = Mock()
        adapter._http.request.side_effect = [
            {"symbol": "TSLA", "class": "us_equity", "shortable": True, "borrow_status": "hard_to_borrow"},
            {"quotes": [{"symbol": "TSLA", "available_qty": 1000, "price": "0.0123", "quoted_at": "2026-10-25T12:00:00Z"}], "errors": []},
        ]
        result = adapter.borrow_availability("TSLA")
        self.assertEqual(result["quantity"], "1000")
        self.assertEqual(result["locate_fee_per_share"], "0.0123")
        self.assertIsNone(result["fee_rate"])
        self.assertTrue(result["locate_required"])
        self.assertFalse(result["inventory_reserved"])
        self.assertTrue(all(call.args[0] == "GET" for call in adapter._http.request.call_args_list))

    def test_paper_htb_quote_is_explicitly_unavailable(self):
        adapter = Adapter({})
        adapter._http = Mock()
        adapter._http.request.return_value = {"symbol": "TSLA", "class": "us_equity", "shortable": True,
                                             "borrow_status": "hard_to_borrow"}
        result = adapter.borrow_availability("TSLA")
        self.assertIsNone(result["quantity"])
        self.assertIn("paper trading", result["reason"])
        self.assertEqual(adapter._http.request.call_count, 1)

    def test_missing_htb_entitlement_preserves_classification_and_unknown_quantity(self):
        adapter = Adapter({"base_url": "https://api.alpaca.markets"})
        adapter._http = Mock()
        adapter._http.request.side_effect = [
            {"symbol": "TSLA", "class": "us_equity", "shortable": True, "borrow_status": "hard_to_borrow"},
            BrokerError("HTTP 403"),
        ]
        result = adapter.borrow_availability("TSLA")
        self.assertIsNone(result["quantity"])
        self.assertIn("quote_error", result)
        self.assertTrue(result["locate_required"])


if __name__ == "__main__":
    unittest.main()
