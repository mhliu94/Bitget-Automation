"""Protocol tests for the official Robinhood Trading MCP (no credentials/network)."""
import json
from unittest.mock import Mock, patch

import pytest
import requests

from common.errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown
from robinhood.adapter import Adapter, ENDPOINT
from robinhood.discover_tools import main


def tool(name="get_equity_positions", inputs=None, required=None, output=None):
    return {"name": name, "inputSchema": {"type": "object", "properties": inputs or {"account_number": {"type": "string"}},
             "required": required if required is not None else ["account_number"], "additionalProperties": False},
            "outputSchema": output or {"type": "object", "properties": {"data": {"type": "object"}}, "required": ["data"]}}


def response(request_id, result=None, *, status=200, headers=None):
    value = Mock(status_code=status, headers=headers or {"Content-Type": "application/json"})
    value.json.return_value = {"jsonrpc": "2.0", "id": request_id, "result": result or {}}
    value.iter_content.side_effect = lambda **kwargs: iter([json.dumps(value.json.return_value).encode()])
    return value


def initialized_adapter(definition=None):
    adapter = Adapter({"access_token": "test-token", "account_number": "TEST123"})
    adapter._initialized = True
    adapter._tools = {"get_equity_positions": definition or tool()}
    return adapter


def test_handshake_session_schema_pagination_and_read_call():
    adapter = Adapter({"access_token": "test-token", "account_number": "TEST123"})
    responses = [
        response(1, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture"}, "capabilities": {"tools": {}}},
                 headers={"Content-Type": "application/json", "Mcp-Session-Id": "session-1"}),
        response(2, status=202),
        response(3, {"tools": [tool()], "nextCursor": "opaque-schema-cursor"}),
        response(4, {"tools": [tool("get_accounts", inputs={}, required=[])]}),
        response(5, {"structuredContent": {"data": {"positions": [], "next": ""}}}),
    ]
    with patch("requests.post", side_effect=responses) as request:
        assert adapter._call_tool("get_equity_positions", {"account_number": "TEST123"}) == {"positions": [], "next": ""}
    methods = [c.kwargs["json"]["method"] for c in request.call_args_list]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list", "tools/call"]
    assert request.call_args_list[3].kwargs["json"]["params"] == {"cursor": "opaque-schema-cursor"}
    assert "id" not in request.call_args_list[1].kwargs["json"]
    for call in request.call_args_list:
        assert call.args == (ENDPOINT,)
        assert call.kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["stream"] is True
    for value in responses:
        value.close.assert_called_once_with()
    assert request.call_args.kwargs["headers"]["Mcp-Session-Id"] == "session-1"
    assert request.call_args.kwargs["headers"]["MCP-Protocol-Version"] == "2025-06-18"


def test_sse_response_with_notifications_and_multiline_data():
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    value.text = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                  'event: message\ndata: {"jsonrpc":"2.0","id":1,\ndata: "result":{"structuredContent":{"data":{"positions":[],"next":""}}}}\n\n')
    value.iter_content.side_effect = lambda **kwargs: iter([value.text.encode()])
    with patch("requests.post", return_value=value):
        assert adapter._call_tool("get_equity_positions", {"account_number": "TEST123"})["next"] == ""
    value.close.assert_called_once_with()


def test_json_text_fallback_validates_actual_output_schema():
    adapter = initialized_adapter()
    value = {"content": [{"type": "text", "text": '{"data":{"positions":[],"next":""}}'}]}
    with patch("requests.post", return_value=response(1, value)):
        assert adapter._call_tool("get_equity_positions", {"account_number": "TEST123"})["positions"] == []


@pytest.mark.parametrize("arguments", [{}, {"account_number": 4}, {"account_number": "TEST123", "unknown": 3}])
def test_input_schema_rejection_sends_no_request(arguments):
    adapter = initialized_adapter()
    with patch("requests.post") as request, pytest.raises(BrokerError):
        adapter._call_tool("get_equity_positions", arguments)
    request.assert_not_called()


def test_undeclared_argument_rejected_even_with_permissive_schema():
    definition = tool()
    definition["inputSchema"]["additionalProperties"] = True
    adapter = initialized_adapter(definition)
    with patch("requests.post") as request, pytest.raises(BrokerError, match="declare"):
        adapter._call_tool("get_equity_positions", {"account_number": "TEST123", "new_parameter": 1})
    request.assert_not_called()


