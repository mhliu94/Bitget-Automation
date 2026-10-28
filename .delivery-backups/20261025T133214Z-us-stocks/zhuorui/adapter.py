"""Run a pinned upstream Zhuorui runtime in an isolated Python process."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys

from common.errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown, UnsupportedOrder
from common.utils import decimal_string, iso_utc

PINNED_COMMIT = "002aa6866b246dbc4c1f5e1f647019abc170080d"


class Adapter:
    def __init__(self, config: dict):
        self.config = dict(config)
        for key in ("allow_timed_cancel_fok", "allow_synthetic_extended_hours_market"):
            if key in self.config and not isinstance(self.config[key], bool):
                raise ConfigurationError(f"Zhuorui {key} must be a boolean")

    def capabilities(self):
        return {"broker": "zhuorui", "scope": "US whole-share equities through the pinned user-supplied runtime",
                "order_types": ["market", "limit"], "limit_time_in_force": ["day"],
                "native_fok": False, "timed_cancel_opt_in": self.config.get("allow_timed_cancel_fok", False),
                "timed_cancel_partial_fills_possible": True,
                "market_regular_session_only": not self.config.get("allow_synthetic_extended_hours_market", False),
                "luld_applicable": True, "luld_auction_participation": None,
                "luld_note": "A regular-session DAY limit can be submitted; routing/acceptance at a LULD reopening auction is unverified",
                "tradability_scope": "Read-only symbol quote/suspension and US session; account permission and auction eligibility remain unknown",
                "borrow_availability": "Unknown: no verified borrow/locate endpoint in the pinned upstream API",
                "runtime_platform": "Windows under the user who imported the DPAPI session",
                "upstream_commit": PINNED_COMMIT}

    def build_order(self, command: dict):
        if command.get("asset_class", "equity") not in ("equity", "equities", "stock", "stocks", "us_equity"):
            raise UnsupportedOrder("Zhuorui bridge supports US equities only")
        symbol = self._symbol(command.get("symbol"))
        side, kind = command.get("side"), command.get("order_type")
        if side not in ("buy", "sell") or kind not in ("market", "limit"):
            raise UnsupportedOrder("Zhuorui requires buy/sell and market/limit")
        quantity = Decimal(decimal_string(command.get("quantity")))
        if quantity != quantity.to_integral_value():
            raise UnsupportedOrder("Zhuorui requires whole shares")
        tif = command.get("time_in_force", "day")
        timed = tif == "fok"
        if tif not in ("day", "fok"):
            raise UnsupportedOrder("Zhuorui supports DAY; native GTC/IOC/FOK are unverified")
        if timed and (kind != "limit" or not self.config.get("allow_timed_cancel_fok", False)):
            raise UnsupportedOrder("Zhuorui has no native FOK; timed limit cancellation requires allow_timed_cancel_fok=true and can partially fill")
        options = command.get("options") or {}
        if set(options) - {"allow_pre_post"}:
            raise UnsupportedOrder("Zhuorui supports only options.allow_pre_post")
        allow_pre_post = options.get("allow_pre_post", False)
        if not isinstance(allow_pre_post, bool):
            raise UnsupportedOrder("allow_pre_post must be boolean")
        if kind == "market" and allow_pre_post:
            raise UnsupportedOrder("Zhuorui native market orders do not support allow_pre_post")
        price = None
        if kind == "limit":
            price = decimal_string(command.get("limit_price"))
            if Decimal(price) != Decimal(price).quantize(Decimal("0.01")):
                raise UnsupportedOrder("Zhuorui upstream rounds limits to cents; supply a cent-aligned price to preserve your limit")
        elif command.get("limit_price") is not None:
            raise UnsupportedOrder("Zhuorui market orders must not have a limit_price")
        return {"command_id": command["id"], "symbol": symbol, "side": side,
                "quantity": int(quantity), "order_type": "fok" if timed else kind,
                "limit_price": price, "allow_pre_post": allow_pre_post,
                "native_fok": False, "timed_cancel": timed,
                "partial_fills_possible": timed,
                "execution_semantics": "limit_then_timed_cancel" if timed else kind}

    @staticmethod
    def _symbol(symbol):
        symbol = str(symbol or "").upper()
        if not re.fullmatch(r"[A-Z0-9.=\-]{1,16}", symbol):
            raise UnsupportedOrder("Zhuorui requires a US stock symbol")
        return symbol

    def _expected_binding(self):
        keys = ("account_id", "account_num_id", "server_id")
        routing = self.config.get("routing")
        if routing is not None and not isinstance(routing, dict):
            raise ConfigurationError("Zhuorui routing must be an object")
        if routing is not None and not any(routing.get(key) not in (None, "") for key in ("account_id", "account_num_id")):
            raise ConfigurationError("Live Zhuorui bridge routing requires account_id or account_num_id")
        expected = {}
        for key in keys:
            values = []
            for section in (self.config, routing or {}):
                value = section.get(key)
                if value in (None, ""):
                    continue
                if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
                    raise ConfigurationError(f"Zhuorui {key} must be a valid account/server selector")
                normalized = str(value).strip()
                if key == "account_num_id":
                    if not normalized.isdigit():
                        raise ConfigurationError("Zhuorui account_num_id must be an integer")
                    normalized = str(int(normalized))
                values.append(normalized)
            if len(set(values)) > 1:
                raise ConfigurationError(f"Zhuorui routing.{key} conflicts with legacy top-level {key}")
            if values:
                expected[key] = values[0]
        return expected

    def _bridge(self, operation: str, payload=None):
        for key in ("upstream_root", "upstream_config"):
            if not self.config.get(key):
                raise ConfigurationError(f"Zhuorui requires {key}; see zhuorui/README.md")
        request = {"operation": operation, "payload": payload or {},
                   "repo": str(Path(self.config["upstream_root"]).expanduser().resolve()),
                   "config": str(Path(self.config["upstream_config"]).expanduser().resolve()),
                   "expected_commit": PINNED_COMMIT,
                   "expected_binding": self._expected_binding(),
                   "allow_synthetic_extended_hours_market": self.config.get("allow_synthetic_extended_hours_market", False)}
        script = str(Path(__file__).with_name("bridge_worker.py"))
        try:
            result = subprocess.run([self.config.get("python_executable", sys.executable), "-I", script],
                                    input=json.dumps(request), text=True, capture_output=True,
                                    timeout=float(self.config.get("bridge_timeout_seconds", 180)), check=False)
        except subprocess.TimeoutExpired as exc:
            raise OutcomeUnknown("Zhuorui worker timed out; check the upstream journal and account before retrying any order") from exc
        except OSError as exc:
            raise ConfigurationError("Cannot launch Zhuorui Python; check python_executable and upstream setup") from exc
        try:
            response = json.loads(result.stdout)
        except ValueError as exc:
            raise OutcomeUnknown("Zhuorui worker returned no valid result; inspect its journal before retrying") from exc
        if result.returncode or not response.get("ok"):
            error = response.get("error", "Zhuorui bridge failed")
            if (response.get("authentication_error") is True and response.get("no_submission") is True
                    and operation in {"positions", "orders_today", "tradability", "account_snapshot", "order_snapshots"}):
                raise AuthenticationError("Zhuorui session expired or was displaced; refresh the session before continuing", no_submission=True)
            if response.get("outcome_unknown"):
                raise OutcomeUnknown(error)
            raise BrokerError(error)
        return response["result"]

    def place_order(self, command):
        result = self._bridge("place_order", self.build_order(command))
        status = result.get("status")
        if status in ("unknown", "cancel_unknown", "cancel_rejected"):
            raise OutcomeUnknown(result.get("message", "Zhuorui order outcome needs reconciliation"))
        if status in ("rejected", "blocked", "disabled"):
            raise BrokerError(result.get("message", "Zhuorui order was not submitted"))
        return result

    def positions(self):
        return self._bridge("positions")

    def orders_today(self, start: datetime, end: datetime):
        return self._bridge("orders_today", {"start": iso_utc(start), "end": iso_utc(end)})

    def tradability(self, symbol):
        return self._bridge("tradability", {"symbol": self._symbol(symbol)})

    def borrow_availability(self, symbol):
        return {"symbol": self._symbol(symbol), "supported": False, "status": "unverified_api",
                "shortable": None, "easy_to_borrow": None, "available_quantity": None,
                "fee_rate": None, "locate_required": None,
                "reason": "The pinned Zhuorui client exposes no verified stock-borrow or locate endpoint. Available owned shares and quote visibility cannot establish borrow inventory.",
                "source": "https://github.com/mhliu94/Zhuorui-Securities-Automation/blob/" + PINNED_COMMIT + "/zhuorui/api/client.py"}

    def recover_cancellations(self):
        """Resume only already-journaled pending timed cancellations; never replay orders."""
        return self._bridge("recover_cancellations")
