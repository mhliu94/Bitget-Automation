"""Crash/recovery tests for opt-in timed cancellation; all transports are mocked."""
import json
from unittest.mock import Mock, patch

import pytest

from common.errors import AuthenticationError, BrokerError, CommandError, ConfigurationError, OutcomeUnknown, UnsupportedOrder
from common.journal import Journal
from common.registry import adapter_for
from common import timed_cancel as timed


def command(broker="alpaca", **changes):
    symbol = {"bitget": "BTCUSDT", "robinhood": "BTC-USD", "longbridge": "AAPL.US"}.get(broker, "AAPL")
    return {"id": "timed-1", "action": "place_order", "symbol": symbol, "side": "buy",
            "quantity": "1", "limit_price": "1", "order_type": "limit", "time_in_force": "fok", **changes}


def adapter(broker="alpaca", **config):
    return adapter_for(broker, {"fok_policy": "timed_cancel", "cancel_confirmation_delays_seconds": [0],
                                "cancel_after_seconds": 0, **config})


@pytest.fixture
def journal(tmp_path):
    result = Journal(tmp_path / "commands.sqlite")
    yield result
    result.close()


@pytest.mark.parametrize("broker,tif", [("alpaca", "day"), ("bitget", "gtc"), ("robinhood", "gtc"),
                                      ("tradestation", "day"), ("longbridge", "day"), ("webull", "day")])
def test_prepare_explicit_conversion_is_pure_and_keeps_limit_quantity(broker, tif):
    instance = adapter(broker)
    original = command(broker)
    snapshot = json.dumps(original, sort_keys=True)
    converted, plan = timed.prepare(instance, broker, original)
    assert json.dumps(original, sort_keys=True) == snapshot
    assert converted["time_in_force"] == tif
    assert converted["limit_price"] == original["limit_price"]
    assert converted["quantity"] == original["quantity"]
    assert plan["enabled"] is True and plan["native_fok"] is False and plan["partial_fills_possible"] is True
    assert plan["client_order_id"]
    assert plan["request"] == instance.build_order(converted)


def test_native_fok_remains_default_and_unsupported_broker_does_not_silently_emulate():
    instance = adapter_for("alpaca", {})
    converted, plan = timed.prepare(instance, "alpaca", command())
    assert converted["time_in_force"] == "fok" and plan["enabled"] is False
    with pytest.raises(UnsupportedOrder):
        timed.prepare(adapter_for("webull", {}), "webull", command("webull"))


def test_non_fok_commands_and_zhuorui_do_not_use_cross_broker_workflow():
    instance = adapter("alpaca")
    converted, plan = timed.prepare(instance, "alpaca", command(time_in_force="day"))
    assert plan["enabled"] is False and converted["time_in_force"] == "day"
    zhuorui = adapter("zhuorui", allow_timed_cancel_fok=True)
    assert timed.prepare(zhuorui, "zhuorui", command())[1]["enabled"] is False


@pytest.mark.parametrize("changes", [{"order_type": "market"}, {"auction_intent": "luld_reopening"}])
def test_unsupported_timed_semantics_rejected_before_execution(changes):
    with pytest.raises(UnsupportedOrder):
        timed.prepare(adapter(), "alpaca", command(**changes))


@pytest.mark.parametrize("value", [-1, 301, "nan", True, None, "bad"])
def test_invalid_deadline_rejected_in_preview(value):
    with pytest.raises(ConfigurationError):
        timed.prepare(adapter(cancel_after_seconds=value), "alpaca", command())


def test_alpaca_crypto_uses_gtc_not_unsupported_day():
    converted, plan = timed.prepare(adapter(), "alpaca", command(symbol="BTC/USD", asset_class="crypto"))
    assert converted["time_in_force"] == "gtc" and plan["request"]["time_in_force"] == "gtc"


