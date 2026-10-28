#!/usr/bin/env python3
"""Read public Nasdaq halt announcements; does not place orders."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import BrokerError
from common.halts import query_halts
from common.journal import encoded

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    args = parser.parse_args()
    try:
        print(encoded(query_halts(args.symbol)))
    except BrokerError as exc:
        print(encoded({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
