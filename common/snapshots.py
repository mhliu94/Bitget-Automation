"""Broker observations mapped to KTrader account and order snapshot fields.

Only read broker state; never infer fills from local submission acknowledgements.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from urllib.parse import quote
from .errors import AuthenticationError, BrokerError, ConfigurationError
from .utils import iso_utc, parse_time


def number(value, field, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or value is None:
        raise BrokerError(f"Missing/invalid snapshot {field}")
    try:
        dec = Decimal(str(value))
        value = int(dec) if dec.is_finite() and dec == dec.to_integral_value() else float(dec)
        if not dec.is_finite() or not math.isfinite(value) or (dec != 0 and value == 0):
            raise ValueError
        return value
    except (ValueError, InvalidOperation, OverflowError):
        raise BrokerError(f"Invalid snapshot {field}") from None


def position(symbol, qty, cost=None, **extras):
    if not isinstance(symbol, str) or not symbol.strip():
        raise BrokerError("Position omitted symbol")
    return {"symbol":symbol, "qty":number(qty,"position quantity"),
            "avg_price":number(cost,"position cost",True), **extras}


def collect_account(adapter, broker):
    if broker in {"longbridge", "webull"}:
        from .snapshot_asia import collect_account as collect
        result = collect(adapter, broker)
        cash = result["cash_by_currency"]
        result["cash"] = cash.get("USD", 0)
        if broker == "longbridge":
            result["available_cash_by_currency"] = dict(cash)
        return result
    if broker == "zhuorui":
        result = adapter._bridge("account_snapshot")
        result["cash"] = number(result.get("cash"), "cash")
        result["cash_by_currency"] = {k:number(v,"cash "+k) for k,v in result["cash_by_currency"].items()}
        result["positions"] = [position(row.get("symbol"),row.get("qty"),row.get("avg_price")) for row in result["positions"]]
        return result
    if broker in {"bitget","robinhood"}:
        return adapter.snapshot_account()
    positions = adapter.positions()
    if broker == "alpaca":
        account = adapter.http.request("GET", "/v2/account")
        currency = account.get("currency")
        if not isinstance(currency,str) or not re.fullmatch("[A-Z]{3}",currency):
            raise BrokerError("Alpaca account omitted currency")
        cash = {currency:number(account.get("cash"),"account cash")}
        mapped=[]
        for row in positions:
            item=position(row.get("symbol"),row.get("qty"),row.get("avg_entry_price"))
            for source,dest in (("current_price","marketPrice"),("market_value","marketValue"),("unrealized_intraday_pl","dailyPL")):
                if row.get(source) is not None:item[dest]=number(row[source],source)
            mapped.append(item)
        return {"cash":cash.get("USD",0),"cash_by_currency":cash,"positions":mapped,"cash_basis":"cash_balance"}
    if broker == "tradestation":
        account_id=adapter.config.get("account_id")
        if not account_id or adapter._accounts()!=[account_id]:
            raise ConfigurationError("Account publication needs one TradeStation account_id, without a different account_ids set")
        response=adapter._request("GET",f"/brokerage/accounts/{quote(account_id,safe='')}/balances")
        rows=response.get("Balances")
        if not isinstance(rows,list) or len(rows)!=1 or rows[0].get("AccountID")!=account_id:
            raise BrokerError("TradeStation balance account is missing or ambiguous")
        row=rows[0]
        if row.get("CurrencyDetails"):
            cash={r["Currency"]:number(r.get("CashBalance"),"currency cash") for r in row["CurrencyDetails"]}
        else:
            cash={adapter.config.get("account_currency","USD"):number(row.get("CashBalance"),"cash")}
        mapped=[]
        for row in positions:
            if row.get("AccountID")!=account_id:raise BrokerError("Position belongs to a different TradeStation account")
            mapped.append(position(row.get("Symbol"),row.get("Quantity"),row.get("AveragePrice")))
        return {"cash":cash.get("USD",0),"cash_by_currency":cash,"positions":mapped,"cash_basis":"cash_balance"}
    raise ConfigurationError("No account snapshot mapper for broker")


def base_order(identity,created,updated,symbol,side,kind,status,qty,filled,average):
    if not isinstance(identity,str) or not identity:raise BrokerError("Order omitted exact string ID")
    filled=number(filled,"filled quantity")
    if filled<0:raise BrokerError("Filled quantity is negative")
    average=number(average,"average fill price",True) if filled else None
    if filled and average is None:raise BrokerError("Filled order omitted average execution price")
    if side not in {"BUY","SELL"}:raise BrokerError("Unknown order side")
    return {"order_id":identity,"created_at":iso_utc(parse_time(created)),"updated_at":iso_utc(parse_time(updated)),
            "symbol":symbol,"side":side,"order_type":kind,"status":status,
            "quantity":number(qty,"order quantity",True),"filled_quantity":filled,"average_fill_price":average}


def map_order(broker,row,observed_at):
    observed_at=iso_utc(observed_at) if isinstance(observed_at,datetime) else observed_at
    if broker in {"longbridge","webull"}:
        from .snapshot_asia import map_order as mapper
        return mapper(broker,row,observed_at)
    if broker=="alpaca":
        statuses={"accepted":"PENDING_NEW","pending_new":"PENDING_NEW","accepted_for_bidding":"PENDING_NEW",
                  "new":"NEW","partially_filled":"PARTIALLY_FILLED","pending_cancel":"PENDING_CANCEL",
                  "filled":"FILLED","canceled":"CANCELED","expired":"EXPIRED","rejected":"REJECTED",
                  "pending_replace":"NEW","replaced":"CANCELED"}
        kind={"market":"MARKET","limit":"LIMIT","stop":"STOP","stop_limit":"STOP_LIMIT","trailing_stop":"TRAILING_STOP"}.get(row.get("type"),"OTHER")
        result=base_order(row.get("id"),row.get("submitted_at") or row.get("created_at"),row.get("updated_at") or observed_at,
                          row.get("symbol"),str(row.get("side","")).upper(),kind,statuses.get(row.get("status"),"UNKNOWN"),
                          row.get("qty"),row.get("filled_qty"),row.get("filled_avg_price"))
        for field in ("limit_price","stop_price","notional"):
            if row.get(field) is not None:result[field]=number(row[field],field)
        for field in ("client_order_id","time_in_force"):
            if row.get(field):result[field]=row[field]
        return result
    if broker=="tradestation":
        legs=row.get("Legs")
        if not isinstance(legs,list) or len(legs)!=1:raise BrokerError("KTrader single-symbol snapshot cannot represent a multi-leg TradeStation order")
        leg=legs[0]
        statuses={"ACK":"NEW","OPN":"NEW","DIS":"NEW","DON":"PENDING_NEW","PLA":"PENDING_NEW","FPR":"PARTIALLY_FILLED",
                  "UCN":"PENDING_CANCEL","ECN":"PENDING_CANCEL","FLL":"FILLED","FLP":"CANCELED","CAN":"CANCELED",
                  "EXP":"EXPIRED","OUT":"CANCELED","TSC":"CANCELED","SCN":"CANCELED","REJ":"REJECTED"}
        side={"Buy":"BUY","BuyToCover":"BUY","Sell":"SELL","SellShort":"SELL"}.get(leg.get("BuyOrSell"))
        kind={"Market":"MARKET","Limit":"LIMIT","StopMarket":"STOP","StopLimit":"STOP_LIMIT"}.get(row.get("OrderType"),"OTHER")
        result=base_order(row.get("OrderID"),row.get("OpenedDateTime"),observed_at,leg.get("Symbol"),side,kind,
                          statuses.get(row.get("Status"),"UNKNOWN"),leg.get("QuantityOrdered"),leg.get("ExecQuantity"),row.get("FilledPrice"))
        for src,dest in (("LimitPrice","limit_price"),("StopPrice","stop_price")):
            if row.get(src) is not None:result[dest]=number(row[src],src)
        for src,dest in (("Currency","currency"),("Duration","time_in_force")):
            if row.get(src):result[dest]=row[src]
        result["message"]=str(row.get("StatusDescription") or row.get("Status"))[:2000]
        return result
    if broker in {"bitget","robinhood"}:
        from .registry import adapter_for
        return adapter_for(broker,{}).snapshot_order(row,observed_at)
    raise ConfigurationError("No order snapshot mapper for broker")


def collect_orders(adapter,broker,start,end,now=None,errors=None):
    if errors is not None and not isinstance(errors,list):
        raise ConfigurationError("Order mapping errors must be a list")
    now=now or datetime.now(timezone.utc)
    if broker in {"longbridge","webull"}:
        from .snapshot_asia import collect_orders as collect
        return collect(adapter,broker,start,end,errors=errors)
    if broker=="zhuorui":
        if adapter.config.get("timezone","America/New_York")!="America/New_York":
            raise ConfigurationError("Zhuorui upstream snapshot mapper requires America/New_York")
        payload={"start":iso_utc(start),"end":iso_utc(end)}
        if errors is not None:payload["report_errors"]=True
        rows = adapter._bridge("order_snapshots",payload)
        if errors is not None:
            if not isinstance(rows,dict) or not isinstance(rows.get("errors"),list) or not isinstance(rows.get("orders"),list):
                raise BrokerError("Unexpected Zhuorui order snapshot batch")
            for error in rows["errors"]:
                if not isinstance(error,dict) or not {"order_id","error_type","error"} <= error.keys():
                    raise BrokerError("Unexpected Zhuorui order snapshot error")
            errors.extend(rows["errors"])
            rows=rows["orders"]
    else:
        # Transport/account-wide query failures are never converted to an
        # apparently successful empty result, even in partial-report mode.
        rows=adapter.orders_today(start,end)
    if not isinstance(rows,list):
        raise BrokerError("Unexpected broker order snapshot list")
    output=[]
    for row in rows:
        original=row
        try:
            if not isinstance(row,dict):raise BrokerError("Unexpected order record")
            if broker=="zhuorui":
                row=dict(row)
                for key in ("quantity","filled_quantity","average_fill_price","limit_price","stop_price","notional"):
                    if key in row:row[key]=number(row[key],key,nullable=key in {"quantity","average_fill_price","limit_price","stop_price"})
                output.append(row)
            else:
                if broker in {"bitget","robinhood"}:
                    output.append(adapter.snapshot_order(row,now))
                else:
                    output.append(map_order(broker,row,now))
        except AuthenticationError:
            raise
        except Exception as exc:
            if errors is None:raise
            identity=None
            if isinstance(original,dict):
                for key in ("order_id","id","OrderID","orderId"):
                    if original.get(key) is not None:
                        identity=original[key]
                        break
            errors.append({"order_id":str(identity) if isinstance(identity,(str,int)) and not isinstance(identity,bool) else None,
                           "error_type":type(exc).__name__,
                           "error":str(exc) if isinstance(exc,BrokerError) else "Order snapshot mapping or detail lookup failed"})
    return output
