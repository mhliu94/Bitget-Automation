import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import requests

from common.errors import BrokerError, ConfigurationError, OutcomeUnknown, UnsupportedOrder
from common.registry import adapter_for


START = datetime(2026, 10, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 26, tzinfo=timezone.utc)
CONFIG = {"app_key": "test-key", "app_secret": "test-secret", "access_token": "test-token"}


def command(**changes):
    return {**{"id": "lb-1", "symbol": "AAPL.US", "side": "buy", "quantity": "2",
              "order_type": "limit", "time_in_force": "day", "limit_price": "123.45"}, **changes}


def order(order_id, stamp=None, status="FilledStatus"):
    return {"order_id": order_id, "submitted_at": str(stamp or int(START.timestamp()) + 100), "status": status}


class LongbridgeTests(unittest.TestCase):
    def adapter(self, config=None):
        return adapter_for("longbridge", config or {})

    def test_dry_run_uses_no_credentials_and_preserves_decimal(self):
        adapter = self.adapter()
        result = adapter.build_order(command(quantity="0.12345678"))
        self.assertEqual(result["submitted_quantity"], "0.12345678")
        self.assertEqual(result["order_type"], "LO")
        self.assertEqual(result["client_request_id"], adapter.build_order(command())["client_request_id"])
        self.assertIsNone(adapter._http)

    def test_market_and_unsupported_semantics(self):
        result = self.adapter().build_order(command(order_type="market", limit_price=None))
        self.assertEqual(result["order_type"], "MO")
        self.assertNotIn("submitted_price", result)
        for update in ({"time_in_force": "fok"}, {"time_in_force": "ioc"},
                       {"symbol": "AAPL"}, {"asset_class": "crypto"},
                       {"options": {"order_type": "SLO"}},
                       {"order_type": "market", "limit_price": None, "options": {"outside_rth": "ANY_TIME"}}):
            with self.subTest(update=update), self.assertRaises(UnsupportedOrder):
                self.adapter().build_order(command(**update))

    def test_history_splits_capped_ranges_and_merges_today(self):
        adapter = self.adapter()
        adapter._request = Mock(side_effect=[
            {"has_more": True, "orders": [order("truncated")]},
            {"has_more": False, "orders": [order("a"), order("duplicate")]},
            {"has_more": False, "orders": [order("b"), order("end", int(END.timestamp()))]},
            {"orders": [order("duplicate", status="NewStatus"), order("old", int(START.timestamp()) - 1)]},
        ])
        result = adapter.orders_today(START, END)
        self.assertEqual({row["order_id"] for row in result}, {"a", "b", "duplicate"})
        self.assertEqual(next(row["status"] for row in result if row["order_id"] == "duplicate"), "NewStatus")
        left = parse_qs(urlsplit(adapter._request.call_args_list[1].args[1]).query)
        right = parse_qs(urlsplit(adapter._request.call_args_list[2].args[1]).query)
        self.assertEqual(int(left["end_at"][0]) + 1, int(right["start_at"][0]))

    def test_capped_single_second_fails_instead_of_truncating(self):
        adapter = self.adapter()
        adapter._request = Mock(return_value={"has_more": True, "orders": []})
        with self.assertRaises(BrokerError):
            adapter._history(100, 100)

    def test_positions_query_stocks_and_funds_without_symbol_filter(self):
        adapter = self.adapter()
        adapter._request = Mock(side_effect=[{"list": ["stock"]}, {"list": ["fund"]}])
        result = adapter.positions()
        self.assertEqual(result, {"stocks": {"list": ["stock"]}, "funds": {"list": ["fund"]}})
        self.assertEqual(adapter._request.call_args_list[0].args, ("get", "/v1/asset/stock"))

    def test_signature_tracks_exact_prepared_body_and_query(self):
        adapter = self.adapter(CONFIG)
        request = adapter._prepare_request("post", "/v1/trade/order?x=a%20b", {"remark": "HK 港"}, 1700000000)
        self.assertEqual(request.headers["Authorization"], "test-token")
        self.assertEqual(request.body, b'{"remark": "HK \\u6e2f"}')
        import hashlib
        import hmac
        canonical = ("POST|/v1/trade/order|x=a%20b|authorization:test-token\n"
                     "x-api-key:test-key\nx-timestamp:1700000000\n|authorization;x-api-key;x-timestamp|"
                     + hashlib.sha1(request.body).hexdigest())
        expected = hmac.new(b"test-secret", ("HMAC-SHA256|" + hashlib.sha1(canonical.encode()).hexdigest()).encode(), hashlib.sha256).hexdigest()
        self.assertTrue(request.headers["X-Api-Signature"].endswith(expected))

    def test_write_timeout_is_not_retried(self):
        adapter = self.adapter(CONFIG)
        adapter._http = Mock()
        adapter._http.send.side_effect = requests.Timeout("secret should not appear")
        with self.assertRaises(OutcomeUnknown) as error:
            adapter.place_order(command())
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(adapter._http.send.call_count, 1)
        self.assertFalse(adapter._http.send.call_args.kwargs["allow_redirects"])

    def test_account_scope_is_explicit(self):
        with self.assertRaises(ConfigurationError):
            self.adapter({**CONFIG, "data_center": "us"})._client()

    def test_extended_alias_and_luld_session_are_consistent(self):
        result = self.adapter().build_order(command(options={"extended_hours": True}))
        self.assertEqual(result["outside_rth"], "ANY_TIME")
        with self.assertRaises(UnsupportedOrder):
            self.adapter().build_order(command(auction_intent="luld_reopening", options={"outside_rth": "ANY_TIME"}))

    def test_quote_normal_does_not_claim_account_tradability(self):
        adapter = self.adapter()
        adapter._request = Mock(side_effect=[{}, {"secu_quote": [{"symbol": "AAPL.US", "trade_status": 0}]}])
        result = adapter.tradability("AAPL.US")
        self.assertIsNone(result["tradable"])
        self.assertFalse(result["halted"])

    def test_fuse_marks_halt_and_missing_status_stays_unknown(self):
        for status, halted, tradable in ((3, True, False), (None, None, None)):
            adapter = self.adapter()
            adapter._request = Mock(side_effect=[{}, {"secu_quote": [{"symbol": "AAPL.US", "trade_status": status}]}])
            result = adapter.tradability("AAPL.US")
            self.assertEqual(result["halted"], halted)
            self.assertEqual(result["tradable"], tradable)

    def test_sell_capacity_is_not_reported_as_borrow_inventory(self):
        adapter = self.adapter()
        adapter._request = Mock(side_effect=[{"im_factor": "0.5"}, {"cash_max_qty": "20", "margin_max_qty": "200"}])
        result = adapter.borrow_availability("AAPL.US")
        for key in ("shortable", "easy_to_borrow", "available_quantity", "fee_rate", "locate_required"):
            self.assertIsNone(result[key])
        self.assertEqual(result["raw"]["sell_capacity_estimate"]["margin_max_qty"], "200")
        self.assertFalse(result["inventory_confirmed"])
        params = parse_qs(urlsplit(adapter._request.call_args.args[1]).query)
        self.assertEqual(params, {"symbol": ["AAPL.US"], "order_type": ["MO"], "side": ["Sell"]})
        self.assertTrue(all(call.args[0] == "get" for call in adapter._request.call_args_list))

    def test_hk_borrow_query_does_not_assume_us_short_estimate(self):
        adapter = self.adapter()
        adapter._request = Mock(return_value={"im_factor": "0.5"})
        result = adapter.borrow_availability("700.HK")
        self.assertIsNone(result["raw"]["sell_capacity_estimate"])
        self.assertEqual(adapter._request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
