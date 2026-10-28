import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from tradestation.adapter import Adapter
from common.errors import BrokerError, ConfigurationError, UnsupportedOrder


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)


def command(**changes):
    base = {"id": "trade-station-test-1", "broker": "tradestation", "action": "place_order",
            "symbol": "AAPL", "side": "buy", "quantity": "2",
            "order_type": "limit", "time_in_force": "fok", "limit_price": "150.25"}
    return {**base, **changes}


def order(order_id, stamp="2026-10-25T12:00:00Z", status="FLL"):
    return {"OrderID": order_id, "AccountID": "12345", "OpenedDateTime": stamp, "Status": status}


class TradeStationTests(unittest.TestCase):
    def test_native_fok_payload(self):
        adapter = Adapter({"account_id": "12345"})
        result = adapter.build_order(command())
        self.assertEqual(result["TimeInForce"], {"Duration": "FOK"})
        self.assertEqual(result["TradeAction"], "BUY")
        self.assertEqual(result["OrderType"], "Limit")
        self.assertEqual(result["AccountID"], "12345")
        self.assertLessEqual(len(result["OrderConfirmID"]), 22)
        self.assertEqual(result["OrderConfirmID"], adapter.build_order(command())["OrderConfirmID"])
        self.assertIsNone(adapter._http)

    def test_market_payload_and_account_free_dry_run(self):
        result = Adapter({}).build_order(command(order_type="market", limit_price=None, time_in_force="day"))
        self.assertEqual(result["OrderType"], "Market")
        self.assertEqual(result["AccountID"], "<configured-account-id>")
        self.assertNotIn("LimitPrice", result)

    def test_explicit_option_and_short_intent(self):
        result = Adapter({}).build_order(command(asset_class="option", options={"trade_action": "BUYTOOPEN"}))
        self.assertEqual(result["TradeAction"], "BUYTOOPEN")
        result = Adapter({}).build_order(command(side="sell", options={"trade_action": "SELLSHORT"}))
        self.assertEqual(result["TradeAction"], "SELLSHORT")
        for fields in ({"asset_class": "option"}, {"options": {"trade_action": "SELLSHORT"}},
                       {"quantity": "0.5"}, {"asset_class": "crypto"}, {"order_type": "market"}):
            with self.subTest(fields=fields), self.assertRaises(UnsupportedOrder):
                Adapter({}).build_order(command(**fields))

    def test_both_history_and_current_paginate_and_deduplicate(self):
        adapter = Adapter({"account_id": "12345"})
        adapter._request = Mock(side_effect=[
            {"Orders": [order("historical")], "NextToken": "history-next"},
            {"Orders": [order("same"), order("old", "2026-10-24T12:00:00Z")]},
            {"Orders": [order("same", status="ACK")], "NextToken": "current-next"},
            {"Orders": [order("open", status="OPN"), order("end", "2026-10-26T00:00:00Z")]},
        ])
        result = adapter.orders_today(START, END)
        self.assertEqual({row["OrderID"] for row in result}, {"historical", "same", "open"})
        self.assertEqual(next(row for row in result if row["OrderID"] == "same")["Status"], "ACK")
        calls = adapter._request.call_args_list
        self.assertIn("historicalorders", calls[0].args[1])
        self.assertEqual(calls[0].kwargs["params"], {"pageSize": 600, "since": "2026-10-24"})
        self.assertEqual(calls[1].kwargs["params"]["nextToken"], "history-next")
        self.assertEqual(calls[3].kwargs["params"]["nextToken"], "current-next")

    def test_positions_cover_all_configured_accounts_in_batches(self):
        adapter = Adapter({"account_ids": [str(i) for i in range(23)]})
        adapter._request = Mock(side_effect=[{"Positions": [1]}, {"Positions": [2]}, {"Positions": [3]}])
        self.assertEqual(adapter.positions(), [1, 2, 3])
        self.assertEqual(adapter._request.call_count, 3)
        self.assertIn("20,21,22", adapter._request.call_args.args[1])

    def test_http_success_with_broker_errors_fails(self):
        adapter = Adapter({"access_token": "fake"})
        adapter._http = Mock()
        adapter._http.request.return_value = {"Errors": [{"Error": "Forbidden", "AccountID": "12345"}]}
        with self.assertRaises(BrokerError):
            adapter._request("GET", "/positions")

    def test_refresh_caches_access_token(self):
        adapter = Adapter({"client_id": "fake-id", "refresh_token": "fake-refresh"})
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "fake-access", "expires_in": 1200,
                                      "refresh_token": "fake-rotated"}
        with patch("requests.post", return_value=response) as post:
            self.assertEqual(adapter._token(), "fake-access")
            self.assertEqual(adapter._token(), "fake-access")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(adapter._refresh_token, "fake-rotated")

    def test_submission_needs_one_account_and_is_not_retried(self):
        with self.assertRaises(ConfigurationError):
            Adapter({}).place_order(command())
        adapter = Adapter({"account_id": "12345"})
        adapter._request = Mock(side_effect=BrokerError("timeout"))
        with self.assertRaises(BrokerError):
            adapter.place_order(command())
        self.assertEqual(adapter._request.call_count, 1)

    def test_repeating_page_token_fails_explicitly(self):
        adapter = Adapter({"account_id": "12345"})
        adapter._request = Mock(return_value={"Orders": [order("same")], "NextToken": "same-token"})
        with self.assertRaises(BrokerError):
            adapter.orders_today(START, END)

    def test_capabilities_are_network_free(self):
        adapter = Adapter({})
        self.assertIn("fok", adapter.capabilities()["time_in_force"])
        self.assertFalse(adapter.capabilities()["luld"]["auction_participation_confirmed"])
        self.assertIsNone(adapter._http)

    def test_tradability_uses_live_halt_evidence_but_not_symbol_existence(self):
        for halted, delayed, expected in [(True, False, False), (False, False, None), (True, True, None)]:
            adapter = Adapter({})
            adapter._request = Mock(side_effect=[
                {"Symbols": [{"Symbol": "AAPL", "AssetType": "STOCK"}]},
                {"Quotes": [{"Symbol": "AAPL", "MarketFlags": {"IsHalted": halted, "IsDelayed": delayed}}]},
            ])
            result = adapter.tradability("AAPL")
            self.assertIs(result["tradable"], expected)
            self.assertTrue(result["instrument_found"])
            if delayed:
                self.assertIsNone(result["halted"])

    def test_borrow_flags_do_not_invent_available_quantity_or_fee(self):
        adapter = Adapter({"account_id": "12345"})
        adapter._request = Mock(side_effect=[
            {"Quotes": [{"Symbol": "AAPL", "MarketFlags": {"IsHardToBorrow": False, "IsDelayed": False}}]},
            {"Accounts": [{"AccountID": "12345", "AccountDetail": {"IsStockLocateEligible": True}},
                          {"AccountID": "OTHER", "AccountDetail": {"IsStockLocateEligible": True}}]},
        ])
        result = adapter.borrow_availability("AAPL")
        for key in ["shortable", "easy_to_borrow", "quantity", "fee_rate", "locate_required"]:
            self.assertIsNone(result[key])
        self.assertEqual(result["account_stock_locate_eligibility"], {"12345": True})
        self.assertEqual(len(result["raw"]["selected_accounts"]), 1)

    def test_htb_flag_excludes_etb_but_does_not_establish_locate_inventory(self):
        adapter = Adapter({"account_id": "12345"})
        adapter._request = Mock(side_effect=[
            {"Quotes": [{"Symbol": "TSLA", "MarketFlags": {"IsHardToBorrow": True, "IsDelayed": False}}]},
            BrokerError("account permission denied"),
        ])
        result = adapter.borrow_availability("TSLA")
        self.assertFalse(result["easy_to_borrow"])
        self.assertTrue(result["hard_to_borrow"])
        self.assertIsNone(result["quantity"])
        self.assertIn("account_lookup_error", result)


if __name__ == "__main__":
    unittest.main()
