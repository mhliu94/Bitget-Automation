from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

from robinhood.adapter import Adapter
from common.errors import BrokerError, UnsupportedOrder


def command(**changes):
    result = dict(id="rh-order-1", broker="robinhood", action="place_order", symbol="BTC-USD",
                  side="buy", quantity="0.001", order_type="limit", time_in_force="gtc", limit_price="10000")
    result.update(changes)
    return result


def test_limit_day_and_idempotency():
    result = Adapter({}).build_order(command(time_in_force="day"))
    assert result["limit_order_config"] == {"asset_quantity": "0.001", "limit_price": "10000", "time_in_force": "gfd"}
    assert str(uuid.UUID(result["client_order_id"])) == result["client_order_id"]
    assert result == Adapter({}).build_order(command(time_in_force="day"))


@pytest.mark.parametrize("tif", ["fok", "ioc"])
def test_robinhood_does_not_invent_fok_or_ioc(tif):
    with pytest.raises(UnsupportedOrder):
        Adapter({}).build_order(command(time_in_force=tif))


def test_equities_rejected_and_market_has_no_tif_field():
    with pytest.raises(UnsupportedOrder, match="crypto"):
        Adapter({}).build_order(command(asset_class="equity", symbol="AAPL"))
    body = Adapter({}).build_order(command(order_type="market", limit_price=None))
    assert body["market_order_config"] == {"asset_quantity": "0.001"}


def test_ed25519_signature_with_documented_public_test_key():
    # Public documentation test vector; this is not an account credential.
    body = '{"client_order_id":"131de903-5a9c-4260-abc1-28d562a5dcf0","side":"buy","type":"market","symbol":"BTC-USD","market_order_config":{"asset_quantity":"0.1"}}'
    result = Adapter.signature("xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU=",
                               "rh-api-6148effc-c0b1-486c-8940-a1d099456be6", "1698708981",
                               "POST", "/api/v1/crypto/trading/orders/", body)
    import base64
    from nacl.signing import VerifyKey
    message = ("rh-api-6148effc-c0b1-486c-8940-a1d099456be6" + "1698708981"
               + "/api/v1/crypto/trading/orders/" + "POST" + body).encode()
    verified = VerifyKey(base64.b64decode("jPItx4TLjcnSUnmnXQQyAKL4eJj3+oWNNMmmm2vATqk=")).verify(message, base64.b64decode(result))
    assert verified == message


def test_post_has_account_query_and_signed_body():
    adapter = Adapter({"api_key": "test", "private_key_base64": "unused", "account_number": "12345"})
    response = Mock(status_code=201)
    response.json.return_value = {"id": "server-id"}
    with patch("requests.request", return_value=response) as request, patch.object(adapter, "signature", return_value="signature") as sign:
        adapter.place_order(command())
    url = request.call_args.args[1]
    assert parse_qs(urlsplit(url).query) == {"account_number": ["12345"]}
    assert sign.call_args.args[4].endswith("orders/?account_number=12345")
    assert sign.call_args.args[5] == request.call_args.kwargs["data"]


def test_orders_all_pages_and_exclusive_midnight_boundary():
    start = datetime(2026, 10, 25, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    adapter = Adapter({"account_number": "123"})
    adapter._request = Mock(side_effect=[
        {"results": [{"id": "a", "created_at": "2026-10-25T00:00:00Z"}], "next": "https://trading.robinhood.com/api/v2/crypto/trading/orders/?cursor=abc"},
        {"results": [{"id": "b", "created_at": "2026-10-25T23:00:00Z"}, {"id": "c", "created_at": "2026-10-26T00:00:00Z"}], "next": None},
    ])
    assert [row["id"] for row in adapter.orders_today(start, end)] == ["a", "b"]
    assert adapter._request.call_args.args[2]["account_number"] == "123"
    assert adapter._request.call_args.args[2]["cursor"] == "abc"
    assert "created_at_start" in adapter._request.call_args.args[2]


def test_holdings_paginate_with_plain_cursor():
    adapter = Adapter({"account_number": "123"})
    adapter._request = Mock(side_effect=[{"results": [{"asset_code": "BTC"}], "next": "next-token"},
                                         {"results": [{"asset_code": "ETH"}], "next": None}])
    assert len(adapter.positions()) == 2


def test_pagination_must_stay_on_origin():
    adapter = Adapter({"account_number": "123"})
    adapter._request = Mock(return_value={"results": [], "next": "https://evil.example/api/v2/crypto/trading/holdings/?cursor=x"})
    with pytest.raises(BrokerError, match="origin"):
        adapter.positions()


def test_tradability_respects_sell_only_and_api_flag():
    adapter = Adapter({})
    adapter._request = Mock(return_value={"results": [{"symbol": "BTC-USD", "status": "sellonly", "is_api_tradable": True}], "next": None})
    result = adapter.tradability("BTC-USD")
    assert result["buy_tradable"] is False
    assert result["sell_tradable"] is True
    assert result["tradable"] is None
    assert result["halted"] is None


def test_borrow_information_is_explicitly_unknown_in_crypto_api():
    result = Adapter({}).borrow_availability("AAPL")
    assert result["supported"] is False
    assert result["shortable"] is None
    assert result["available_quantity"] is None
