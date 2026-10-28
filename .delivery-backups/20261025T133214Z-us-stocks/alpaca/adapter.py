"""Alpaca Trading API v2. No order submission is retried automatically."""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote, urlsplit

from common.errors import BrokerError, ConfigurationError, UnsupportedOrder
from common.equity_scope import require_us_equity, us_stock_symbol
from common.http import HttpClient
from common.utils import client_order_id, decimal_string, in_window, iso_utc


class Adapter:
    def __init__(self, config):
        self.config = config
        self._http = None

    @property
    def http(self):
        if self._http is None:
            key, secret = self.config.get("api_key"), self.config.get("api_secret")
            if not key or not secret:
                raise ConfigurationError("Alpaca requires api_key and api_secret")
            self._http = HttpClient(
                self.config.get("base_url", "https://paper-api.alpaca.markets"),
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                timeout=self.config.get("timeout", 20),
            )
        return self._http

    def build_order(self, command):
        options = command.get("options") or {}
        unknown = set(options) - {"extended_hours", "position_intent"}
        if unknown:
            raise UnsupportedOrder("Unsupported Alpaca options: " + ", ".join(sorted(unknown)))
        require_us_equity(command, self.config)
        symbol = us_stock_symbol(command["symbol"])
        asset = "us_equity"
        order_type = command["order_type"]
        tif = command.get("time_in_force", "day")
        if order_type not in {"market", "limit"} or tif not in {"day", "gtc", "ioc", "fok"}:
            raise UnsupportedOrder("Unsupported Alpaca order type or time in force")
        if command["side"] not in {"buy", "sell"}:
            raise UnsupportedOrder("Alpaca side must be buy or sell")
        quantity = decimal_string(command["quantity"])
        qty = Decimal(quantity)
        if qty <= 0:
            raise UnsupportedOrder("Alpaca quantity must be positive")
        if asset == "us_equity" and qty != qty.to_integral_value() and tif != "day":
            raise UnsupportedOrder("Alpaca fractional equity orders require day; FOK is unavailable")
        extended = options.get("extended_hours", False)
        if not isinstance(extended, bool):
            raise UnsupportedOrder("extended_hours must be a boolean")
        if extended and (asset != "us_equity" or order_type != "limit" or tif not in {"day", "gtc"}):
            raise UnsupportedOrder("Alpaca extended hours require equity limit orders with day/gtc")
        payload = {
            "symbol": symbol, "qty": quantity, "side": command["side"],
            "type": order_type, "time_in_force": tif,
            "client_order_id": client_order_id(str(command["id"]), max_length=48),
        }
        if order_type == "limit":
            if command.get("limit_price") is None:
                raise UnsupportedOrder("Alpaca limit orders require limit_price")
            price = decimal_string(command["limit_price"])
            value = Decimal(price)
            if value <= 0:
                raise UnsupportedOrder("Alpaca limit_price must be positive")
            if asset == "us_equity":
                tick = Decimal("0.01") if value >= 1 else Decimal("0.0001")
                if value % tick:
                    raise UnsupportedOrder("Alpaca equity limit price exceeds the supported tick precision")
            payload["limit_price"] = price
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("Market orders must not include limit_price")
        if "extended_hours" in options:
            payload["extended_hours"] = extended
        if "position_intent" in options:
            intent = options["position_intent"]
            if intent not in {"buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"}:
                raise UnsupportedOrder("Unsupported Alpaca position_intent")
            if not intent.startswith(command["side"] + "_"):
                raise UnsupportedOrder("position_intent conflicts with side")
            payload["position_intent"] = intent
        return payload

    def place_order(self, command):
        payload = self.build_order(command)
        return self.http.request("POST", "/v2/orders", json=payload)

    def positions(self):
        result = self.http.request("GET", "/v2/positions")
        if not isinstance(result, list):
            raise BrokerError("Unexpected Alpaca positions response")
        return [row for row in result if self.is_us_stock_order(row)]

    def capabilities(self):
        return {
            "broker": "alpaca", "scope": ["us_equity"],
            "order_types": ["market", "limit"],
            "broker_order_types": {"us_equity": ["market", "limit", "stop", "stop_limit", "trailing_stop"]},
            "time_in_force": {"whole_share_equity": ["day", "gtc", "ioc", "fok"],
                              "fractional_equity": ["day"]},
            "limitations": ["Equity IOC/FOK may require a broker entitlement; OTC FOK is unavailable.",
                            "Only single-leg orders are implemented; broker validation still applies."],
            "tradability": "US stock eligibility only; regular-session halt status is unknown in this REST check.",
            "borrow_availability": {
                "support": "partial", "scope": "US equity stock borrow",
                "fields": ["shortable", "borrow_status", "easy_to_borrow", "locate_required"],
                "htb_quotes": "GET /v1/locates/quotes returns advisory quantity and per-share locate price "
                              "for entitled live accounts; unavailable in paper trading.",
                "daily_borrow_fee_rate": "Not supplied by this lookup; locate price is a separate fee.",
            },
            "sources": ["https://docs.alpaca.markets/us/reference/postorder",
                        "https://docs.alpaca.markets/us/docs/orders-at-alpaca",
                        "https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data"],
            "luld": {
                "dedicated_auction_instruction": False, "auction_participation_confirmed": False,
                "ordinary_resting_order": {"order_type": "limit", "time_in_force": "day"},
                "guidance": "An ordinary limit DAY can be submitted, subject to broker acceptance and routing. "
                            "Its participation in a halt reopening auction is not guaranteed. FOK cannot rest "
                            "waiting for reopening. OPG is the opening-session auction, not an LULD instruction.",
                "status_source": "Alpaca stock-data WebSocket statuses/lulds/imbalances (subscription dependent); "
                                 "not collected by this adapter's REST lookup.",
            },
        }

    def _stock_asset(self, symbol):
        symbol = us_stock_symbol(symbol)
        result = self.http.request("GET", "/v2/assets/" + quote(symbol, safe=""))
        if not isinstance(result, dict) or str(result.get("symbol", "")).upper() != symbol:
            raise BrokerError("Alpaca asset lookup did not identify the requested US stock")
        if result.get("class") != "us_equity":
            raise UnsupportedOrder("Alpaca instrument is not a verified US stock")
        return result

    def is_us_stock_order(self, row):
        """Classify native order/position records without assuming missing metadata."""
        if not isinstance(row, dict):
            raise BrokerError("Unexpected Alpaca instrument record")
        reported = row.get("asset_class", row.get("class"))
        if reported is not None and reported != "us_equity":
            return False
        try:
            symbol = us_stock_symbol(row.get("symbol"))
        except UnsupportedOrder:
            if row.get("symbol"):
                return False
            raise BrokerError("Alpaca row omitted its symbol; US stock scope is unknown") from None
        if reported == "us_equity":
            return True
        asset = self.http.request("GET", "/v2/assets/" + quote(symbol, safe=""))
        if not isinstance(asset, dict) or str(asset.get("symbol", "")).upper() != symbol or "class" not in asset:
            raise BrokerError("Alpaca cannot verify the asset class for this record")
        return asset["class"] == "us_equity"

    def tradability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        result = self._stock_asset(symbol)
        reported = result.get("tradable")
        status = result.get("status")
        eligible = reported if isinstance(reported, bool) else None
        if status == "inactive":
            eligible = False
        return {
            "symbol": result.get("symbol", symbol), "broker": "alpaca",
            "scope": "Broker instrument eligibility; current execution availability is not established",
            "tradable": False if eligible is False else None,
            "instrument_tradable": eligible, "status": status, "halted": None,
            "reason": "The REST instrument response does not establish a current regular-session halt state, "
                      "account permissions, buying power, or order/session eligibility.",
            "raw": result,
        }

    def borrow_availability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        asset = self._stock_asset(symbol)
        borrow_status = asset.get("borrow_status")
        etb = {"easy_to_borrow": True, "hard_to_borrow": False}.get(borrow_status)
        # Compatibility for old accounts/responses, not the primary API field.
        if borrow_status is None and isinstance(asset.get("easy_to_borrow"), bool):
            etb = asset["easy_to_borrow"]
        shortable = asset.get("shortable") if isinstance(asset.get("shortable"), bool) else None
        locate_required = {"easy_to_borrow": False, "hard_to_borrow": True}.get(borrow_status)
        result = {
            "broker": "alpaca", "symbol": asset.get("symbol", symbol), "support": "partial",
            "scope": "US equity borrow classification and advisory HTB locate quote",
            "shortable": shortable, "borrow_status": borrow_status, "easy_to_borrow": etb,
            "quantity": None, "fee_rate": None, "locate_required": locate_required,
            "locate_fee_per_share": None, "quoted_at": None, "inventory_reserved": False,
            "reason": "Asset shortability is not an inventory reservation or account approval. "
                      "No daily borrow fee rate is supplied by these endpoints.",
            "raw": {"asset": asset},
        }
        if borrow_status != "hard_to_borrow" or shortable is False:
            return result
        host = urlsplit(self.config.get("base_url", "https://paper-api.alpaca.markets")).hostname
        if host == "paper-api.alpaca.markets":
            result["reason"] += " Locate quotes are unavailable in Alpaca paper trading."
            return result
        try:
            quotes = self.http.request("GET", "/v1/locates/quotes", params={"symbols": asset.get("symbol", symbol)})
        except BrokerError as exc:
            result["quote_error"] = str(exc)
            result["reason"] += " Live locate quote could not be retrieved; availability and fee remain unknown."
            return result
        if not isinstance(quotes, dict):
            raise BrokerError("Unexpected Alpaca locate quotes response")
        result["raw"]["locate_quotes"] = quotes
        match = next((item for item in quotes.get("quotes", [])
                      if str(item.get("symbol", "")).upper() == str(asset.get("symbol", symbol)).upper()), None)
        if match is None:
            result["reason"] += " No matching locate quote was returned; inspect raw per-symbol errors."
            return result
        for source, target in [("available_qty", "quantity"), ("price", "locate_fee_per_share")]:
            if match.get(source) is not None:
                try:
                    value = Decimal(str(match[source]))
                    if not value.is_finite() or value < 0:
                        raise ValueError()
                    result[target] = format(value, "f")
                except (ValueError, ArithmeticError):
                    raise BrokerError("Invalid numeric value in Alpaca locate quote") from None
        result["quoted_at"] = match.get("quoted_at")
        result["reason"] += " Locate quote quantity is advisory; per-share locate price is not a daily borrow rate."
        return result

    def orders_today(self, start, end):
        """Return orders submitted in [start, end), regardless of status.

        ID cursors preserve orders that share the same timestamp. Alpaca forbids
        timestamp filters alongside ID cursors, so only the first page uses them.
        """
        page_size = 500
        params = {
            "status": "all", "limit": page_size, "direction": "desc", "nested": "false",
            "after": iso_utc(start - timedelta(microseconds=1)), "until": iso_utc(end),
        }
        collected, seen, cursors = [], set(), set()
        for _ in range(int(self.config.get("max_pages", 10000))):
            page = self.http.request("GET", "/v2/orders", params=params)
            if not isinstance(page, list):
                raise BrokerError("Unexpected Alpaca orders response")
            for order in page:
                order_id = order.get("id")
                stamp = order.get("submitted_at") or order.get("created_at")
                if not order_id or not stamp:
                    raise BrokerError("Alpaca order is missing its ID or submission time; completeness is unknown")
                if order_id not in seen and in_window(stamp, start, end):
                    if self.is_us_stock_order(order):
                        collected.append(order)
                    seen.add(order_id)
            if len(page) < page_size:
                return collected
            last = page[-1]
            stamp = last.get("submitted_at") or last.get("created_at")
            if not in_window(stamp, start, end):
                return collected
            cursor = last["id"]
            if cursor in cursors:
                raise BrokerError("Alpaca repeated its order cursor; refusing an incomplete result")
            cursors.add(cursor)
            params = {
                "status": "all", "limit": page_size, "direction": "desc", "nested": "false",
                "before_order_id": cursor,
            }
        raise BrokerError("Alpaca max_pages reached; order history is incomplete")
