"""Longbridge HK/SG account OpenAPI adapter (including US-listed securities)."""
from datetime import datetime
from decimal import Decimal
import hashlib
import hmac
import time
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

import requests

from common.errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown, UnsupportedOrder
from common.equity_scope import require_us_equity, us_stock_symbol
from common.utils import decimal_string, in_window


class Adapter:
    def __init__(self, config: dict):
        self.config = config
        self._http = None

    def build_order(self, command: dict) -> dict:
        tif = command.get("time_in_force", "day").lower()
        if tif not in {"day", "gtc"}:
            raise UnsupportedOrder("Longbridge supports DAY/GTC here; native FOK and IOC are unavailable.")
        order_type = command.get("order_type", "market").lower()
        if order_type not in {"market", "limit"}:
            raise UnsupportedOrder("Longbridge order_type must be market or limit")
        side = command.get("side", "").lower()
        if side not in {"buy", "sell"}:
            raise UnsupportedOrder("Longbridge side must be buy or sell")
        require_us_equity(command, self.config)
        symbol = us_stock_symbol(command.get("symbol"), qualified=True)
        options = command.get("options") or {}
        if set(options) - {"outside_rth", "remark", "extended_hours"}:
            raise UnsupportedOrder("Longbridge options supports only outside_rth, extended_hours and remark")
        quantity = decimal_string(command["quantity"])
        if Decimal(quantity) <= 0:
            raise UnsupportedOrder("quantity must be positive")
        outside = options.get("outside_rth", "RTH_ONLY")
        if "extended_hours" in options:
            if not isinstance(options["extended_hours"], bool):
                raise UnsupportedOrder("extended_hours must be boolean")
            mapped = "ANY_TIME" if options["extended_hours"] else "RTH_ONLY"
            if "outside_rth" in options and outside != mapped:
                raise UnsupportedOrder("Conflicting outside_rth and extended_hours options")
            outside = mapped
        if outside not in {"RTH_ONLY", "ANY_TIME", "OVERNIGHT"}:
            raise UnsupportedOrder("Invalid Longbridge outside_rth")
        if order_type == "market" and outside != "RTH_ONLY":
            raise UnsupportedOrder("Use a limit order outside regular trading hours")
        if command.get("auction_intent") and outside != "RTH_ONLY":
            raise UnsupportedOrder("LULD reopening intent requires outside_rth=RTH_ONLY")
        result = {
            "symbol": symbol,
            "side": "Buy" if side == "buy" else "Sell",
            "order_type": "MO" if order_type == "market" else "LO",
            "submitted_quantity": quantity,
            "time_in_force": "Day" if tif == "day" else "GTC",
            "outside_rth": outside,
            # Broker-side idempotency lasts 10 minutes; the shared ledger persists longer.
            "client_request_id": str(uuid5(NAMESPACE_URL, "longbridge:" + str(command["id"]))),
        }
        if order_type == "limit":
            if command.get("limit_price") is None:
                raise UnsupportedOrder("limit_price is required for a limit order")
            price = decimal_string(command["limit_price"])
            if Decimal(price) <= 0:
                raise UnsupportedOrder("limit_price must be positive")
            result["submitted_price"] = price
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("A market order cannot contain limit_price")
        if "remark" in options:
            if not isinstance(options["remark"], str) or len(options["remark"]) > 255:
                raise UnsupportedOrder("Longbridge remark must be a string of at most 255 characters")
            result["remark"] = options["remark"]
        return result

    def _client(self):
        if self._http is None:
            if self.config.get("data_center", "ap").lower() != "ap":
                raise ConfigurationError("This adapter uses Longbridge HK/SG (AP) account APIs; US-domiciled account APIs differ")
            for key in ("app_key", "app_secret", "access_token"):
                if not self.config.get(key):
                    raise ConfigurationError(f"longbridge.{key} is required")
            if any(str(self.config[key]).startswith("us_") for key in ("app_key", "app_secret", "access_token")):
                raise ConfigurationError("US-domiciled Longbridge credentials require the separate US account APIs")
            if urlsplit(self.config.get("http_url", "https://openapi.longbridge.com")).scheme != "https":
                raise ConfigurationError("longbridge.http_url must use HTTPS")
            self._http = requests.Session()
        return self._http

    def _prepare_request(self, method, path, body, timestamp):
        """Sign the exact bytes requests will transmit, including its URL encoding."""
        endpoint = self.config.get("http_url", "https://openapi.longbridge.com").rstrip("/")
        headers = {"Authorization": self.config["access_token"], "X-Api-Key": self.config["app_key"],
                   "X-Timestamp": str(timestamp), "Content-Type": "application/json; charset=utf-8",
                   "X-DC-Region": "ap"}
        prepared = requests.Request(method.upper(), endpoint + path, json=body, headers=headers).prepare()
        signed_headers = "authorization;x-api-key;x-timestamp"
        values = (f"authorization:{headers['Authorization']}\n"
                  f"x-api-key:{headers['X-Api-Key']}\nx-timestamp:{timestamp}\n")
        url = urlsplit(prepared.url)
        body_bytes = prepared.body or b""
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        payload_hash = hashlib.sha1(body_bytes).hexdigest() if prepared.body is not None else ""
        canonical = f"{method.upper()}|{url.path}|{url.query}|{values}|{signed_headers}|{payload_hash}"
        digest = "HMAC-SHA256|" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        signature = hmac.new(self.config["app_secret"].encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()
        prepared.headers["X-Api-Signature"] = f"HMAC-SHA256 SignedHeaders={signed_headers}, Signature={signature}"
        return prepared

    def _request(self, method, path, body=None):
        client = self._client()
        prepared = self._prepare_request(method, path, body, int(time.time()))
        write = method.lower() != "get" and not path.startswith("/quote/")
        try:
            response = client.send(prepared, timeout=self.config.get("timeout", 20), allow_redirects=False)
        except requests.RequestException:
            raise (OutcomeUnknown if write else BrokerError)("Longbridge request failed; no automatic retry") from None
        if response.status_code in (401, 403):
            # The official error table also uses HTTP 403 for a duplicate
            # signed request. That is not proof of a credential rejection.
            try:
                auth_body = response.json()
            except ValueError:
                auth_body = None
            if isinstance(auth_body, dict) and str(auth_body.get("code")) == "403202":
                raise BrokerError("Longbridge rejected a duplicate signed request; reconcile before resubmitting")
            raise AuthenticationError(f"Longbridge denied authentication or access (HTTP {response.status_code})", no_submission=True)
        if not 200 <= response.status_code < 300:
            error = OutcomeUnknown if write and response.status_code >= 500 else BrokerError
            raise error(f"Longbridge HTTP {response.status_code}; no automatic retry")
        try:
            result = response.json()
        except ValueError:
            raise (OutcomeUnknown if write else BrokerError)("Longbridge returned invalid JSON") from None
        if isinstance(result, dict) and "code" in result:
            # https://open.longbridge.com/docs/error-codes
            if str(result["code"]) in {"401003", "403201", "403203", "403205"}:
                raise AuthenticationError(f"Longbridge rejected API credentials/access (code {result['code']})", no_submission=True)
            if str(result["code"]) != "0":
                raise BrokerError(f"Longbridge returned error code {result['code']}")
            result = result.get("data")
        else:
            raise (OutcomeUnknown if write else BrokerError)("Unexpected Longbridge response envelope")
        return result

    def place_order(self, command: dict):
        return self._request("post", "/v1/trade/order", self.build_order(command))

    def is_us_stock_order(self, row):
        if not isinstance(row, dict):
            raise BrokerError("Unexpected Longbridge instrument record")
        for key in ("asset_class", "instrument_type", "security_type"):
            if row.get(key) is not None and str(row[key]).lower() not in {"us_equity", "equity", "stock"}:
                return False
        if row.get("market") is not None and str(row["market"]).upper() != "US":
            return False
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise BrokerError("Longbridge row omitted its qualified instrument symbol")
        try:
            qualified = us_stock_symbol(symbol, qualified=True)
        except UnsupportedOrder:
            return False
        # Native .US stock symbols and option contract symbols have distinct
        # documented formats; the stock grammar excludes OCC contracts.
        if symbol.upper().endswith(".US"):
            return True
        static = self._request("post", "/quote/static-info", {"symbol": [qualified]})
        rows = static.get("secu_static_info", static.get("secuStaticInfo")) if isinstance(static, dict) else None
        if not isinstance(rows, list):
            raise BrokerError("Longbridge could not verify US stock metadata for this record")
        return any(isinstance(item, dict) and item.get("symbol") == qualified for item in rows)

    def positions(self):
        stocks = self._request("get", "/v1/asset/stock")
        if not isinstance(stocks, dict) or not isinstance(stocks.get("list"), list):
            raise BrokerError("Unexpected Longbridge stock positions response")
        channels = []
        for channel in stocks["list"]:
            if not isinstance(channel, dict) or not isinstance(channel.get("stock_info"), list):
                raise BrokerError("Longbridge omitted a stock position channel")
            channels.append({**channel, "stock_info": [row for row in channel["stock_info"] if self.is_us_stock_order(row)]})
        return {"stocks": {**stocks, "list": channels}, "funds": {"list": []}}

    def capabilities(self):
        return {
            "broker": "longbridge", "scope": "US stocks through HK/SG (AP) accounts",
            "implemented_order_types": ["market", "limit"], "time_in_force": ["day", "gtc"],
            "native_fok": False,
            "borrow_availability": {"query": "borrow_availability", "scope": "Margin ratios and US account sell-capacity estimates only", "shortable_flag": False, "easy_to_borrow_flag": False, "inventory_quantity": False, "fee_rate": False, "locate_required": False},
            "other_broker_order_types": {"US": ["LIT", "MIT", "TSLPAMT", "TSLPPCT"]},
            "luld_auction": {
                "supported": None,
                "guidance": "No documented API instruction guarantees US LULD reopening-auction participation. Ordinary DAY limit orders may be accepted or rejected during a halt; confirm broker routing/eligibility. FOK cannot wait for reopening.",
            },
            "limitations": ["No FOK/IOC emulation", "US-domiciled Longbridge accounts use different APIs", "Quote status does not prove account trading permission"],
            "sources": ["https://open.longbridge.com/docs/trade/trade-definition", "https://open.longbridge.com/docs/trade/order/submit", "https://open.longbridge.com/docs/quote/objects"],
        }

    def tradability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol, qualified=True)
        static = self._request("post", "/quote/static-info", {"symbol": [symbol]})
        quote = self._request("post", "/quote/quotes", {"symbol": [symbol]})
        if not isinstance(quote, dict):
            raise BrokerError("Unexpected Longbridge quote response")
        items = quote.get("secu_quote", quote.get("secuQuote", []))
        matching = [row for row in items if row.get("symbol") == symbol]
        raw_status = matching[0].get("trade_status", matching[0].get("tradeStatus")) if matching else None
        names = {0: "normal", 1: "halted", 2: "delisted", 3: "fuse", 4: "prepare_list", 5: "code_moved", 6: "to_be_opened", 7: "split_stock_halts", 8: "expired", 9: "warrant_prepare_list", 10: "suspend_trade"}
        numeric = int(raw_status) if str(raw_status).isdigit() else None
        status = names.get(numeric, str(raw_status).lower() if raw_status is not None else "unknown")
        halted = status in {"halted", "fuse", "split_stock_halts", "suspend_trade"} if status in names.values() else None
        unavailable = status in set(names.values()) - {"normal"}
        return {"symbol": symbol, "tradable": False if unavailable else None, "halted": halted,
                "status": status, "raw_status": raw_status, "quote_found": bool(matching),
                "scope": "Market status only; normal quotes do not establish account eligibility or auction routing",
                "raw": {"static_info": static, "quote": quote}}

    def borrow_availability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol, qualified=True)
        margin = self._request("get", "/v1/risk/margin-ratio?" + urlencode({"symbol": symbol}))
        estimate = None
        if symbol.endswith(".US"):
            estimate = self._request("get", "/v1/trade/estimate/buy_limit?" + urlencode({
                "symbol": symbol, "order_type": "MO", "side": "Sell"}))
        return {
            "symbol": symbol, "status": "capacity_estimate_only", "shortable": None,
            "easy_to_borrow": None, "available_quantity": None, "fee_rate": None, "locate_required": None,
            "inventory_confirmed": False,
            "reason": "The documented retail OpenAPI exposes margin ratios and account sell-capacity estimates, not stock-loan inventory, borrow fees or locate confirmation. A positive margin_max_qty can include risk/position capacity and does not confirm lendable shares.",
            "raw": {"margin_ratios": margin, "sell_capacity_estimate": estimate},
            "estimate_parameters": {"side": "Sell", "order_type": "MO"} if estimate is not None else None,
            "sources": ["https://open.longbridge.com/docs/trade/asset/margin_ratio", "https://open.longbridge.com/docs/trade/order/estimate_available_buy_limit"],
        }

    @staticmethod
    def _orders(result):
        if not isinstance(result, dict) or not isinstance(result.get("orders", []), list):
            raise BrokerError("Unexpected Longbridge order response")
        return result.get("orders", [])

    def _history(self, lower, upper):
        query = urlencode({"start_at": str(lower), "end_at": str(upper)})
        result = self._request("get", "/v1/trade/order/history?" + query)
        orders = self._orders(result)
        if result.get("has_more"):
            # The API caps history at 1000 and exposes no cursor. Split inclusive
            # second-resolution windows to retrieve the full result set.
            if lower >= upper:
                raise BrokerError("Longbridge history exceeds 1000 orders in one second; API cannot return a complete result")
            midpoint = (lower + upper) // 2
            return self._history(lower, midpoint) + self._history(midpoint + 1, upper)
        return orders

    def orders_today(self, start: datetime, end: datetime):
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ConfigurationError("orders_today requires timezone-aware start < end")
        orders = self._history(int(start.timestamp()), int(end.timestamp()))
        today = self._request("get", "/v1/trade/order/today")
        if isinstance(today, dict) and today.get("has_more"):
            raise BrokerError("Longbridge today response is incomplete")
        orders.extend(self._orders(today))
        unique = {}
        for order in orders:
            if not isinstance(order, dict) or not order.get("order_id") or order.get("submitted_at") is None:
                raise BrokerError("Longbridge order lacks order_id/submitted_at; cannot safely filter today's orders")
            if in_window(order["submitted_at"], start, end) and self.is_us_stock_order(order):
                unique[str(order["order_id"])] = order
        return list(unique.values())
