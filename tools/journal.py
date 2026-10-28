#!/usr/bin/env python3
"""Inspect uncertain commands or record reviewed broker evidence; never trades."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.config import load_config
from common.journal import Journal,encoded
from common.errors import BrokerError,ConfigurationError
from common.locking import operation_lock
from common.registry import BROKERS
import hashlib


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker",choices=BROKERS,required=True)
    parser.add_argument("--config",default=str(Path(__file__).resolve().parents[1]/"config.json"))
    parser.add_argument("--journal")
    parser.add_argument("--command-id")
    parser.add_argument("--outcome",choices=["not_submitted","found","canceled","filled","rejected"])
    parser.add_argument("--evidence")
    parser.add_argument("--broker-order-id")
    parser.add_argument("--execute",action="store_true",help="Record reconciliation locally after reviewing broker evidence; no order is sent")
    args=parser.parse_args()
    try:
        _,config=load_config(args.config,args.broker,False)
        if not config.get("journal_account"):raise ConfigurationError("Set the same stable journal_account used by the worker")
        path=Path(args.journal or Path(__file__).resolve().parents[1]/".state"/(args.broker+".sqlite3"))
        if not path.is_file():raise ConfigurationError("No command journal exists at this path")
        scope=args.broker+":"+config["journal_account"]
        lock=path.with_suffix("."+hashlib.sha256(scope.encode()).hexdigest()[:16]+".lock")
        with operation_lock(lock):
            journal=Journal(path)
            try:
                if args.command_id:
                    if not args.outcome or not args.evidence:raise ConfigurationError("Reconciliation requires --outcome and --evidence")
                    result=journal.reconcile(scope,args.command_id,args.outcome,args.evidence,args.broker_order_id) if args.execute else {
                        "status":"dry_run","command_id":args.command_id,"outcome":args.outcome,"orders_replayed":False}
                else:
                    result={"scope":scope,"unresolved":journal.unresolved(scope)}
                    if journal.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timed_cancellations'").fetchone():
                        from common.timed_cancel import unresolved
                        result["pending_timed_cancellations"] = unresolved(journal,scope)
                print(encoded(result))
            finally:journal.close()
        return 0
    except BrokerError as exc:
        print(encoded({"status":"error","error":str(exc)}),file=sys.stderr);return 2

if __name__=="__main__":raise SystemExit(main())
