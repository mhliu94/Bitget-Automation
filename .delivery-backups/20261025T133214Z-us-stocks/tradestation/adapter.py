"""TradeStation API v3, including native FOK and paginated order history."""
from datetime import timedelta
from decimal import Decimal
import time
from urllib.parse import quote

from common.errors import AuthenticationError, BrokerError, ConfigurationError, UnsupportedOrder
from common.equity_scope import require_us_equity, us_stock_symbol
from common.http import HttpClient
from common.utils import client_order_id, decimal_string, in_window


class Adapter:
    def __init__(self, config):
        self.config = config
        self._http = None
        self._access_token = config.get("access_token")
        self._refresh_token = config.get("refresh_token")
        self._expires_at = float(config.get("access_token_expires_at", 0))

    def _token(self):
        if self._access_token and (not self._refresh_token or time.time() < self._expires_at - 60):
            return self._access_token
        if not self._refresh_token or not self.config.get("client_id"):
            raise ConfigurationError("TradeStation requires access_token or client_id and refresh_token")
        # OAuth is not a trading mutation. Still make exactly one refresh attempt.
        import requests
        form = {"grant_type": "refresh_token", "client_id": self.config["client_id"],
                "refresh_token": self._refresh_token}
        if self.config.get("client_secret"):
            form["client_secret"] = self.config["client_secret"]
        try:
            response = requests.post("https://signin.tradestation.com/oauth/token", data=form,
                                     timeout=self.config.get("timeout", 20), allow_redirects=False)
            if response.status_code in (401, 403):
                raise AuthenticationError(f"TradeStation OAuth credentials/access rejected (HTTP {response.status_code})", no_submission=True)
            if response.status_code != 200:
                if response.status_code == 400:
                    try:
                        failure = response.json()
                    except ValueError:
                        failure = None
                    if isinstance(failure, dict) and failure.get("error") in {
                        "invalid_grant", "invalid_client", "unauthorized_client", "access_denied", "invalid_scope"
                    }:
                        raise AuthenticationError("TradeStation OAuth credentials/access rejected; refresh authorization", no_submission=True)
                raise ConfigurationError(f"TradeStation OAuth refresh failed (HTTP {response.status_code})")
            token = response.json()
            self._access_token = token["access_token"]
            self._expires_at = time.time() + float(token.get("expires_in", 1200))
            self._refresh_token = token.get("refresh_token", self._refresh_token)
            return self._access_token
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise ConfigurationError("TradeStation OAuth refresh failed; verify the configured tokens") from exc

    def _request(self, method, path, **kwargs):
        if self._http is None:
            self._http = HttpClient(
                self.config.get("base_url", "https://sim-api.tradestation.com/v3"),
                timeout=self.config.get("timeout", 20),
            )
        result = self._http.request(method, path, headers={"Authorization": "Bearer " + self._token()}, **kwargs)
        if not isinstance(result, dict):
            raise BrokerError("Unexpected TradeStation response")
        if result.get("Error") or result.get("Errors"):
            # HTTP 200 can still contain per-account/order errors. Never return
            # a partial account snapshot or claim an unsuccessful order succeeded.
            raise BrokerError("TradeStation returned a broker error: " + str(result.get("Errors") or result.get("Error")))
        return result

    def _accounts(self):
        accounts = self.config.get("account_ids") or [self.config.get("account_id")]
        if isinstance(accounts, str):
            accounts = [item.strip() for item in accounts.split(",")]
        if not accounts or any(not isinstance(item, str) or not item.strip() for item in accounts):
            raise ConfigurationError("TradeStation requires account_id or account_ids for queries")
        return list(dict.fromkeys(accounts))

    def build_order(self, command):
        options = command.get("options") or {}
        unknown = set(options) - {"trade_action", "route"}
        if unknown:
            raise UnsupportedOrder("Unsupported TradeStation options: " + ", ".join(sorted(unknown)))
        require_us_equity(command, self.config)
        symbol = us_stock_symbol(command["symbol"])
        asset = "equity"
        order_type, side = command["order_type"], command["side"]
        tif = command.get("time_in_force", "day")
        if order_type not in {"market", "limit"} or tif not in {"day", "gtc", "ioc", "fok"}:
            raise UnsupportedOrder("Unsupported TradeStation order type or time in force")
        if side not in {"buy", "sell"}:
            raise UnsupportedOrder("TradeStation side must be buy or sell")
        quantity = decimal_string(command["quantity"])
        qty = Decimal(quantity)
        if qty <= 0 or qty != qty.to_integral_value():
            raise UnsupportedOrder("TradeStation orders require a positive whole quantity")
        action = str(options.get("trade_action", side)).upper()
        allowed = {"equity": {"buy": {"BUY", "BUYTOCOVER"}, "sell": {"SELL", "SELLSHORT"}}}
        if action not in allowed[asset][side]:
            raise UnsupportedOrder("Set options.trade_action to a valid action for the asset class and side")
        payload = {
            "AccountID": self.config.get("account_id") or "<configured-account-id>",
            "Symbol": symbol, "Quantity": quantity,
            "OrderType": order_type.title(), "TradeAction": action,
            "TimeInForce": {"Duration": tif.upper()},
            "OrderConfirmID": client_order_id(str(command["id"]), max_length=22),
        }
        route = options.get("route", self.config.get("route"))
        if route:
            payload["Route"] = route
        if order_type == "limit":
            if command.get("limit_price") is None:
                raise UnsupportedOrder("TradeStation limit orders require limit_price")
            price = decimal_string(command["limit_price"])
            if Decimal(price) <= 0:
                raise UnsupportedOrder("TradeStation limit_price must be positive")
            payload["LimitPrice"] = price
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("Market orders must not include limit_price")
        return payload

    def place_order(self, command):
        payload = self.build_order(command)
        if not self.config.get("account_id"):
            raise ConfigurationError("TradeStation order submission requires a single account_id")
        result = self._request("POST", "/orderexecution/orders", json=payload)
        if not result.get("Orders"):
            raise BrokerError("TradeStation returned no order acknowledgement; reconcile before resubmitting")
        if any(order.get("Error") for order in result["Orders"]):
            raise BrokerError("TradeStation rejected the order: " + str(result["Orders"]))
        return result

    def positions(self):
        positions = []
        accounts = self._accounts()
        for offset in range(0, len(accounts), 10):
            segment = ",".join(quote(account, safe="") for account in accounts[offset:offset + 10])
            result = self._request("GET", f"/brokerage/accounts/{segment}/positions")
            rows = result.get("Positions")
            if not isinstance(rows, list):
                raise BrokerError("Unexpected TradeStation positions response")
            positions.extend(row for row in rows if self.is_us_stock_order(row))
        return positions

    def capabilities(self):
        return {
            "broker": "tradestation", "scope": ["us_equity"],
            "order_types": ["market", "limit"], "time_in_force": ["day", "gtc", "ioc", "fok"],
            "broker_order_types": ["Market", "Limit", "StopMarket", "StopLimit"],
            "limitations": ["Duration availability depends on asset, route, session, and account.",
                            "US stocks, whole quantities and single-leg orders only."],
            "routing": "options.route is sent as the official Route parameter; no route is invented or auto-selected.",
            "tradability": "Symbol details plus quote MarketFlags; account eligibility remains unknown.",
            "borrow_availability": {
                "support": "partial", "scope": "US equity stock borrow",
                "fields": ["reported_hard_to_borrow", "account_stock_locate_eligibility"],
                "limitations": "The public v3 specification exposes no borrow-inventory quantity, daily fee rate, "
                               "or locate-reservation endpoint. IsHardToBorrow false does not prove shortability.",
            },
            "sources": ["https://api.tradestation.com/docs/specification/"],
            "luld": {
                "dedicated_auction_instruction": False, "auction_participation_confirmed": False,
                "ordinary_resting_order": {"order_type": "limit", "time_in_force": "day"},
                "guidance": "An ordinary limit DAY order can be submitted to an explicitly configured route, "
                            "subject to broker acceptance. Confirm that route's halt-auction eligibility with "
                            "TradeStation. FOK cannot wait/rest for reopening and is never substituted for DAY.",
                "status_source": "Quote MarketFlags.IsHalted; IsDelayed is reported and delayed/missing flags "
                                 "do not establish a current halt state.",
            },
        }

    def _symbol_details(self, symbol):
        details = self._request("GET", "/marketdata/symbols/" + quote(symbol, safe=""))
        instruments = details.get("Symbols")
        if not isinstance(instruments, list):
            raise BrokerError("TradeStation omitted symbol metadata")
        matches = [row for row in instruments if isinstance(row, dict) and str(row.get("Symbol", "")).upper() == symbol]
        if len(matches) != 1 or matches[0].get("AssetType") is None:
            raise BrokerError("TradeStation cannot verify the requested instrument's asset class")
        return matches[0]

    def is_us_stock_order(self, row):
        if not isinstance(row, dict):
            raise BrokerError("Unexpected TradeStation instrument record")
        if row.get("AssetType") is not None and row["AssetType"] != "STOCK":
            return False
        if row.get("Market") is not None and str(row["Market"]).upper() not in {"US", "USA"}:
            return False
        legs = row.get("Legs")
        if legs is not None:
            if not isinstance(legs, list) or len(legs) != 1:
                return False
            return self.is_us_stock_order(legs[0])
        try:
            symbol = us_stock_symbol(row.get("Symbol"))
        except UnsupportedOrder:
            if row.get("Symbol"):
                return False
            raise BrokerError("TradeStation row omitted its symbol; US stock scope is unknown") from None
        if row.get("AssetType") == "STOCK":
            return True
        return self._symbol_details(symbol)["AssetType"] == "STOCK"

    def tradability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        encoded = quote(symbol, safe="")
        instrument = self._symbol_details(symbol)
        if instrument["AssetType"] != "STOCK":
            raise UnsupportedOrder("TradeStation instrument is not a US stock")
        details = {"Symbols": [instrument]}
        quotes = self._request("GET", "/marketdata/quotes/" + encoded)
        instruments = details.get("Symbols", [])
        rows = quotes.get("Quotes", [])
        instrument = next((row for row in instruments if row.get("Symbol", "").upper() == symbol.upper()), None)
        snapshot = next((row for row in rows if row.get("Symbol", "").upper() == symbol.upper()), None)
        flags = (snapshot or {}).get("MarketFlags") or {}
        reported = flags.get("IsHalted")
        delayed = flags.get("IsDelayed")
        # Even a real-time unhalted quote does not prove the account can trade.
        halted = reported if isinstance(reported, bool) and delayed is False else None
        return {
            "symbol": symbol, "broker": "tradestation",
            "scope": "Instrument metadata and market-data halt flag; account eligibility is unknown",
            "tradable": False if halted is True else None,
            "instrument_found": instrument is not None,
            "status": "halted" if halted is True else "not_halted" if halted is False else "unknown",
            "halted": halted, "reported_halted": reported, "data_delayed": delayed,
            "reason": "An instrument or quote match does not prove account, route, or order eligibility. "
                      "Only an explicitly nondelayed halt flag is used as current evidence.",
            "raw": {"symbols": details, "quotes": quotes},
        }

    def borrow_availability(self, symbol):
        require_us_equity({}, self.config)
        symbol = us_stock_symbol(symbol)
        if self._symbol_details(symbol)["AssetType"] != "STOCK":
            raise UnsupportedOrder("TradeStation stock borrow applies only to US stocks")
        quotes = self._request("GET", "/marketdata/quotes/" + quote(symbol, safe=""))
        snapshot = next((row for row in quotes.get("Quotes", [])
                         if row.get("Symbol", "").upper() == symbol.upper()), None)
        flags = (snapshot or {}).get("MarketFlags") or {}
        reported = flags.get("IsHardToBorrow")
        delayed = flags.get("IsDelayed")
        htb = reported if isinstance(reported, bool) and delayed is False else None
        result = {
            "broker": "tradestation", "symbol": symbol, "support": "partial",
            "scope": "US equity market-data borrow classification and account locate eligibility",
            "shortable": None, "easy_to_borrow": False if htb is True else None,
            "hard_to_borrow": htb, "reported_hard_to_borrow": reported, "data_delayed": delayed,
            "quantity": None, "fee_rate": None, "locate_required": None,
            "account_stock_locate_eligibility": {}, "inventory_reserved": False,
            "reason": "The public v3 API exposes an HTB quote flag and account locate eligibility, "
                      "not available share quantity, borrow fee rate, or confirmed symbol shortability. "
                      "A false HTB flag does not prove ETB inventory; account locate eligibility is not a locate approval.",
            "raw": {"quotes": quotes},
        }
        try:
            account_ids = set(self._accounts())
            accounts = self._request("GET", "/brokerage/accounts")
            selected = [row for row in accounts.get("Accounts", []) if row.get("AccountID") in account_ids]
            result["raw"]["selected_accounts"] = selected
            for row in selected:
                eligible = (row.get("AccountDetail") or {}).get("IsStockLocateEligible")
                result["account_stock_locate_eligibility"][row["AccountID"]] = eligible if isinstance(eligible, bool) else None
        except BrokerError as exc:
            result["account_lookup_error"] = str(exc)
            result["reason"] += " Account locate eligibility could not be retrieved."
        return result

    def _orders(self, path, extra=None):
        params = {"pageSize": 600, **(extra or {})}
        seen_tokens, results = set(), []
        for _ in range(int(self.config.get("max_pages", 10000))):
            result = self._request("GET", path, params=params.copy())
            rows = result.get("Orders")
            if not isinstance(rows, list):
                raise BrokerError("Unexpected TradeStation orders response")
            results.extend(rows)
            token = result.get("NextToken")
            if not token:
                return results
            if token in seen_tokens:
                raise BrokerError("TradeStation repeated its page token; refusing an incomplete result")
            seen_tokens.add(token)
            params["nextToken"] = token
        raise BrokerError("TradeStation max_pages reached; order history is incomplete")

    def orders_today(self, start, end):
        """Orders opened in [start,end), including open and completed orders."""
        accounts, collected = self._accounts(), {}
        # Widen the server's date-only filter across timezone boundaries. The
        # exact configured local-day window is applied to timestamps below.
        since = (start - timedelta(days=1)).date().isoformat()
        for offset in range(0, len(accounts), 10):
            segment = ",".join(quote(account, safe="") for account in accounts[offset:offset + 10])
            base = f"/brokerage/accounts/{segment}"
            historical = self._orders(base + "/historicalorders", {"since": since})
            current = self._orders(base + "/orders")
            for order in historical + current:
                stamp, order_id = order.get("OpenedDateTime"), order.get("OrderID")
                if not stamp or not order_id:
                    raise BrokerError("TradeStation order is missing its ID or opened time; completeness is unknown")
                if in_window(stamp, start, end) and self.is_us_stock_order(order):
                    collected[(order.get("AccountID"), order_id)] = order
        return sorted(collected.values(), key=lambda row: row["OpenedDateTime"], reverse=True)