def test_live_enum_is_authoritative():
    adapter = initialized_adapter(tool(inputs={"account_number": {"type": "string", "enum": ["OTHER"]}}))
    with patch("requests.post") as request, pytest.raises(BrokerError, match="schema"):
        adapter._call_tool("get_equity_positions", {"account_number": "TEST123"})
    request.assert_not_called()


def test_remote_schema_reference_never_resolves():
    definition = tool()
    definition["inputSchema"]["properties"]["account_number"] = {"$ref": "https://untrusted.example/schema"}
    adapter = initialized_adapter(definition)
    with patch("requests.post") as request, pytest.raises(BrokerError, match="external"):
        adapter._call_tool("get_equity_positions", {"account_number": "TEST123"})
    request.assert_not_called()


def test_missing_output_schema_stops_before_tool_call():
    definition = tool()
    definition.pop("outputSchema")
    adapter = initialized_adapter(definition)
    with patch("requests.post") as request, pytest.raises(BrokerError, match="output schema"):
        adapter._call_tool("get_equity_positions", {"account_number": "TEST123"})
    request.assert_not_called()


def test_write_output_schema_failure_is_uncertain():
    adapter = initialized_adapter()
    adapter._tools["place_equity_order"] = tool("place_equity_order")
    with patch("requests.post", return_value=response(1, {"structuredContent": {"data": []}})) as request:
        with pytest.raises(OutcomeUnknown, match="schema"):
            adapter._call_tool("place_equity_order", {"account_number": "TEST123"}, mutation=True)
    assert request.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
def test_oauth_failure_is_not_retried_and_has_safe_rejection(status):
    adapter = initialized_adapter()
    with patch("requests.post", return_value=response(1, status=status)) as request:
        with pytest.raises(AuthenticationError) as error:
            adapter._rpc("tools/call", {}, mutation=True)
    assert error.value.no_submission is True
    assert request.call_count == 1


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_write_transport_errors_preserve_uncertainty(status):
    adapter = initialized_adapter()
    with patch("requests.post", return_value=response(1, status=status)) as request, pytest.raises(OutcomeUnknown):
        adapter._rpc("tools/call", {}, mutation=True)
    assert request.call_count == 1


def test_write_timeout_has_no_retry_or_token_in_error():
    adapter = initialized_adapter()
    with patch("requests.post", side_effect=requests.Timeout("test-token")) as request:
        with pytest.raises(OutcomeUnknown) as error:
            adapter._rpc("tools/call", {}, mutation=True)
    assert request.call_count == 1
    assert "test-token" not in str(error.value)


def test_wrong_rpc_request_id_on_write_is_unknown():
    adapter = initialized_adapter()
    with patch("requests.post", return_value=response(42)):
        with pytest.raises(OutcomeUnknown):
            adapter._rpc("tools/call", {}, mutation=True)


def test_invalid_tool_result_on_write_is_unknown():
    adapter = initialized_adapter()
    adapter._tools["place_equity_order"] = tool("place_equity_order")
    with patch("requests.post", return_value=response(1, {"isError": True, "content": []})):
        with pytest.raises(OutcomeUnknown):
            adapter._call_tool("place_equity_order", {"account_number": "TEST123"}, mutation=True)


def test_repeated_schema_cursor_rejected():
    adapter = Adapter({"access_token": "test-token"})
    adapter._initialized = True
    with patch.object(adapter, "_rpc", side_effect=[{"tools": [], "nextCursor": "x"}, {"tools": [], "nextCursor": "x"}]):
        with pytest.raises(BrokerError, match="cursor"):
            adapter.discover_tools()


def test_only_official_endpoint_accepts_bearer():
    with pytest.raises(ConfigurationError):
        Adapter({"base_url": "https://evil.example"})


def test_crypto_credentials_are_not_an_oauth_fallback():
    adapter = Adapter({"api_key": "test-key", "private_key_base64": "unused"})
    with patch("requests.post") as request, pytest.raises(ConfigurationError, match="OAuth"):
        adapter.discover_tools()
    request.assert_not_called()


