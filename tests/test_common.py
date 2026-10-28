from datetime import datetime, timezone
import json
from unittest.mock import Mock
import pytest

from common.commands import decode_command, normalize, validate_age
from common.config import resolve_env, load_config
from common.errors import BrokerError, CommandError, ConfigurationError, OutcomeUnknown
from common.journal import Journal
from common.runner import Runner
from common.utils import today_window, decimal_string


def order(**overrides):
    return {"id": "example-1", "type": "LIMIT_ORDER_FOK", "symbol": "AAPL", "side": "buy",
            "qty_shares": "2", "limit_price": "10.20", **overrides}


def test_ktrader_contract():
    command = normalize(order(account_num_id=7), "alpaca", {"routing": {"account_num_id": 7}}, require_account=True)
    assert command["quantity"] == "2"
    assert command["limit_price"] == "10.2"
    assert command["order_type"] == "limit" and command["time_in_force"] == "fok"


@pytest.mark.parametrize("payload", ['{"id":1,"id":2}', '{"quantity":NaN}', '[]', '{bad}', '{"a":Infinity}'])
def test_bad_json(payload):
    with pytest.raises(CommandError):
        decode_command(payload)


def test_json_decimal_not_float():
    assert str(decode_command('{"price":1.00000000000000000001}')["price"]) == "1.00000000000000000001"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", True, None, "1e1000"])
def test_invalid_amount(value):
    with pytest.raises(CommandError):
        decimal_string(value)


def test_conflicting_aliases():
    with pytest.raises(CommandError):
        normalize(order(qty="3"), "alpaca", {})
    with pytest.raises(CommandError):
        normalize(order(time_in_force="day"), "alpaca", {})
    with pytest.raises(CommandError):
        normalize(order(action="positions"), "alpaca", {})


def test_all_routing_selectors_must_match():
    config = {"routing": {"account_id": "ACC", "account_num_id": 7, "server_id": "s1"}}
    assert normalize(order(account_id="ACC", account_num_id=8), "alpaca", config) is None
    assert normalize(order(account_id="ACC", serverId="s2"), "alpaca", config) is None
    assert normalize(order(broker="webull"), "alpaca", config) is None
    with pytest.raises(CommandError):
        normalize(order(), "alpaca", config, require_account=True)
    assert normalize(order(account_id="7", accountNumId=7, serverId="s1"), "alpaca", config)["broker"] == "alpaca"


def test_query_aliases_and_tradability():
    assert normalize({"id": "p", "type": "QUERY_POSITIONS"}, "alpaca", {})["action"] == "positions"
    assert normalize({"id": "p", "action": "TEST_TRADABLE", "ticker": "aapl"}, "alpaca", {})["symbol"] == "AAPL"
    with pytest.raises(CommandError):
        normalize({"id": "p", "action": "tradability"}, "alpaca", {})


def test_kafka_identity_fallback():
    payload = order()
    del payload["id"]
    assert normalize(payload, "alpaca", {}, message_id="trading-commands:0:9000")["id"] == "trading-commands:0:9000"


def test_market_price_and_notional_are_not_silently_ignored():
    with pytest.raises(CommandError):
        normalize(order(type="MARKET_ORDER"), "alpaca", {})
    with pytest.raises(CommandError):
        normalize(order(notional_usd=200), "alpaca", {})


def test_luld_requires_day_limit_us_equity():
    with pytest.raises(CommandError):
        normalize(order(auction_intent="luld_reopening"), "alpaca", {})
    command = normalize(order(type="LIMIT_ORDER", asset_class="us_equity", auction_intent="luld_reopening"), "alpaca", {})
    assert command["time_in_force"] == "day"
    assert normalize(order(type="LIMIT_ORDER", time_in_force="day", auction_intent="luld_reopening"), "robinhood", {})["asset_class"] == "us_equity"
    with pytest.raises(CommandError):
        normalize(order(type="LIMIT_ORDER", extended_hours=True, auction_intent="luld_reopening"), "alpaca", {})
    command = normalize(order(type="LIMIT_ORDER", asset_class="us_equity", auction_intent="luld_reopening"), "zhuorui", {})
    assert command["options"]["allow_pre_post"] is False


