#!/usr/bin/env python3
"""Inspect runner health or request graceful stop after its current operation."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.health import inspect
from common.journal import encoded
from common.registry import BROKERS

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["status","stop"])
    parser.add_argument("--broker",choices=BROKERS,required=True)
    parser.add_argument("--state-dir",default=str(Path(__file__).resolve().parents[1]/".state"))
    args=parser.parse_args();base=Path(args.state_dir)
    try:
        if args.action=="status":print(encoded(inspect(base/(args.broker+".status.json"))))
        else:
            base.mkdir(parents=True,exist_ok=True)
            (base/(args.broker+".stop")).touch()
            print(encoded({"status":"stop_requested","broker":args.broker,"note":"Current operation may finish before exit; this does not cancel broker orders."}))
    except (OSError,ValueError) as exc:
        print(encoded({"status":"unavailable","error_type":type(exc).__name__}),file=sys.stderr)
        raise SystemExit(2)
