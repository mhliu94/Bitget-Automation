import pytest
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
    return {"order_id": order_id, "submitted_at": str(stamp or int(START.timestamp()) + 100), "status": status, "symbol": "AAPL.US"}


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
                       {"symbol": "700.HK"}, {"asset_class": "crypto"},
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

    def test_positions_query_us_stocks_and_do_not_fetch_funds(self):
        adapter = self.adapter()
        adapter._request = Mock(return_value={"list": [{"account_channel": "lb", "stock_info": [
            {"symbol": "AAPL.US", "market": "US"}, {"symbol": "700.HK", "market": "HK"}]}]})
        result = adapter.positions()
        self.assertEqual(result["stocks"]["list"][0]["stock_info"], [{"symbol": "AAPL.US", "market": "US"}])
        self.assertEqual(result["funds"], {"list": []})
        adapter._request.assert_called_once_with("get", "/v1/asset/stock")

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

    def test_hk_borrow_query_is_rejected_before_network(self):
        adapter = self.adapter()
        adapter._request = Mock()
        with self.assertRaises(UnsupportedOrder):
            adapter.borrow_availability("700.HK")
        adapter._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("ack", [None, [], {}, {"order_id": ""}, {"order_id": True}, {"order_id": 0},
                                  {"order_id": -1}, {"order_id": "bad\x7f"}, {"order_id": 683615454870679600.0},
                                  {"order_id": "one", "client_request_id": "wrong"},
                                  {"order_id": "one", "error": "rejected"}])
def test_submission_requires_exact_matching_broker_order_id(ack):
    adapter = adapter_for("longbridge", {})
    adapter._request = Mock(return_value=ack)
    with pytest.raises(OutcomeUnknown):
        adapter.place_order(command())
    adapter._request.assert_called_once()


def test_exact_integer_id_is_preserved_in_placement_and_native_nested_reads():
    adapter = adapter_for("longbridge", CONFIG)
    response = Mock(status_code=200)
    identity = 683615454870679601
    response.json.return_value = {"code": 0, "data": {"order_id": identity}}
    adapter._http = Mock()
    adapter._http.send.return_value = response
    assert adapter.place_order(command())["order_id"] == str(identity)
    response.json.return_value = {"code": 0, "data": {"order_id": identity, "orders": [{"order_id": identity + 1}]}}
    detail = adapter._request("get", "/v1/trade/order?order_id=" + str(identity))
    assert detail["order_id"] == str(identity)
    assert detail["orders"][0]["order_id"] == str(identity + 1)
    response.json.return_value = {"code": 0, "data": {"orders": [{"order_id": identity + 2}]}}
    assert adapter._request("get", "/v1/trade/order/history")["orders"][0]["order_id"] == str(identity + 2)
    response.json.return_value = {"code": 0, "data": {"order_id": float(identity)}}
    with pytest.raises(OutcomeUnknown):
        adapter.place_order(command())
    with pytest.raises(BrokerError):
        adapter._request("get", "/v1/trade/order/history")