def test_pending_row_committed_before_place_and_confirmed_before_cancel(journal):
    instance = adapter()
    def submit(converted):
        pending = timed.unresolved(journal, "scope")
        assert pending[0]["state"] == "submission_pending" and pending[0]["client_order_id"]
        assert converted["time_in_force"] == "day"
        return {"id": "broker-id"}
    instance.place_order = Mock(side_effect=submit)
    def cancel(*args):
        pending = timed.unresolved(journal, "scope")[0]
        assert pending["state"] == "cancel_dispatching" and pending["order_id"] == "broker-id"
        return {"status": "cancel_requested"}
    with patch.object(timed, "_detail", side_effect=[({}, "new"), ({"filled_qty": "0"}, "canceled")]), \
         patch.object(timed, "_write_cancel", side_effect=cancel) as cancel_mock:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["cancellation_confirmed"] is True and result["terminal_confirmed"] is True
    assert result["native_fok"] is False
    assert timed.unresolved(journal, "scope") == []
    assert cancel_mock.call_args.args[2]["order_id"] == "broker-id"


def test_wait_is_measured_from_dispatch_not_acknowledgement(journal):
    instance = adapter(cancel_after_seconds=1)
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed.time, "monotonic", side_effect=[10, 10.4]), \
         patch.object(timed.time, "sleep") as sleep, \
         patch.object(timed, "_detail", return_value=({}, "filled")):
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert sleep.call_count == 1 and sleep.call_args.args[0] == pytest.approx(.6)
    assert result["terminal_state"] == "filled"


def test_slow_submission_does_not_wait_another_second(journal):
    instance = adapter(cancel_after_seconds=1)
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed.time, "monotonic", side_effect=[10, 12]), \
         patch.object(timed.time, "sleep") as sleep, \
         patch.object(timed, "_detail", return_value=({}, "filled")):
        timed.execute(instance, "alpaca", command(), journal, "scope")
    sleep.assert_not_called()


def test_filled_before_deadline_never_canceled(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({"filled_qty": "1"}, "filled")), \
         patch.object(timed, "_write_cancel") as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    cancel.assert_not_called()
    assert result["status"] == "no_action" and result["cancellation_confirmed"] is False
    assert result["order"]["filled_qty"] == "1"


def test_partial_fill_then_cancel_retains_actual_fill(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", side_effect=[({"filled_qty": ".5"}, "partially_filled"),
                                                    ({"filled_qty": ".5"}, "canceled")]), \
         patch.object(timed, "_write_cancel", return_value={"status": "cancel_requested"}):
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["partial_fills_possible"] is True and result["order"]["filled_qty"] == ".5"
    assert result["cancellation_confirmed"] is True


def test_acknowledgement_without_terminal_confirmation_remains_unresolved(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({}, "new")), \
         patch.object(timed, "_write_cancel", return_value={"status": "cancel_requested"}) as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
        recovery = timed.recover(instance, "alpaca", journal, "scope")
    assert result["status"] == recovery[0]["status"] == "outcome_unknown"
    assert result["cancellation_confirmed"] is False
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_requested"
    assert cancel.call_count == 1
    with pytest.raises(OutcomeUnknown, match="earlier"):
        timed.execute(instance, "alpaca", command(id="new-command"), journal, "scope")
    assert instance.place_order.call_count == 1


