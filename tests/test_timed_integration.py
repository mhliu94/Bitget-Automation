"""Public Runner integration: previews, late recovery and auth never replay trades."""
from unittest.mock import Mock, patch
import pytest
from common.runner import Runner
from common.errors import AuthenticationError, BrokerError, ConfigurationError
from common import timed_cancel


def config():
    return {"trading_enabled": True, "journal_account": "paper", "fok_policy": "timed_cancel",
            "cancel_after_seconds": 0, "cancel_confirmation_delays_seconds": [0]}


def command():
    return {"id": "timed-root", "type": "LIMIT_ORDER_FOK", "symbol": "AAPL", "side": "buy", "quantity": "1", "limit_price": "10"}


def test_public_runner_preview_exposes_actual_day_order():
    runner = Runner("alpaca", config(), False)
    result = runner.process(command())
    assert result["status"] == "dry_run"
    assert result["request"]["time_in_force"] == "day"
    assert result["execution_plan"]["native_fok"] is False
    assert result["execution_plan"]["partial_fills_possible"] is True


def test_public_runner_confirmed_cancel_and_duplicate(tmp_path):
    runner = Runner("alpaca", config(), True, tmp_path/"state.sqlite3")
    runner.adapter.place_order = Mock(return_value={"id": "broker-1"})
    with patch.object(timed_cancel, "_detail", side_effect=[({}, "new"), ({"filled_qty":"0.5"}, "canceled")]), patch.object(timed_cancel, "_write_cancel", return_value=None) as cancel:
        result = runner.process(command())
        duplicate = runner.process(command())
    assert result["status"] == "submitted" and result["data"]["cancellation_confirmed"] is True
    assert duplicate["duplicate"] is True
    runner.adapter.place_order.assert_called_once();cancel.assert_called_once()
    assert runner.trading_enabled() is True
    runner.close()


def test_recovery_completes_outer_journal_without_second_submission_or_cancel(tmp_path):
    runner = Runner("alpaca", config(), True, tmp_path/"state.sqlite3")
    runner.adapter.place_order = Mock(return_value={"id": "broker-1"})
    with patch.object(timed_cancel, "_detail", return_value=({}, "new")), patch.object(timed_cancel, "_write_cancel", return_value=None) as cancel:
        assert runner.process(command())["status"] == "unknown"
        assert runner.trading_enabled() is False
        with pytest.raises(ConfigurationError): runner.process({**command(), "id":"another"})
        with patch.object(timed_cancel, "_detail", return_value=({}, "canceled")):
            recovered = runner.recover_pending()
    assert recovered["status"] == "ok"
    assert runner.journal.unresolved(runner.scope) == []
    assert runner.unresolved_timed() == []
    assert runner.process(command())["status"] == "reconciled"
    runner.adapter.place_order.assert_called_once();cancel.assert_called_once()
    runner.close()


def test_auth_after_acceptance_never_releases_original_id(tmp_path):
    runner = Runner("alpaca", config(), True, tmp_path/"state.sqlite3")
    runner.adapter.place_order = Mock(return_value={"id": "broker-1"})
    with patch.object(timed_cancel, "_detail", side_effect=AuthenticationError("read denied", no_submission=True)):
        result = runner.process(command())
    assert result["status"] == "authentication_required" and result["submission_outcome"] == "unknown"
    assert runner.journal.unresolved(runner.scope)
    assert runner.process(command())["duplicate"] is True
    runner.adapter.place_order.assert_called_once()
    runner.close()


def test_manual_not_submitted_clears_both_journals_without_writes(tmp_path):
    runner = Runner("alpaca", config(), True, tmp_path/"state.sqlite3")
    runner.adapter.place_order = Mock(side_effect=BrokerError("uncertain"))
    assert runner.process(command())["status"] == "unknown"
    result = runner.journal.reconcile(runner.scope, "timed-root", "not_submitted", "Reviewed exact client ID with broker support")
    assert result["timed_cancellation_state"] == "complete"
    assert runner.unresolved_timed() == [] and runner.journal.unresolved(runner.scope) == []
    assert runner.process(command())["status"] == "reconciled"
    runner.adapter.place_order.assert_called_once()
    runner.close()


def test_manual_found_keeps_cancel_responsibility_and_validates_ticket_on_recovery(tmp_path):
    runner = Runner("alpaca", config(), True, tmp_path/"state.sqlite3")
    runner.adapter.place_order = Mock(side_effect=BrokerError("uncertain"))
    runner.process(command())
    result = runner.journal.reconcile(runner.scope, "timed-root", "found", "Matched exact client ID in account", "broker-1")
    assert result["timed_cancellation_state"] == "cancel_pending"
    assert runner.trading_enabled() is False
    with patch.object(timed_cancel, "_detail", side_effect=BrokerError("ticket fields differ")), patch.object(timed_cancel, "_write_cancel") as cancel:
        assert runner.recover_pending()["status"] == "reconciliation_required"
    cancel.assert_not_called();runner.adapter.place_order.assert_called_once()
    runner.close()
