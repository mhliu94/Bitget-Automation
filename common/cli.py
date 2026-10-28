import argparse
import json
from pathlib import Path
import sys
import uuid
from .commands import decode_command
from .config import load_config
from .consumer import consume
from .errors import BrokerError, CommandError
from .journal import encoded
from .runner import Runner

ROOT = Path(__file__).resolve().parent.parent


def main(broker, fixed_action="command", argv=None):
    parser = argparse.ArgumentParser(description=f"{broker}: {fixed_action}; broker operations are previewed unless --execute is supplied")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--execute", action="store_true", help="Perform broker reads/submissions; orders also require trading_enabled=true")
    parser.add_argument("--journal", help="Persistent SQLite deduplication journal (default .state/<broker>.sqlite3)")
    if fixed_action == "consume":
        parser.add_argument("--max-messages", type=int, default=0, help="Stop after N records; zero means continuous")
    elif fixed_action == "command":
        parser.add_argument("--file", required=True, help="JSON command path, or - for stdin")
    else:
        parser.add_argument("--id", help="Stable command ID, required for mutations; use the same ID after an uncertain result")
        if fixed_action in {"market", "limit", "fok", "tradability", "borrow_availability"}:
            parser.add_argument("--symbol", required=True)
        if fixed_action == "cancel_order":
            parser.add_argument("--order-id", required=True)
            parser.add_argument("--symbol", help="Broker-native symbol, required by brokers that need it for cancellation")
        if fixed_action == "set_trading_enabled":
            parser.add_argument("--enabled", choices=["true", "false"], required=True)
        if fixed_action in {"market", "limit", "fok"}:
            parser.add_argument("--side", choices=["buy", "sell"], required=True)
            parser.add_argument("--quantity", required=True, help="Exact decimal string; see broker docs for units")
            parser.add_argument("--asset-class")
            parser.add_argument("--time-in-force")
            parser.add_argument("--options-json", default="{}", help="Broker-specific options as a JSON object")
            parser.add_argument("--auction-intent", choices=["luld_reopening"])
            if fixed_action in {"limit", "fok"}:
                parser.add_argument("--limit-price", required=True)
    args = parser.parse_args(argv)
    runner = None
    try:
        config, selected = load_config(args.config, broker, execute=args.execute)
        runner = Runner(broker, selected, args.execute, args.journal or ROOT / ".state" / f"{broker}.sqlite3")
        if fixed_action == "recover_cancellations":
            result = runner.recover_pending()
            print(encoded(result))
            return 0 if result["status"] in {"ok", "dry_run"} else 2
        if fixed_action == "consume":
            if args.max_messages < 0:
                raise CommandError("max-messages must be nonnegative")
            consume(runner, config, args.max_messages)
            return 0
        if fixed_action == "command":
            payload = decode_command(sys.stdin.read(65537) if args.file == "-" else Path(args.file).read_bytes())
        else:
            is_order = fixed_action in {"market", "limit", "fok"}
            mutation = is_order or fixed_action in {"cancel_order", "cancel_open_orders", "set_trading_enabled"}
            if mutation and not args.id:
                raise CommandError("Mutations require --id for durable duplicate protection")
            payload = {"id": args.id or str(uuid.uuid4()), "broker": broker,
                       "type" if is_order else "action": fixed_action}
            for key in ("symbol", "side", "quantity", "asset_class", "time_in_force", "limit_price", "auction_intent", "order_id"):
                if getattr(args, key, None) is not None:
                    payload[key] = getattr(args, key)
            if is_order:
                payload["options"] = decode_command(args.options_json)
            if fixed_action == "set_trading_enabled":
                payload["trading_enabled"] = args.enabled == "true"
        result = runner.process(payload)
        print(encoded(result))
        return 0 if result["status"] in {"ok", "submitted", "cancel_requested", "dry_run", "ignored"} else 2
    except BrokerError as exc:
        print(encoded({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(encoded({"status": "stopped"}), file=sys.stderr)
        return 130
    except Exception as exc:
        print(encoded({"status": "error", "error_type": type(exc).__name__,
                       "error": "Operation failed; check configuration, dependencies and connectivity. Reconcile any uncertain order before retrying."}), file=sys.stderr)
        return 2
    finally:
        if runner:
            runner.close()