def test_timestamps_stale_expired_future():
    now = 1_800_000_000
    for payload, stamp in [({}, (now-121)*1000), ({"expires_at": now-1}, now*1000), ({"issued_at": now+61}, now*1000)]:
        with pytest.raises(CommandError):
            validate_age(payload, stamp, now=now)
    validate_age({"issued_at": now}, now*1000, now=now)


def test_dst_calendar_day_bounds():
    start, end = today_window("America/New_York", datetime(2026, 3, 8, 12, tzinfo=timezone.utc))
    assert (end-start).total_seconds() == 23*3600
    start, end = today_window("America/New_York", datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    assert (end-start).total_seconds() == 25*3600


def test_journal_duplicate_conflict_and_unknown(tmp_path):
    path = tmp_path / "state.sqlite3"
    journal = Journal(path)
    cmd = {"id": "one", "quantity": "1"}
    assert journal.reserve("alpaca:account1", cmd) is None
    with pytest.raises(OutcomeUnknown):
        journal.reserve("alpaca:account1", cmd)
    journal.finish("alpaca:account1", "one", {"status": "submitted", "data": {"order_id": "x"}})
    journal.close()
    journal = Journal(path)
    assert journal.reserve("alpaca:account1", cmd)["data"]["order_id"] == "x"
    with pytest.raises(CommandError):
        journal.reserve("alpaca:account1", {**cmd, "quantity": "2"})
    assert journal.reserve("alpaca:account2", cmd) is None
    journal.close()


def test_dry_run_and_live_idempotency(tmp_path, monkeypatch):
    adapter = Mock()
    adapter.build_order.return_value = {"symbol": "AAPL", "time_in_force": "fok"}
    adapter.place_order.return_value = {"order_id": "native-1"}
    monkeypatch.setattr("common.runner.adapter_for", lambda *_: adapter)
    preview = Runner("alpaca", {}, False)
    assert preview.process(order())["status"] == "dry_run"
    adapter.place_order.assert_not_called()
    live = Runner("alpaca", {"trading_enabled": True, "journal_account": "paper-test"}, True, tmp_path/"state.sqlite3")
    assert live.process(order())["status"] == "submitted"
    assert live.process(order())["duplicate"] is True
    adapter.place_order.assert_called_once()
    live.close()


def test_network_ambiguity_never_retries(tmp_path, monkeypatch):
    adapter = Mock()
    adapter.place_order.side_effect = TimeoutError("may have been accepted")
    monkeypatch.setattr("common.runner.adapter_for", lambda *_: adapter)
    live = Runner("alpaca", {"trading_enabled": True, "journal_account": "paper-test"}, True, tmp_path/"state.sqlite3")
    assert live.process(order())["status"] == "unknown"
    assert live.process(order())["duplicate"] is True
    adapter.place_order.assert_called_once()
    live.close()


def test_only_selected_broker_env_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("SELECTED_KEY", "test")
    path = tmp_path/"config.json"
    path.write_text(json.dumps({"brokers": {"alpaca": {"api_key": "${SELECTED_KEY}"}, "other": {"key": "${MISSING_KEY}"}}}))
    assert load_config(path, "alpaca", True)[1]["api_key"] == "test"
    with pytest.raises(ConfigurationError):
        resolve_env("${MISSING_KEY}")


def test_read_only_queries_are_fresh_and_borrow_fields_consistent(tmp_path, monkeypatch):
    adapter = Mock()
    adapter.borrow_availability.side_effect = [{"quantity": "100", "shortable": True}, {"quantity": "75", "shortable": True}]
    monkeypatch.setattr("common.runner.adapter_for", lambda *_: adapter)
    live = Runner("alpaca", {}, True, tmp_path/"state.sqlite3")
    payload = {"id": "borrow-1", "action": "QUERY_BORROW_AVAILABILITY", "symbol": "AAPL"}
    assert live.process(payload)["data"]["available_quantity"] == "100"
    second = live.process(payload)["data"]
    assert second["available_quantity"] == "75" and second["quantity_unit"] == "shares"
    assert second["fee_rate"] is None and second["inventory_reserved"] is False
    live.close()