def test_discovery_cli_dry_run_ignores_missing_config_and_sends_nothing(capsys):
    with patch("requests.post") as request:
        assert main(["--config", "missing.json"]) == 0
    request.assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_discovery_cli_only_exports_schemas(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"brokers": {"robinhood": {"access_token": "test-token", "account_number": "TEST123"}}}))
    output = tmp_path / "schemas.json"
    with patch.object(Adapter, "discover_tools", return_value={"endpoint": ENDPOINT, "tools": [tool()]}) as discover:
        assert main(["--config", str(config), "--execute", "--output", str(output)]) == 0
    assert discover.call_count == 1
    assert "test-token" not in output.read_text()
    assert json.loads(capsys.readouterr().out)["tools"] == 1


def test_sse_write_returns_at_complete_matching_event_without_waiting_for_close():
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    def chunks(**kwargs):
        assert kwargs == {"chunk_size": 1}
        yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        yield b'data: {"jsonrpc":"2.0","id":1,"result":{"accepted":true}}\n'
        yield b'\n'
        raise AssertionError("Client read after the complete matching response instead of closing")
    value.iter_content.side_effect = chunks
    with patch("requests.post", return_value=value) as request:
        assert adapter._rpc("tools/call", {}, mutation=True) == {"accepted": True}
    value.close.assert_called_once_with()
    value.json.assert_not_called()
    assert request.call_count == 1
    assert request.call_args.kwargs["stream"] is True


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_sse_line_endings_are_accepted(ending):
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    body = b'data: {"jsonrpc":"2.0","id":1,"result":{}}' + ending + ending
    value.iter_content.side_effect = lambda **kwargs: (bytes([char]) for char in body)
    with patch("requests.post", return_value=value):
        assert adapter._rpc("tools/list", {}) == {}
    value.close.assert_called_once_with()


def test_stream_timeout_after_partial_response_is_unknown_and_closes_without_retry():
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    def chunks(**kwargs):
        yield b'data: {"jsonrpc":"2.0","id":1'
        raise requests.ConnectionError("private-secret")
    value.iter_content.side_effect = chunks
    with patch("requests.post", return_value=value) as request:
        with pytest.raises(OutcomeUnknown) as caught:
            adapter._rpc("tools/call", {}, mutation=True)
    assert "private-secret" not in str(caught.value)
    assert request.call_count == 1
    value.close.assert_called_once_with()


def test_sse_incomplete_event_is_not_confirmation():
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    value.iter_content.side_effect = lambda **kwargs: iter([b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n'])
    with patch("requests.post", return_value=value), pytest.raises(OutcomeUnknown):
        adapter._rpc("tools/call", {}, mutation=True)
    value.close.assert_called_once_with()


@pytest.mark.parametrize("mime", ["text/event-stream", "application/json"])
def test_response_size_bound_closes_without_oversized_parse(mime):
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": mime})
    value.iter_content.side_effect = lambda **kwargs: iter([b"x" * 33])
    with patch("robinhood.adapter.MAX_RESPONSE_BYTES", 32), patch("requests.post", return_value=value):
        with pytest.raises(OutcomeUnknown, match="oversized"):
            adapter._rpc("tools/call", {}, mutation=True)
    value.close.assert_called_once_with()


def test_sse_heartbeat_cannot_extend_total_response_deadline():
    adapter = initialized_adapter()
    value = response(1, headers={"Content-Type": "text/event-stream"})
    value.iter_content.side_effect = lambda **kwargs: iter([b": heartbeat\n\n"])
    with patch("robinhood.adapter.time.monotonic", side_effect=[0, 21]), patch("requests.post", return_value=value):
        with pytest.raises(OutcomeUnknown, match="transport"):
            adapter._rpc("tools/call", {}, mutation=True)
    value.close.assert_called_once_with()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_error_responses_are_closed_without_reading_body(status):
    adapter = initialized_adapter()
    value = response(1, status=status)
    value.iter_content.side_effect = AssertionError("Do not consume denial/error bodies")
    with patch("requests.post", return_value=value), pytest.raises((AuthenticationError, OutcomeUnknown)):
        adapter._rpc("tools/call", {}, mutation=True)
    value.close.assert_called_once_with()


def test_json_response_stream_is_closed_after_validation():
    adapter = initialized_adapter()
    value = response(1, {"accepted": True})
    with patch("requests.post", return_value=value):
        assert adapter._rpc("tools/call", {}, mutation=True) == {"accepted": True}
    value.close.assert_called_once_with()
    value.json.assert_not_called()
