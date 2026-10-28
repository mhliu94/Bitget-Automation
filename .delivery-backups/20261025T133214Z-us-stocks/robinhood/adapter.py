"""US equities through Robinhood's official OAuth-authenticated Trading MCP.

The published tool catalog establishes capabilities. Publicly captured schemas seed
our arguments, but every tool call is checked against the authenticated live schema.
No Crypto API, private mobile API, or automatic submission retry is used.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import re
import uuid

from common.errors import AuthenticationError, BrokerError, ConfigurationError, UnsupportedOrder, OutcomeUnknown
from common.utils import decimal_string, in_window, iso_utc

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
OPEN_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
PENDING_STATES = {"pending_cancelled"}
CLOSED_STATES = {"filled", "cancelled", "partially_filled_rest_cancelled", "rejected", "failed", "voided"}
TOOLS = {"get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders",
         "get_equity_tradability", "review_equity_order", "place_equity_order", "cancel_equity_order"}


class Adapter:
    def __init__(self, config: dict):
        self.config = dict(config)
        # An OAuth bearer is scoped to this resource; do not send it to configurable origins.
        self.base_url = self.config.get("base_url", ENDPOINT)
        if self.base_url != ENDPOINT:
            raise ConfigurationError("Robinhood US stocks require the official Trading MCP endpoint")
        for key in ("review_before_order",):
            if key in self.config and not isinstance(self.config[key], bool):
                raise ConfigurationError(f"Robinhood {key} must be boolean")
        self._sequence = 0
        self._session_id = None
        self._protocol_version = "2025-06-18"
        self._initialized = False
        self._tools = None
        self._server_info = None

    @staticmethod
    def _symbol(symbol):
        if not isinstance(symbol, str):
            raise UnsupportedOrder("Robinhood requires a US stock ticker")
        symbol = symbol.strip().upper()
        if (not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?", symbol)
                or symbol.endswith(("-USD", "-USDT", "-USDC"))):
            raise UnsupportedOrder("Robinhood requires a US stock ticker such as AAPL, not a crypto pair or option")
        return symbol

    def _account_params(self):
        account = self.config.get("account_number")
        if not isinstance(account, str) or not account.strip() or account != account.strip():
            raise ConfigurationError("Robinhood requires account_number for its US equities account")
        return {"account_number": account}

    def build_order(self, command: dict) -> dict:
        if command.get("asset_class", "equity") not in {"stock", "equity", "us_equity"}:
            raise UnsupportedOrder("Robinhood scripts support US equities only")
        if command.get("options"):
            raise UnsupportedOrder("Robinhood stock adapter does not accept extra order options")
        side, kind = command.get("side"), command.get("order_type")
        if side not in {"buy", "sell"} or kind not in {"market", "limit"}:
            raise UnsupportedOrder("Robinhood Agentic supports long-equity buy/sell market or limit orders here")
        tif = command.get("time_in_force", "day")
        if tif not in {"day", "gtc"}:
            raise UnsupportedOrder("Robinhood equity MCP has no verified FOK/IOC; use DAY or GTC")
        if command.get("extended_hours"):
            raise UnsupportedOrder("This US stock adapter currently submits regular-session orders only")
        if command.get("notional") is not None or command.get("dollar_amount") is not None:
            raise UnsupportedOrder("This adapter accepts share quantities; dollar-based orders are not enabled")
        quantity = decimal_string(command.get("quantity"))
        amount = Decimal(quantity)
        if kind == "limit" and amount != amount.to_integral_value():
            raise UnsupportedOrder("Robinhood MCP fractional quantities require regular-session market orders")
        if kind == "market" and amount.as_tuple().exponent < -6:
            raise UnsupportedOrder("Robinhood fractional market quantities support at most six decimal places")
        if not isinstance(command.get("id"), str) or not command["id"]:
            raise UnsupportedOrder("Robinhood requires a stable command ID")
        body = {"account_number": self.config.get("account_number") or "<configured-account>",
                "symbol": self._symbol(command.get("symbol")), "side": side, "type": kind,
                "quantity": quantity, "time_in_force": "gfd" if tif == "day" else "gtc",
                "market_hours": "regular_hours",
                "ref_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "broker-api-scripts:robinhood:equity:" + command["id"]))}
        if kind == "limit":
            body["limit_price"] = decimal_string(command.get("limit_price"))
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("Market orders must not include limit_price")
        if command.get("auction_intent") and (kind != "limit" or tif != "day"):
            raise UnsupportedOrder("LULD reopening intent requires an ordinary regular-session DAY limit")
        return body

    @staticmethod
    def _validate_schema(instance, schema, label):
        try:
            from jsonschema import validators, SchemaError, ValidationError
        except ImportError:
            raise ConfigurationError("Install jsonschema>=4.18,<5 for Robinhood MCP schema validation") from None
        if not isinstance(schema, dict):
            raise BrokerError(f"Robinhood omitted {label} JSON schema")
        def check_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(value, str) or not value.startswith("#")):
                        raise BrokerError("Robinhood schema contains an external reference; local validation required")
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)
        check_refs(schema)
        try:
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
            validator(schema).validate(instance)
        except (SchemaError, ValidationError):
            # Validation messages may contain account numbers or complete payloads.
            raise BrokerError(f"Robinhood {label} does not match the authenticated tool schema") from None

    def _rpc(self, method, params=None, *, mutation=False, notification=False):
        import requests
        token = self.config.get("access_token")
        if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
            raise ConfigurationError("Robinhood requires an OAuth access_token for its Trading MCP; crypto API keys do not work")
        self._sequence += 1
        request_id = self._sequence
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        failure = OutcomeUnknown if mutation else BrokerError
        try:
            response = requests.post(self.base_url, headers=headers, json=payload,
                                     timeout=self.config.get("timeout", 20), allow_redirects=False)
        except requests.RequestException:
            raise failure("Robinhood MCP transport failed; no automatic retry; reconcile any unconfirmed write") from None
        if response.status_code in {401, 403}:
            raise AuthenticationError("Robinhood OAuth token expired or access was denied; refresh through the authorized OAuth client", no_submission=True)
        if not 200 <= response.status_code < 300:
            raise failure(f"Robinhood MCP returned HTTP {response.status_code}; no automatic retry")
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            if not isinstance(session_id, str) or len(session_id) > 1024 or any(ord(x) < 33 or ord(x) > 126 for x in session_id):
                raise failure("Robinhood returned an invalid MCP session identifier")
            if method != "initialize" and self._session_id and session_id != self._session_id:
                raise failure("Robinhood unexpectedly changed its MCP session identifier")
            self._session_id = session_id
        if notification:
            return None
        try:
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                messages = []
                data = []
                for line in (response.text + "\n\n").splitlines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        messages.append(json.loads("\n".join(data)))
                        data = []
                matches = [m for m in messages if isinstance(m, dict) and type(m.get("id")) is int and m["id"] == request_id]
                if len(matches) != 1:
                    raise ValueError
                result = matches[0]
            else:
                result = response.json()
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or type(result.get("id")) is not int or result["id"] != request_id):
                raise ValueError
        except (ValueError, TypeError):
            raise failure("Robinhood MCP response is malformed or does not match the request; reconcile unconfirmed writes") from None
        if "error" in result:
            error = result["error"]
            if isinstance(error, dict) and error.get("code") in {-32601, -32602}:
                raise BrokerError("Robinhood rejected the MCP method or argument schema before execution")
            raise failure("Robinhood returned an MCP error; no automatic retry")
        if "result" not in result or not isinstance(result["result"], dict):
            raise failure("Robinhood MCP omitted its result object")
        return result["result"]

    def discover_tools(self):
        if self._tools is not None:
            return {"endpoint": ENDPOINT, "protocol_version": self._protocol_version,
                    "server_info": self._server_info, "tools": list(self._tools.values())}
        if not self._initialized:
            result = self._rpc("initialize", {"protocolVersion": self._protocol_version, "capabilities": {},
                               "clientInfo": {"name": "broker-api-scripts", "version": "1.0"}})
            version = result.get("protocolVersion")
            if version not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                raise BrokerError("Robinhood negotiated an unsupported MCP protocol version")
            if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
                raise BrokerError("Robinhood MCP did not advertise tools")
            self._protocol_version, self._server_info = version, result.get("serverInfo")
            self._initialized = True
            self._rpc("notifications/initialized", notification=True)
        tools, seen, cursor = {}, set(), None
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            rows = result.get("tools")
            if not isinstance(rows, list):
                raise BrokerError("Robinhood MCP tool listing is malformed")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("inputSchema"), dict):
                    raise BrokerError("Robinhood MCP returned an invalid tool definition")
                if row["name"] in tools:
                    raise BrokerError("Robinhood MCP returned duplicate tool definitions")
                tools[row["name"]] = row
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise BrokerError("Robinhood MCP repeated an invalid tool cursor")
            seen.add(cursor)
        self._tools = tools
        return {"endpoint": ENDPOINT, "protocol_version": self._protocol_version,
                "server_info": self._server_info, "tools": list(tools.values())}

    def _call_tool(self, name, arguments, *, mutation=False):
        if name not in TOOLS:
            raise UnsupportedOrder("Only the reviewed Robinhood US equity tools may be invoked")
        self.discover_tools()
        tool = self._tools.get(name)
        if tool is None:
            raise BrokerError(f"Robinhood does not expose the required {name} tool to this account")
        properties = tool["inputSchema"].get("properties", {})
        if not isinstance(properties, dict) or not set(arguments) <= set(properties):
            raise BrokerError(f"Robinhood {name} does not declare all requested arguments")
        self._validate_schema(arguments, tool.get("inputSchema"), name + " input")
        if not isinstance(tool.get("outputSchema"), dict):
            raise BrokerError(f"Robinhood {name} lacks an output schema; inspect discovery before using this adapter")
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}, mutation=mutation)
        failure = OutcomeUnknown if mutation else BrokerError
        if result.get("isError"):
            raise failure(f"Robinhood {name} returned a tool error; reconcile any unconfirmed write")
        structured = result.get("structuredContent")
        if structured is None:
            blocks = result.get("content", [])
            texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            try:
                structured = json.loads(texts[0]) if len(texts) == 1 else None
            except (ValueError, TypeError):
                structured = None
        try:
            self._validate_schema(structured, tool["outputSchema"], name + " output")
        except BrokerError:
            raise failure(f"Robinhood {name} response failed live-schema validation; reconcile unconfirmed writes") from None
        if not isinstance(structured, dict) or not isinstance(structured.get("data"), dict):
            raise failure(f"Robinhood {name} response omitted its data envelope")
        return structured["data"]

    def _account_record(self, *, trading=False):
        number = self._account_params()["account_number"]
        data = self._call_tool("get_accounts", {})
        rows = data.get("accounts")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BrokerError("Robinhood account response is malformed")
        matches = [row for row in rows if row.get("account_number") == number]
        if len(matches) != 1:
            raise BrokerError("Robinhood configured account is missing or ambiguous")
        account = matches[0]
        if account.get("affiliate") not in (None, "", "RHF"):
            raise UnsupportedOrder("This adapter is scoped to Robinhood US brokerage accounts")
        if trading and account.get("agentic_allowed") is not True:
            raise UnsupportedOrder("The configured Robinhood account is not accessible for Agentic trading")
        if trading and (account.get("state") != "active" or account.get("deactivated") is True or account.get("permanently_deactivated") is True):
            raise UnsupportedOrder("The configured Robinhood account is not active for trading")
        return account

    def _pages(self, tool, arguments, collection):
        params, seen = dict(arguments), set()
        while True:
            data = self._call_tool(tool, params)
            rows = data.get(collection)
            if rows is None:
                rows = []  # The published output schema permits null collections.
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise BrokerError(f"Robinhood {tool} returned an invalid record list")
            yield from rows
            if "next" not in data:
                raise BrokerError(f"Robinhood {tool} omitted pagination completeness")
            cursor = data["next"]
            if cursor == "":
                return
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise BrokerError("Robinhood returned an invalid or repeated pagination cursor")
            seen.add(cursor)
            params = dict(arguments, cursor=cursor)  # Opaque token, never a URL to fetch.

    def positions(self):
        self._account_record()
        rows = list(self._pages("get_equity_positions", self._account_params(), "positions"))
        if any(not isinstance(row.get("symbol"), str) for row in rows):
            raise BrokerError("Robinhood equity position omitted its ticker")
        return rows

    def _orders(self, **filters):
        params = dict(self._account_params(), **filters)
        unique = {}
        for row in self._pages("get_equity_orders", params, "orders"):
            identity = row.get("id")
            if not isinstance(identity, str) or not identity:
                raise BrokerError("Robinhood equity order omitted its ID")
            if row.get("account_number", params["account_number"]) != params["account_number"]:
                raise BrokerError("Robinhood equity order returned another account")
            if identity in unique and unique[identity] != row:
                raise BrokerError("Robinhood returned conflicting duplicate orders")
            unique[identity] = row
        return list(unique.values())

    def orders_today(self, start: datetime, end: datetime):
        self._account_record()
        rows = self._orders(created_at_gte=iso_utc(start))
        for row in rows:
            if not row.get("created_at"):
                raise BrokerError("Robinhood equity order omitted creation timestamp")
        return sorted((row for row in rows if in_window(row["created_at"], start, end)),
                      key=lambda row: (row["created_at"], row["id"]))

    def tradability(self, symbol):
        symbol = self._symbol(symbol)
        account = self._account_record()
        data = self._call_tool("get_equity_tradability", dict(self._account_params(), symbols=[symbol]))
        rows = data.get("results")
        if not isinstance(rows, list):
            raise BrokerError("Robinhood tradability omitted results")
        matches = [row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol]
        if not matches:
            if symbol not in (data.get("not_found") or []):
                raise BrokerError("Robinhood did not explicitly resolve the requested symbol")
            return {"symbol": symbol, "found": False, "tradable": False, "buy_tradable": False,
                    "sell_tradable": False, "account_tradable": False, "halted": None, "luld_applicable": True}
        if len(matches) != 1:
            raise BrokerError("Robinhood tradability symbol is ambiguous")
        row = matches[0]
        us_listing = row.get("country") == "US"
        active = row.get("state") == "active"
        tradable = row.get("tradeable") is True and active and us_listing
        statuses = [r.get("account_type_tradability") for r in (row.get("account_type_tradabilities") or [])
                    if isinstance(r, dict) and r.get("account_type") == account.get("brokerage_account_type")]
        if len(statuses) > 1:
            raise BrokerError("Robinhood account-type tradability is ambiguous")
        status = statuses[0] if statuses else None
        buy = tradable and status == "tradable" and account.get("agentic_allowed") is True
        sell = tradable and status in {"tradable", "position_closing_only"} and account.get("agentic_allowed") is True
        halted = True if row.get("internal_halt_reason") else None
        return {"symbol": symbol, "found": True, "tradable": tradable,
                "buy_tradable": buy, "sell_tradable": sell, "account_tradable": buy,
                "account_status": status, "us_listing_verified": us_listing, "halted": halted,
                "luld_applicable": True, "fractional_tradability": row.get("fractional_tradability"),
                "short_selling_tradability": row.get("short_selling_tradability"),
                "instrument": row, "reason": "Listing and account-type eligibility; no confirmed LULD auction route or borrow inventory"}

    def place_order(self, command):
        body = self.build_order(command)
        body.update(self._account_params())
        self._account_record(trading=True)
        eligibility = self.tradability(body["symbol"])
        if eligibility.get(body["side"] + "_tradable") is not True:
            raise UnsupportedOrder("Robinhood has not confirmed this US listing and account eligible for the requested side")
        if Decimal(body["quantity"]) != Decimal(body["quantity"]).to_integral_value() and eligibility.get("fractional_tradability") != "tradable":
            raise UnsupportedOrder("Robinhood has not confirmed fractional trading for this stock")
        # Agentic is long-equity only. Do not implicitly open a short via SELL.
        if body["side"] == "sell":
            positions = [row for row in self.positions() if row.get("symbol") == body["symbol"]]
            if len(positions) != 1:
                raise UnsupportedOrder("Robinhood Agentic sell requires one existing long position")
            try:
                available = Decimal(positions[0]["shares_available_for_sells"])
                if not available.is_finite() or available < Decimal(body["quantity"]):
                    raise ValueError
            except (KeyError, ValueError, TypeError, ArithmeticError):
                raise UnsupportedOrder("Robinhood available long shares do not cover this sell; short sales are not enabled") from None
        if self.config.get("review_before_order", True):
            review = self._call_tool("review_equity_order", {k: v for k, v in body.items() if k != "ref_id"})
            if review.get("order_checks") != {}:
                raise UnsupportedOrder("Robinhood pre-trade review returned warnings or no clear result; order was not submitted")
            for key in ("symbol", "side", "type", "quantity", *( ["limit_price"] if body["type"] == "limit" else [] )):
                if review.get(key) != body[key]:
                    raise BrokerError("Robinhood review did not confirm the requested ticket")
        result = self._call_tool("place_equity_order", body, mutation=True)
        row = result.get("order")
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise OutcomeUnknown("Robinhood returned no placed order ID; it may require in-app approval. Reconcile before retrying")
        if row.get("ref_id") != body["ref_id"] or row.get("symbol") != body["symbol"] or row.get("side") != body["side"]:
            raise OutcomeUnknown("Robinhood order acknowledgement does not match the client ticket; reconcile before retrying")
        return row

    def open_stock_orders(self):
        self._account_record()
        rows = self._orders()
        if any(row.get("state") not in OPEN_STATES | PENDING_STATES | CLOSED_STATES for row in rows):
            raise BrokerError("Robinhood returned an unrecognized equity order state; no cancellations sent")
        return [{**row, "order_id": row["id"]} for row in rows if row["state"] in OPEN_STATES]

    def stock_order_detail(self, order_id):
        if not isinstance(order_id, str) or not order_id or order_id != order_id.strip():
            raise BrokerError("Robinhood requires an exact stock order ID")
        rows = self._orders(order_id=order_id)
        if len(rows) != 1 or rows[0]["id"] != order_id:
            raise BrokerError("Robinhood exact stock order is not uniquely visible in the configured account")
        return {**rows[0], "order_id": order_id}

    def cancel_stock_order(self, order_id, symbol=None):
        self._account_record(trading=True)
        row = self.stock_order_detail(order_id)
        if symbol and row.get("symbol") != self._symbol(symbol):
            raise BrokerError("Robinhood cancellation symbol does not match its order")
        if row.get("state") not in OPEN_STATES:
            raise BrokerError("Robinhood order is not confirmed open for cancellation")
        result = self._call_tool("cancel_equity_order", dict(self._account_params(), order_id=order_id), mutation=True)
        if result.get("accepted") is not True:
            raise OutcomeUnknown("Robinhood did not confirm accepting cancellation; reconcile order status")
        return {"accepted": True, "order_id": order_id, "cancellation_confirmed": False}

    def borrow_availability(self, symbol):
        eligibility = self.tradability(symbol)
        flag = eligibility.get("short_selling_tradability")
        return {"symbol": self._symbol(symbol), "supported": False, "status": "borrow_inventory_api_unavailable",
                "shortable": True if flag == "tradable" else False if flag == "untradable" else None,
                "short_selling_tradability": flag, "short_order_supported": False,
                "easy_to_borrow": None, "available_quantity": None, "fee_rate": None, "locate_required": None,
                "reason": "The MCP reports stock short eligibility but publishes no borrow-quantity, fee, or locate tool; Agentic orders currently support long equities",
                "source": "https://robinhood.com/us/en/support/articles/trading-with-your-agent/"}

    def capabilities(self):
        return {"broker": "robinhood", "scope": "US equities via official Robinhood Trading MCP",
                "order_types": ["market", "limit"], "time_in_force": ["day", "gtc"],
                "limit_time_in_force": ["day", "gtc"], "market_time_in_force": ["day", "gtc"],
                "native_fok": False, "short_selling": False, "luld_applicable": True,
                "luld_auction_participation": False, "authentication": "OAuth bearer and Agentic account",
                "borrow_availability": "Short eligibility only; no verified inventory/rate/locate API",
                "schema_validation": "Authenticated tools/list checked before calls; discovery and OAuth needed",
                "review_before_order": self.config.get("review_before_order", True),
                "tradability_scope": "US listing, account-type, fractional/session eligibility; live checks required"}

    def snapshot_account(self):
        from common.snapshots import number, position
        self._account_record()
        portfolio = self._call_tool("get_portfolio", self._account_params())
        if portfolio.get("currency") != "USD":
            raise BrokerError("Robinhood US portfolio did not report USD")
        cash = number(portfolio.get("cash"), "cash")
        buying_power = number(portfolio.get("buying_power"), "buying power")
        positions = [position(row.get("symbol"), row.get("quantity"), row.get("average_buy_price"),
                              available_qty=number(row.get("shares_available_for_sells"), "available shares"))
                     for row in self.positions()]
        return {"cash": cash, "cash_by_currency": {"USD": cash}, "buying_power_by_currency": {"USD": buying_power},
                "positions": positions, "cash_basis": "cash_balance"}

    def snapshot_order(self, row, observed_at):
        from common.snapshots import base_order, number
        statuses = {**{state: "NEW" for state in OPEN_STATES}, "partially_filled": "PARTIALLY_FILLED",
                    "pending_cancelled": "PENDING_CANCEL", "filled": "FILLED", "cancelled": "CANCELED",
                    "partially_filled_rest_cancelled": "CANCELED", "rejected": "REJECTED", "failed": "REJECTED", "voided": "CANCELED"}
        kind = {"market": "MARKET", "limit": "LIMIT"}.get(row.get("type"), "OTHER")
        if row.get("trigger") == "stop":
            kind = {"MARKET": "STOP", "LIMIT": "STOP_LIMIT"}.get(kind, "OTHER")
        observed_at = iso_utc(observed_at) if isinstance(observed_at, datetime) else observed_at
        result = base_order(row.get("id"), row.get("created_at"), row.get("last_transaction_at") or observed_at,
                            row.get("symbol"), str(row.get("side", "")).upper(), kind,
                            statuses.get(row.get("state"), "UNKNOWN"), row.get("quantity"),
                            row.get("cumulative_quantity"), row.get("average_price"))
        for src, dst in (("price", "limit_price"), ("stop_price", "stop_price")):
            if row.get(src) is not None:
                result[dst] = number(row[src], src)
        if row.get("dollar_based_amount"):
            result["notional"] = number(row["dollar_based_amount"].get("amount"), "notional")
        if row.get("ref_id"):
            result["client_order_id"] = row["ref_id"]
        if row.get("time_in_force"):
            result["time_in_force"] = "day" if row["time_in_force"] == "gfd" else row["time_in_force"]
        return result
