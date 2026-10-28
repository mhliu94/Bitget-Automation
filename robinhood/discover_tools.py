#!/usr/bin/env python3
"""Export authenticated Robinhood MCP tool schemas; never invoke a trading tool."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from common.errors import BrokerError
from robinhood.adapter import Adapter, ENDPOINT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", help="Shared configuration with brokers.robinhood")
    parser.add_argument("--execute", action="store_true", help="Authorize read-only MCP initialize/tools/list requests")
    parser.add_argument("--output", help="Save the server tool schemas to this JSON file; default stdout")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "dry_run", "endpoint": ENDPOINT,
                          "operations": ["initialize", "notifications/initialized", "tools/list"],
                          "note": "Pass --execute with OAuth configuration to read authenticated schemas. No trading tools are called."}, indent=2))
        return 0
    try:
        _, config = load_config(args.config, "robinhood", execute=True)
        result = Adapter(config).discover_tools()
        encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).expanduser().write_text(encoded)
            print(json.dumps({"status": "ok", "tools": len(result["tools"]), "output": args.output}))
        else:
            print(encoded, end="")
    except BrokerError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"status": "error", "error": "Could not write schema output file"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
