#!/usr/bin/env python3
"""Resume previously authorized pending timed cancellations using the upstream journal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from common.errors import BrokerError
from common.registry import adapter_for


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Shared broker-api-scripts JSON config")
    parser.add_argument("--execute", action="store_true", help="Recover previously authorized pending cancellations; default is offline preview")
    args = parser.parse_args(argv)
    try:
        _, config = load_config(args.config, "zhuorui", execute=args.execute)
        if not args.execute:
            result = {"status": "dry_run", "action": "recover_cancellations", "broker": "zhuorui",
                      "request_sent": False, "orders_replayed": False,
                      "note": "Use --execute to resume only already-journaled pending timed cancellations"}
        else:
            result = adapter_for("zhuorui", config).recover_cancellations()
        print(json.dumps(result, indent=2))
        return 0
    except BrokerError as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
