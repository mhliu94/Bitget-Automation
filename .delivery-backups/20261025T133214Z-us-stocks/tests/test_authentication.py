"""Only explicit rejected authentication permits releasing a submission claim."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from common.errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown
from common.http import HttpClient
from common.registry import adapter_for
from common.snapshot_asia import collect_orders as collect_asia_orders
from common.snapshots import collect_orders
from zhuorui import bridge_worker


def response(status, body=None):
    result = Mock(status_code=status)
    result.json.return_value = {} if body is None else body
    return result


def assert_rejected_auth(call):
    with pytest.raises(AuthenticationError) as caught:
        call()
    assert caught.value.no_submission is True
    assert caught.value.safe_to_retry_submission is True
    assert "private-secret" not in str(caught.value)


def test_authentication_error_needs_explicit_no_submission_evidence():
    assert isinstance(AuthenticationError("repair access"), BrokerError)
    assert AuthenticationError("repair access").no_submission is False
    assert AuthenticationError("repair access", no_submission="true").no_submission is False
    assert AuthenticationError("rejected", no_submission=True).safe_to_retry_submission is True


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [401, 403])
def test_shared_http_authentication_denial_is_distinct_from_unknown_write(method, status):
    http = HttpClient("https://broker.example")
    http.session.request = Mock(return_value=response(status, {"message": "private-secret"}))
    assert_rejected_auth(lambda: http.request(method, "/orders"))
    assert http.session.request.call_count == 1


@pytest.mark.parametrize("failure", [requests.Timeout("private-secret"), 500, 502])
def test_shared_http_uncertain_write_never_claims_authentication_or_safe_replay(failure):
    http = HttpClient("https://broker.example")
    http.session.request = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=response(failure))
    with pytest.raises(OutcomeUnknown) as caught:
        http.request("POST", "/orders")
    assert not isinstance(caught.value, AuthenticationError)


@pytest.mark.parametrize("broker", ["bitget", "robinhood"])
@pytest.mark.parametrize("status", [401, 403])
def test_signed_brokers_auth_denial_is_not_retried(broker, status):
    config = {"api_key": "test-key", "api_secret": "test-secret", "passphrase": "test-passphrase",
              "private_key_base64": "test-private-key"}
    adapter = adapter_for(broker, config)
    adapter.signature = Mock(return_value="test-signature")
    with patch("requests.request", return_value=response(status, {"error": "private-secret"})) as send:
        assert_rejected_auth(lambda: adapter._request("POST", "/orders", payload={}))
    assert send.call_count == 1


@pytest.mark.parametrize("http_status", [200, 400])
@pytest.mark.parametrize("code", ["40009", "40012", "40014", "40038"])
def test_bitget_documented_credential_codes_reject_before_acceptance(http_status, code):
    adapter = adapter_for("bitget", {"api_key": "key", "api_secret": "secret", "passphrase": "passphrase"})
    with patch("requests.request", return_value=response(http_status, {"code": code, "msg": "private-secret"})):
        assert_rejected_auth(lambda: adapter._request("POST", "/api/v2/spot/trade/place-order", payload={}))


@pytest.mark.parametrize("code", ["40010", "40725", "45001"])
def test_bitget_timeout_and_unknown_result_codes_do_not_release_auth_reservation(code):
    adapter = adapter_for("bitget", {"api_key": "key", "api_secret": "secret", "passphrase": "passphrase"})
    with patch("requests.request", return_value=response(200, {"code": code})):
        with pytest.raises(BrokerError) as caught:
            adapter._request("POST", "/api/v2/spot/trade/place-order", payload={})
    assert not isinstance(caught.value, AuthenticationError)


@pytest.mark.parametrize("broker", ["bitget", "robinhood"])
def test_signed_broker_5xx_auth_looking_body_remains_ambiguous(broker):
    adapter = adapter_for(broker, {"api_key": "key", "api_secret": "secret", "passphrase": "pass",
                                   "private_key_base64": "test-private-key"})
    adapter.signature = Mock(return_value="test-signature")
    with patch("requests.request", return_value=response(503, {"code": "40009", "error": "unauthorized"})):
        with pytest.raises(OutcomeUnknown):
            adapter._request("POST", "/orders", payload={})


@pytest.mark.parametrize("status,body", [(401, {"code": 401003}), (403, {"code": 403201}),
                                       (200, {"code": 403203}), (200, {"code": 403205})])
def test_longbridge_documented_access_denials(status, body):
    adapter = adapter_for("longbridge", {"app_key": "key", "app_secret": "secret", "access_token": "token"})
    adapter._http = Mock(send=Mock(return_value=response(status, body)))
    assert_rejected_auth(lambda: adapter._request("POST", "/v1/trade/order", {}))


def test_longbridge_duplicate_signature_is_not_a_safe_auth_replay():
    adapter = adapter_for("longbridge", {"app_key": "key", "app_secret": "secret", "access_token": "token"})
    adapter._http = Mock(send=Mock(return_value=response(403, {"code": 403202})))
    with pytest.raises(BrokerError) as caught:
        adapter._request("POST", "/v1/trade/order", {})
    assert not isinstance(caught.value, AuthenticationError)


@pytest.mark.parametrize("status", [401, 403])
def test_webull_actual_sdk_exception_preserves_http_auth_evidence(status):
    pytest.importorskip("webull.core.exception.exceptions")
    from webull.core.exception.exceptions import ServerException
    adapter = adapter_for("webull", {})
    method = Mock(side_effect=ServerException("unauthorized", "private-secret", status))
    assert_rejected_auth(lambda: adapter._call(method, mutation=True))
    assert method.call_count == 1
    assert_rejected_auth(lambda: adapter._call(Mock(return_value=response(status)), mutation=True))


def test_webull_sdk_initialization_auth_failure_occurs_before_any_order_call():
    pytest.importorskip("webull.core.exception.exceptions")
    from webull.core.exception.exceptions import ServerException
    from webull.core.http.initializer.client_initializer import ClientInitializer
    adapter = adapter_for("webull", {"app_key": "key", "app_secret": "secret", "account_id": "account"})
    with patch.object(ClientInitializer, "initializer", side_effect=ServerException("auth", "private-secret", 401)):
        assert_rejected_auth(adapter._client)
    assert adapter._trade is None


def test_webull_sdk_5xx_and_transport_exception_are_ambiguous_writes():
    pytest.importorskip("webull.core.exception.exceptions")
    from webull.core.exception.exceptions import ServerException
    adapter = adapter_for("webull", {})
    for error in (ServerException("auth", "private-secret", 503), TimeoutError("private-secret")):
        with pytest.raises(OutcomeUnknown) as caught:
            adapter._call(Mock(side_effect=error), mutation=True)
        assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("status,body", [(401, {}), (403, {}), (400, {"error": "invalid_grant"}),
                                       (400, {"error": "invalid_client"})])
def test_tradestation_refresh_credential_denial_happens_before_broker_write(status, body):
    adapter = adapter_for("tradestation", {"client_id": "client", "refresh_token": "token"})
    adapter._http = Mock()
    with patch("requests.post", return_value=response(status, body)):
        assert_rejected_auth(lambda: adapter._request("POST", "/orderexecution/orders", json={}))
    adapter._http.request.assert_not_called()


@pytest.mark.parametrize("failure", [requests.Timeout("private-secret"), 503])
def test_tradestation_refresh_transport_failure_does_not_claim_credentials_invalid(failure):
    adapter = adapter_for("tradestation", {"client_id": "client", "refresh_token": "token"})
    send = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=response(failure, {"error": "invalid_grant"}))
    with patch("requests.post", send):
        with pytest.raises(ConfigurationError):
            adapter._token()


@pytest.mark.parametrize("operation", ["positions", "orders_today", "tradability", "account_snapshot", "order_snapshots"])
def test_zhuorui_worker_explicit_read_session_denial_is_safe_and_redacted(operation, monkeypatch, capsys):
    session_error = type("SessionExpired", (Exception,), {"__module__": "zhuorui.api.errors"})
    monkeypatch.setattr(bridge_worker, "run", Mock(side_effect=session_error("private-secret")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"operation": operation})))
    assert bridge_worker.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["authentication_error"] is True and result["no_submission"] is True
    assert "private-secret" not in result["error"]


@pytest.mark.parametrize("operation", ["place_order", "cancel_order", "cancel_open_orders", "recover_cancellations"])
def test_zhuorui_mutation_session_error_does_not_claim_no_earlier_write(operation, monkeypatch, capsys):
    session_error = type("LoggedInElsewhere", (Exception,), {"__module__": "zhuorui.api.errors"})
    monkeypatch.setattr(bridge_worker, "run", Mock(side_effect=session_error("expired")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"operation": operation})))
    assert bridge_worker.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["authentication_error"] is False and result["no_submission"] is False


def test_zhuorui_adapter_accepts_auth_marker_only_for_read_operations():
    adapter = adapter_for("zhuorui", {"upstream_root": "/tmp/test-upstream", "upstream_config": "/tmp/test-config"})
    denial = SimpleNamespace(returncode=2, stdout=json.dumps({"ok": False, "authentication_error": True,
                             "no_submission": True, "error": "private-secret"}))
    with patch("subprocess.run", return_value=denial):
        assert_rejected_auth(lambda: adapter._bridge("positions"))
        with pytest.raises(BrokerError) as caught:
            adapter._bridge("place_order")
        assert not isinstance(caught.value, AuthenticationError)


def test_detail_authentication_cannot_be_hidden_as_one_malformed_snapshot():
    from datetime import datetime, timezone
    start = datetime(2026, 10, 25, tzinfo=timezone.utc)
    end = datetime(2026, 10, 26, tzinfo=timezone.utc)
    adapter = adapter_for("longbridge", {})
    adapter.orders_today = Mock(return_value=[{"order_id": "one", "executed_quantity": "1"}])
    adapter._request = Mock(side_effect=AuthenticationError("auth rejected", no_submission=True))
    errors = []
    with pytest.raises(AuthenticationError):
        collect_asia_orders(adapter, "longbridge", start, end, errors=errors)
    assert errors == []
    adapter = Mock()
    adapter.orders_today.return_value = [{"orderId": "one"}]
    adapter._request.side_effect = AuthenticationError("auth rejected", no_submission=True)
    with pytest.raises(AuthenticationError):
        collect_orders(adapter, "bitget", start, end, errors=errors)
    assert errors == []


def test_zhuorui_snapshot_mapper_cannot_hide_explicit_session_denial(monkeypatch):
    session_error = type("SessionExpired", (Exception,), {"__module__": "zhuorui.api.errors"})
    monkeypatch.setattr(bridge_worker, "read_orders", Mock(return_value=[{"orderTxnReference": "one", "entrustTime": 1}]))
    with pytest.raises(session_error):
        bridge_worker.collect_order_snapshots({}, Mock(), {"start": "2026-10-25T00:00:00Z", "end": "2026-10-26T00:00:00Z", "report_errors": True},
                                             Mock(side_effect=session_error("expired")), Mock())
