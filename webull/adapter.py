"""Official Webull US Trading API; no unofficial password-login endpoints."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
import sys
import time

from common.errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown, UnsupportedOrder
from common.equity_scope import require_us_equity, us_stock_symbol
from common.utils import client_order_id, decimal_string, in_window


class Adapter:
    def __init__(self, config: dict):
        self.config = config
        self._trade = None
        self._api = None

    def build_order(self, command: dict) -> dict:
        require_us_equity(command, self.config)
        instrument = "EQUITY"
        tif = command.get("time_in_force", "day").upper()
        if tif not in {"DAY", "GTC"}:
            raise UnsupportedOrder("Webull US stocks support DAY/GTC here; native FOK/IOC is unavailable")
        kind = command.get("order_type", "market").upper()
        if kind not in {"MARKET", "LIMIT"}:
            raise UnsupportedOrder("Webull order_type must be market or limit")
        side = command.get("side", "").upper()
        if side not in {"BUY", "SELL"}:
            raise UnsupportedOrder("Webull side must be buy or sell")
        symbol = us_stock_symbol(command.get("symbol"))
        quantity = decimal_string(command["quantity"])
        value = Decimal(quantity)
        if value <= 0:
            raise UnsupportedOrder("quantity must be positive")
        options = command.get("options") or {}
        if set(options) - {"support_trading_session", "extended_hours"}:
            raise UnsupportedOrder("Webull options supports only support_trading_session and extended_hours")
        result = {
            "combo_type": "NORMAL", "client_order_id": client_order_id(command["id"], max_length=32),
            "symbol": symbol, "instrument_type": instrument, "market": "US",
            "order_type": kind, "quantity": quantity, "side": side,
            "time_in_force": tif, "entrust_type": "QTY",
        }
        if kind == "LIMIT":
            if command.get("limit_price") is None:
                raise UnsupportedOrder("limit_price is required for a limit order")
            price = decimal_string(command["limit_price"])
            if Decimal(price) <= 0:
                raise UnsupportedOrder("limit_price must be positive")
            result["limit_price"] = price
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("A market order cannot contain limit_price")
        if instrument == "EQUITY":
            session = options.get("support_trading_session", "CORE")
            if "extended_hours" in options:
                if not isinstance(options["extended_hours"], bool):
                    raise UnsupportedOrder("extended_hours must be boolean")
                mapped = "ALL" if options["extended_hours"] else "CORE"
                if "support_trading_session" in options and session != mapped:
                    raise UnsupportedOrder("Conflicting support_trading_session and extended_hours")
                session = mapped
            if session not in {"CORE", "ALL", "NIGHT"}:
                raise UnsupportedOrder("Invalid Webull support_trading_session")
            if kind == "MARKET" and session != "CORE":
                raise UnsupportedOrder("Use a limit order outside the core session")
            if command.get("auction_intent") and session != "CORE":
                raise UnsupportedOrder("LULD reopening intent requires support_trading_session=CORE")
            result["support_trading_session"] = session
        elif "support_trading_session" in options or "extended_hours" in options:
            raise UnsupportedOrder("Trading session options are applicable only to equities")
        return result

    def _account_id(self):
        if not self.config.get("account_id"):
            raise ConfigurationError("webull.account_id is required")
        return self.config["account_id"]

    def _client(self):
        if self._trade is None:
            for key in ("app_key", "app_secret"):
                if not self.config.get(key):
                    raise ConfigurationError(f"webull.{key} is required")
            if self.config.get("region", "us") != "us":
                raise ConfigurationError("This Webull adapter validates US Trading API capabilities; region must be us")
            try:
                from webull.core.client import ApiClient
                from webull.trade.trade_client import TradeClient
            except ImportError as exc:
                raise ConfigurationError("Install webull/requirements.txt for the official Webull SDK") from exc
            api = ApiClient(self.config["app_key"], self.config["app_secret"], "us",
                            auto_retry=False, timeout=self.config.get("timeout", 20))
            endpoint = self.config.get("api_endpoint", "api.webull.com")
            if "/" in endpoint or ":" in endpoint:
                raise ConfigurationError("webull.api_endpoint must be a hostname, without https:// or a path")
            api.add_endpoint("us", endpoint)
            # SDK error logging includes complete signed requests. Application
            # exceptions below provide a sanitized diagnostic instead.
            api.set_stream_logger(stream=sys.stderr, log_level=logging.CRITICAL)
            if self.config.get("token_dir"):
                api.set_token_dir(self.config["token_dir"])
            if self.config.get("access_token"):
                api.set_token(self.config["access_token"])
            try:
                self._trade = TradeClient(api)
            except Exception as exc:
                status = self._auth_status(exc)
                if status is not None:
                    raise AuthenticationError(f"Webull denied authentication or access (HTTP {status})", no_submission=True) from None
                raise
            self._api = api
        return self._trade

    @staticmethod
    def _auth_status(error):
        try:
            from webull.core.exception.exceptions import ServerException
        except ImportError:
            return None
        if isinstance(error, ServerException) and error.get_http_status() in (401, 403):
            return error.get_http_status()
        return None

    @staticmethod
    def _call(method, *, mutation=False, **kwargs):
        try:
            response = method(**kwargs)
        except AuthenticationError:
            raise
        except Exception as exc:
            status = Adapter._auth_status(exc)
            if status is not None:
                raise AuthenticationError(f"Webull denied authentication or access (HTTP {status})", no_submission=True) from None
            raise (OutcomeUnknown if mutation else BrokerError)(f"Webull request failed ({type(exc).__name__}); reconcile any uncertain order before retrying") from None
        if response.status_code in (401, 403):
            raise AuthenticationError(f"Webull denied authentication or access (HTTP {response.status_code})", no_submission=True)
        if response.status_code != 200:
            error = OutcomeUnknown if mutation and response.status_code >= 500 else BrokerError
            raise error(f"Webull HTTP {response.status_code}; inspect account/order status before retrying")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise (OutcomeUnknown if mutation else BrokerError)("Webull returned invalid JSON") from exc

    def place_order(self, command: dict):
        order = self.build_order(command)
        account = self._account_id()
        result = self._call(self._client().order_v3.place_order, mutation=True, account_id=account, new_orders=[order])
        if not isinstance(result, dict):
            raise OutcomeUnknown("Webull submission returned no order acknowledgement; reconcile before retrying")
        identity = result.get("order_id")
        if (not isinstance(identity, str) or not identity or len(identity) > 256
                or any(char.isspace() or ord(char) < 32 or ord(char) >= 127 for char in identity)
                or result.get("error") or result.get("error_code")):
            raise OutcomeUnknown("Webull submission did not identify the NORMAL order's broker ID; reconcile before retrying")
        if "client_order_id" in result and result["client_order_id"] != order["client_order_id"]:
            raise OutcomeUnknown("Webull submission returned a different client order ID")
        if "account_id" in result and result["account_id"] != account:
            raise OutcomeUnknown("Webull submission returned a different account")
        return result

    def is_us_stock_order(self, row):
        if not isinstance(row, dict):
            raise BrokerError("Unexpected Webull instrument record")
        kind, market = row.get("instrument_type"), row.get("market")
        if kind is not None and str(kind).upper() != "EQUITY":
            return False
        if market is not None and str(market).upper() != "US":
            return False
        try:
            symbol = us_stock_symbol(row.get("symbol"))
        except UnsupportedOrder:
            if row.get("symbol"):
                return False
            raise BrokerError("Webull row omitted its symbol; US stock scope is unknown") from None
        if kind == "EQUITY" and market == "US":
            return True
        raw = self._stock_instruments(symbol)
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise BrokerError("Webull US stock instrument lookup returned an invalid response")
        matches = [item for item in rows if isinstance(item, dict) and str(item.get("symbol", "")).upper() == symbol]
        if len(matches) != 1:
            raise BrokerError("Webull could not verify this record through its US_STOCK instrument endpoint")
        profile = matches[0]
        if profile.get("instrument_type") not in (None, "EQUITY") or profile.get("market") not in (None, "US"):
            return False
        return True

    def positions(self):
        account = self._account_id()
        response = self._call(self._client().account_v2.get_account_position, account_id=account)
        if isinstance(response, list):
            return [row for row in response if self.is_us_stock_order(row)]
        if isinstance(response, dict):
            for key in ("data", "positions"):
                if isinstance(response.get(key), list):
                    return {**response, key: [row for row in response[key] if self.is_us_stock_order(row)]}
        raise BrokerError("Unexpected Webull positions response")

    def capabilities(self):
        return {
            "broker": "webull", "scope": "Webull US Trading API, configured account",
            "order_types_by_asset": {"us_equity": ["market", "limit"]},
            "time_in_force_by_asset": {"us_equity": ["day", "gtc"]},
            "native_fok": False,
            "borrow_availability": {"query": "borrow_availability", "scope": "US stock instrument flags, not account-specific locates", "shortable_flag": True, "easy_to_borrow_flag": True, "inventory_quantity": False, "fee_rate": False, "locate_required": False},
            "other_broker_order_types": ["stop loss", "stop loss limit", "trailing stop", "institutional opening/closing orders"],
            "luld_auction": {"supported": None, "guidance": "No documented retail API field guarantees participation in a LULD reopening auction. An ordinary CORE DAY limit order can be submitted, subject to broker acceptance and routing. Institutional opening orders are not LULD reopening instructions; FOK is unavailable for equities."},
            "limitations": ["Tradability checks query stock instrument status, not account-specific permissions or live halt status"],
            "sources": ["https://developer.webull.com/apis/docs/reference/common-order-place/", "https://developer.webull.com/apis/docs/reference/instrument-list/", "https://www.webull.com/learn/courseware/CNsYOv/Limit-Up-Limit-Down"],
        }

    def _stock_instruments(self, symbol):
        self._client()
        from webull.core.request import ApiRequest
        request = ApiRequest("/trading/instruments/stocks/profiles/list", version="v3", method="GET",
                             query_params={"symbols": symbol, "category": "US_STOCK"})
        return self._call(self._api.get_response, api_request=request)

    def tradability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        raw = self._stock_instruments(symbol)
        rows = raw.get("data", []) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise BrokerError("Unexpected Webull stock instrument response")
        matching = [row for row in rows if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper()]
        if any(row.get("instrument_type") not in (None, "EQUITY") or row.get("market") not in (None, "US") for row in matching):
            raise UnsupportedOrder("Webull stock profile did not identify a US equity")
        status = matching[0].get("status") if matching else None
        return {"symbol": symbol, "tradable": {"OC": True, "NT": False}.get(status),
                "halted": None, "status": status or "unknown", "close_only": status == "CO",
                "scope": "Broker stock-instrument eligibility; account permissions and live LULD/halt status remain unverified",
                "raw": raw}

    def borrow_availability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        raw = self._stock_instruments(symbol)
        rows = raw.get("data", []) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise BrokerError("Unexpected Webull stock instrument response")
        matching = [row for row in rows if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper()]
        if any(row.get("instrument_type") not in (None, "EQUITY") or row.get("market") not in (None, "US") for row in matching):
            raise UnsupportedOrder("Webull stock profile did not identify a US equity")
        profile = matching[0] if matching else {}
        shortable = profile.get("shortable")
        easy = profile.get("easy_to_borrow")
        return {
            "symbol": symbol, "status": "flags_only" if matching else "unknown",
            "shortable": shortable if isinstance(shortable, bool) else None,
            "easy_to_borrow": easy if isinstance(easy, bool) else None,
            "available_quantity": None, "fee_rate": None, "locate_required": None,
            "inventory_confirmed": False,
            "reason": "US retail stock profiles expose shortable/easy_to_borrow flags, not current lendable quantity, borrow fee or locate-required/confirmed status. Flags do not reserve shares or establish this account's margin permissions. Missing fields remain unknown.",
            "raw": raw,
            "sources": ["https://developer.webull.com/apis/docs/reference/instrument-list/"],
        }

    def _pages(self, method, **kwargs):
        cursor = None
        seen = set()
        for _ in range(int(self.config.get("max_query_pages", 10000))):
            response = self._call(method, pagination_key=cursor, **kwargs)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise BrokerError("Unexpected Webull response; the cursor API requires SDK >=3.0.4")
            yield from response["data"]
            cursor = response.get("pagination_key")
            if not cursor:
                return
            if cursor in seen:
                raise BrokerError("Webull returned a repeated cursor; results are incomplete")
            seen.add(cursor)
            time.sleep(float(self.config.get("query_interval", 1.1)))
        raise BrokerError("Webull pagination limit reached; increase max_query_pages to fetch all orders")

    def orders_today(self, start: datetime, end: datetime):
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ConfigurationError("orders_today requires timezone-aware start < end")
        account = self._account_id()
        orders = self._client().order_v3
        stamp = lambda dt: dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        groups = list(self._pages(orders.list_order_history, account_id=account,
                                 start_time=stamp(start), end_time=stamp(end)))
        time.sleep(float(self.config.get("query_interval", 1.1)))
        groups.extend(self._pages(orders.list_order_open, account_id=account))
        unique = {}
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("orders"), list):
                raise BrokerError("Webull returned an invalid order group")
            for raw_order in group["orders"]:
                order = dict(raw_order)
                order_id = order.get("order_id") or order.get("client_order_id")
                placed = order.get("place_time_at") or order.get("place_time")
                if order_id is None or placed is None:
                    raise BrokerError("Webull order lacks id/placement time; cannot safely filter today's orders")
                if in_window(placed, start, end) and self.is_us_stock_order(order):
                    order["group_client_order_id"] = group.get("client_order_id")
                    order["combo_type"] = group.get("combo_type")
                    unique[str(order_id)] = order
        return list(unique.values())