def test_missing_order_listing_is_not_terminal_confirmation(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", side_effect=BrokerError("not uniquely visible")), \
         patch.object(timed, "_write_cancel", return_value={"status": "no_action"}):
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["status"] == "outcome_unknown"
    assert result["terminal_confirmed"] is False
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_pending"


@pytest.mark.parametrize("raw", [None, {}, {"id": ""}, {"id": "broker", "client_order_id": "another-command"}])
def test_malformed_submit_ack_never_creates_cancel_target(journal, raw):
    instance = adapter()
    instance.place_order = Mock(return_value=raw)
    with patch.object(timed, "_write_cancel") as cancel, patch.object(timed, "_detail") as detail:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
        recovered = timed.recover(instance, "alpaca", journal, "scope")
    assert result["status"] == "outcome_unknown" and result["order_id"] is None
    assert recovered[0]["submission_confirmed"] is False
    cancel.assert_not_called()
    detail.assert_not_called()
    assert instance.place_order.call_count == 1


def test_unknown_submission_and_duplicate_never_replay(journal):
    instance = adapter()
    instance.place_order = Mock(side_effect=OutcomeUnknown("network timeout"))
    assert timed.execute(instance, "alpaca", command(), journal, "scope")["status"] == "outcome_unknown"
    with pytest.raises(OutcomeUnknown, match="already dispatched"):
        timed.execute(instance, "alpaca", command(), journal, "scope")
    timed.recover(instance, "alpaca", journal, "scope")
    assert instance.place_order.call_count == 1


def test_crash_after_submission_before_saving_reference_stays_unresolved(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_save", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            timed.execute(instance, "alpaca", command(), journal, "scope")
    assert timed.unresolved(journal, "scope")[0]["state"] == "submission_pending"
    with patch.object(timed, "_write_cancel") as cancel, patch.object(timed, "_detail") as detail:
        assert timed.recover(instance, "alpaca", journal, "scope")[0]["status"] == "outcome_unknown"
    cancel.assert_not_called()
    detail.assert_not_called()
    assert instance.place_order.call_count == 1


def test_crash_after_saving_reference_recovery_cancels_only_that_reference(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_finish_pending", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            timed.execute(instance, "alpaca", command(), journal, "scope")
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_pending"
    with patch.object(timed, "_detail", side_effect=[({}, "new"), ({}, "canceled")]), \
         patch.object(timed, "_write_cancel", return_value={"status": "cancel_requested"}) as cancel:
        result = timed.recover(instance, "alpaca", journal, "scope")
    assert result[0]["cancellation_confirmed"] is True
    assert cancel.call_args.args[2]["order_id"] == "broker"
    assert instance.place_order.call_count == 1


def test_crash_during_cancel_never_resends_cancel_but_can_read_terminal(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({}, "new")), \
         patch.object(timed, "_write_cancel", side_effect=KeyboardInterrupt) as cancel:
        with pytest.raises(KeyboardInterrupt):
            timed.execute(instance, "alpaca", command(), journal, "scope")
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_dispatching"
    with patch.object(timed, "_detail", return_value=({}, "canceled")), patch.object(timed, "_write_cancel") as again:
        result = timed.recover(instance, "alpaca", journal, "scope")
    assert result[0]["cancellation_confirmed"] is True
    again.assert_not_called()


def test_recovery_is_account_scope_specific(journal):
    instance = adapter()
    instance.place_order = Mock(side_effect=OutcomeUnknown("timeout"))
    timed.execute(instance, "alpaca", command(), journal, "one")
    assert timed.recover(instance, "alpaca", journal, "two") == []
    with pytest.raises(ConfigurationError, match="broker"):
        timed.recover(instance, "bitget", journal, "one")


def test_completed_duplicate_returns_saved_result_without_any_write(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({}, "filled")):
        original = timed.execute(instance, "alpaca", command(), journal, "scope")
        duplicate = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert duplicate == {**original, "duplicate": True}
    assert instance.place_order.call_count == 1
    with pytest.raises(CommandError):
        timed.execute(instance, "alpaca", command(quantity="2"), journal, "scope")


@pytest.mark.parametrize("broker,raw,target", [
    ("alpaca", {"id": "a"}, "a"), ("bitget", {"orderId": "b"}, "b"),
    ("robinhood", {"id": "r", "account_number": "mine"}, "r"),
    ("tradestation", {"Orders": [{"OrderID": "t"}]}, "t"),
    ("longbridge", {"order_id": "l"}, "l"), ("webull", {"order_id": "w"}, "w")])
def test_each_broker_submission_reference_extraction(broker, raw, target):
    instance = adapter(broker, account_number="mine")
    _, plan = timed.prepare(instance, broker, command(broker))
    assert timed._ack_reference(broker, raw, plan, instance) == target


def test_webull_confirmed_client_ref_works_when_broker_id_not_returned():
    instance = adapter("webull")
    _, plan = timed.prepare(instance, "webull", command("webull"))
    assert timed._ack_reference("webull", {"client_order_id": plan["client_order_id"]}, plan, instance) == plan["client_order_id"]


def test_robinhood_detail_and_ack_cannot_cross_accounts():
    instance = adapter("robinhood", account_number="mine")
    _, plan = timed.prepare(instance, "robinhood", command("robinhood"))
    with pytest.raises(OutcomeUnknown, match="another account"):
        timed._ack_reference("robinhood", {"id": "r", "account_number": "other"}, plan, instance)
    instance._request = Mock(return_value={"id": "r", "account_number": "other", "state": "canceled"})
    with pytest.raises(BrokerError, match="account"):
        timed._detail(instance, "robinhood", {"order_id": "r"})


def test_webull_detail_matches_child_client_and_broker_identity():
    instance = adapter("webull", account_id="mine")
    instance._trade = Mock()
    instance._call = Mock(return_value={"orders": [{"order_id": "broker", "client_order_id": "client", "status": "CANCELLED"}]})
    _, plan = timed.prepare(instance, "webull", command("webull"))
    record = {"order_id": "broker", "client_order_id": "client", "request": plan["request"]}
    instance._call.return_value["orders"][0].update(symbol="AAPL", side="BUY", order_type="LIMIT", total_quantity="1", limit_price="1")
    row, state = timed._detail(instance, "webull", record)
    assert state == "CANCELLED"
    instance._call.assert_called_once_with(instance._trade.order_v3.get_order_detail, account_id="mine", client_order_id="client")
    with pytest.raises(BrokerError, match="different"):
        timed._detail(instance, "webull", {**record, "order_id": "other"})


def test_alpaca_configured_crypto_also_uses_gtc():
    converted, _ = timed.prepare(adapter(asset_class="crypto"), "alpaca", command(symbol="BTCUSD"))
    assert converted["time_in_force"] == "gtc"


def test_authentication_rejection_before_acceptance_is_safe_and_retryable(journal):
    instance = adapter()
    instance.place_order = Mock(side_effect=[AuthenticationError("unauthorized", no_submission=True), {"id": "broker"}])
    with pytest.raises(AuthenticationError) as error:
        timed.execute(instance, "alpaca", command(), journal, "scope")
    assert error.value.no_submission is True
    assert timed.unresolved(journal, "scope") == []
    assert timed.recover(instance, "alpaca", journal, "scope") == []
    with patch.object(timed, "_detail", return_value=({}, "filled")):
        assert timed.execute(instance, "alpaca", command(), journal, "scope")["terminal_confirmed"] is True
    assert instance.place_order.call_count == 2


def test_authentication_error_without_zero_acceptance_proof_is_unresolved(journal):
    instance = adapter()
    instance.place_order = Mock(side_effect=AuthenticationError("uncertain auth"))
    result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["status"] == "outcome_unknown" and result["authentication_error"] is True
    assert len(timed.unresolved(journal, "scope")) == 1


def test_authentication_failure_after_submit_never_releases_submission(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", side_effect=AuthenticationError("read unauthorized", no_submission=True)), \
         patch.object(timed, "_write_cancel") as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["status"] == "outcome_unknown" and result["authentication_error"] is True
    assert result["submission_confirmed"] is True
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_pending"
    cancel.assert_not_called()


def test_authentication_failure_during_cancel_never_replays_after_repair(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({}, "new")), \
         patch.object(timed, "_write_cancel", side_effect=AuthenticationError("cancel unauthorized", no_submission=True)) as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
        recovered = timed.recover(instance, "alpaca", journal, "scope")
    assert result["status"] == recovered[0]["status"] == "outcome_unknown"
    assert result["authentication_error"] is True and result["submission_confirmed"] is True
    assert cancel.call_count == 1


def test_recovery_can_cancel_when_exact_detail_was_temporarily_missing(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", side_effect=BrokerError("not visible")), patch.object(timed, "_write_cancel") as cancel:
        first = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert first["status"] == "outcome_unknown"
    cancel.assert_not_called()
    with patch.object(timed, "_detail", side_effect=[({}, "new"), ({}, "canceled")]), \
         patch.object(timed, "_write_cancel", return_value=None) as cancel:
        result = timed.recover(instance, "alpaca", journal, "scope")
    assert result[0]["cancellation_confirmed"] is True
    assert cancel.call_count == 1 and instance.place_order.call_count == 1


def test_unknown_state_before_dispatch_keeps_cancel_pending_not_ambiguous(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", return_value=({}, "NEW_FUTURE_STATE")), patch.object(timed, "_write_cancel") as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["status"] == "outcome_unknown"
    assert timed.unresolved(journal, "scope")[0]["state"] == "cancel_pending"
    cancel.assert_not_called()


def test_existing_pending_cancel_is_observed_without_another_cancel(journal):
    instance = adapter()
    instance.place_order = Mock(return_value={"id": "broker"})
    with patch.object(timed, "_detail", side_effect=[({}, "pending_cancel"), ({}, "canceled")]), \
         patch.object(timed, "_write_cancel") as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["cancellation_confirmed"] is True
    cancel.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"symbol": "MSFT"}, {"side": "sell"}, {"type": "market"},
    {"qty": "2"}, {"limit_price": "1.01"}, {"qty": "NaN"}, {"client_order_id": "different"}])
def test_exact_id_with_wrong_ticket_is_never_cancelled(journal, changes):
    instance = adapter()
    instance._http = Mock()
    instance.place_order = Mock(return_value={"id": "broker"})
    instance.http.request.return_value = {"id": "broker", "symbol": "AAPL", "side": "buy", "type": "limit",
                                         "qty": "1", "limit_price": "1", "status": "new", **changes}
    with patch.object(timed, "_write_cancel") as cancel:
        result = timed.execute(instance, "alpaca", command(), journal, "scope")
    assert result["status"] == "outcome_unknown" and timed.unresolved(journal, "scope")[0]["state"] == "cancel_pending"
    cancel.assert_not_called()


def test_webull_uses_verified_detail_without_waiting_for_open_list(journal):
    instance = adapter("webull", account_id="mine")
    instance._trade = Mock()
    _, plan = timed.prepare(instance, "webull", command("webull"))
    ticket = {"order_id": "broker", "client_order_id": plan["client_order_id"], "symbol": "AAPL", "side": "BUY",
              "order_type": "LIMIT", "total_quantity": "1", "limit_price": "1", "status": "SUBMITTED"}
    instance.place_order = Mock(return_value={"order_id": "broker", "client_order_id": plan["client_order_id"]})
    instance._pages = Mock(side_effect=AssertionError("Open listing must not be consulted"))
    instance._call = Mock(side_effect=[{"orders": [ticket]}, {"order_id": "broker", "client_order_id": plan["client_order_id"]},
                                     {"orders": [{**ticket, "status": "CANCELLED"}]}])
    result = timed.execute(instance, "webull", command("webull"), journal, "scope")
    assert result["cancellation_confirmed"] is True
    instance._pages.assert_not_called()
    write = instance._call.call_args_list[1]
    assert write.args[0] == instance._trade.order_v3.cancel_order
    assert write.kwargs == {"mutation": True, "account_id": "mine", "client_order_id": plan["client_order_id"]}


@pytest.mark.parametrize("action,returned", [("BUYTOCOVER", "BuyToCover"), ("SELLSHORT", "SellShort")])
def test_tradestation_explicit_trade_actions_match_valid_detail(action, returned):
    instance = adapter("tradestation", account_id="mine")
    cmd = command("tradestation", side="buy" if action.startswith("BUY") else "sell", options={"trade_action": action})
    _, plan = timed.prepare(instance, "tradestation", cmd)
    row = {"OrderID": "t", "AccountID": "mine", "OrderType": "Limit", "LimitPrice": "1", "Status": "ACK",
           "Legs": [{"Symbol": "AAPL", "QuantityOrdered": "1", "BuyOrSell": returned}]}
    instance._request = Mock(return_value={"Orders": [row]})
    assert timed._detail(instance, "tradestation", {"order_id": "t", "client_order_id": plan["client_order_id"], "request": plan["request"]}) == (row, "ACK")
