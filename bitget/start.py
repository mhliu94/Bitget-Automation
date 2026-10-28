#!/usr/bin/env python3
"""Load local Bitget credentials, check connectivity, or run the Kafka worker."""
import argparse
import configparser
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import load_config, resolve_env
from common.errors import BrokerError, ConfigurationError


def load_credentials(path):
    properties = configparser.ConfigParser(interpolation=None)
    try:
        properties.read_string("[credentials]\n" + Path(path).expanduser().read_text())
    except (OSError, configparser.Error, UnicodeError):
        raise ConfigurationError("Cannot read Bitget credentials properties file") from None
    fields = {"client_id": "BITGET_API_KEY", "client_secret": "BITGET_API_SECRET",
              "passphrase": "BITGET_PASSPHRASE"}
    for field, variable in fields.items():
        value = properties["credentials"].get(field, "").strip()
        if not value:
            raise ConfigurationError(f"Credentials file requires {field}")
        os.environ[variable] = value


def check(config_path):
    from bitget.adapter import Adapter
    from common.snapshots import collect_account, collect_orders
    from common.utils import today_window
    from kafka import KafkaConsumer

    config, selected = load_config(config_path, "bitget", execute=True)
    settings = resolve_env(config["kafka"])
    connection = {key: settings[key] for key in (
        "security_protocol", "sasl_mechanism", "sasl_plain_username",
        "sasl_plain_password", "ssl_cafile", "ssl_certfile", "ssl_keyfile"
    ) if settings.get(key)}
    consumer = KafkaConsumer(
        bootstrap_servers=settings["bootstrap_servers"], group_id=None,
        enable_auto_commit=False, allow_auto_create_topics=False,
        client_id="bitget-lyj-preflight", **connection,
    )
    try:
        required = {settings["command_topic"], settings["account_topic"], settings["order_topic"]}
        if required - consumer.topics():
            raise ConfigurationError("Required Kafka topics are missing")
    finally:
        consumer.close()
    adapter = Adapter(selected)
    account = collect_account(adapter, "bitget")
    errors = []
    orders = collect_orders(adapter, "bitget", *today_window(selected["timezone"]), errors=errors)
    if errors:
        raise ConfigurationError("Stock+ order snapshot mapping failed")
    if adapter._stock_instrument("AAPL.US") is None:
        raise ConfigurationError("Stock+ instrument access check failed")
    print(json.dumps({"status": "preflight_ok", "broker": "bitget",
                      "routing": selected["routing"],
                      "kafka": settings["bootstrap_servers"],
                      "position_count": len(account["positions"]),
                      "order_count": len(orders)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", default="/root/bitget-configs/config.properties")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--check", action="store_true", help="Read-only broker and Kafka preflight")
    parser.add_argument("--execute", action="store_true", help="Start the live Kafka listener")
    args = parser.parse_args()
    if not args.check and not args.execute:
        parser.error("Choose --check or --execute")
    os.umask(0o077)
    try:
        load_credentials(args.credentials)
        if args.check:
            check(args.config)
            return 0
        from common.cli import main as cli_main
        return cli_main("bitget", "consume", ["--config", args.config, "--execute"])
    except BrokerError as exc:
        message = str(exc)
        for variable in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"):
            if os.environ.get(variable):
                message = message.replace(os.environ[variable], "[redacted]")
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": message}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "error": "Startup check failed; verify connectivity and dependencies"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
