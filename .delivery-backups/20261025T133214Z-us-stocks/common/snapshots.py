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
    if broker == "bitget":
        cash={}; mapped=[]
        for row in positions:
            coin=row.get("coin")
            if not isinstance(coin,str) or not coin:raise BrokerError("Bitget asset omitted coin")
            parts=[number(row.get(key),key) for key in ("available","frozen","locked")]
            qty=sum(parts)
            if coin=="USD":cash[coin]=qty
            elif qty:mapped.append(position(coin,qty,available_qty=parts[0]))
        return {"cash":cash.get("USD",0),"cash_by_currency":cash,"positions":mapped,
                "cash_basis":"fiat_USD_only", "note":"Crypto and stablecoins remain coin positions; no USD peg or cost basis is inferred."}
    if broker == "robinhood":
        if adapter.config.get("snapshot_cash_basis")!="buying_power":
            raise ConfigurationError("Robinhood Crypto exposes buying power, not cash; explicitly set snapshot_cash_basis=buying_power to use spendable funds in the legacy cash field")
        expected=adapter.config.get("account_number")
        accounts=[row for row in adapter._pages(adapter.prefix+"accounts/",{}) if row.get("account_number")==expected]
        if len(accounts)!=1:raise BrokerError("Robinhood account is missing or ambiguous")
        account=accounts[0];currency=account.get("buying_power_currency")
        if not isinstance(currency,str) or not re.fullmatch("[A-Z]{3}",currency):raise BrokerError("Robinhood account omitted buying-power currency")
        available={currency:number(account.get("buying_power"),"buying power")}
        mapped=[]
        for row in positions:
            if row.get("account_number")!=expected:raise BrokerError("Robinhood holding account does not match")
            mapped.append(position(row.get("asset_code"),row.get("total_quantity"),available_qty=number(row.get("quantity_available_for_trading"),"available quantity",True)))
        return {"cash":available.get("USD",0),"cash_by_currency":dict(available),"available_cash_by_currency":available,
                "positions":mapped,"cash_basis":"crypto_buying_power_explicit_proxy"}
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
    if broker=="bitget":
        kind={"market":"MARKET","limit":"LIMIT"}.get(row.get("orderType"),"OTHER")
        statuses={"live":"NEW","partially_filled":"PARTIALLY_FILLED","filled":"FILLED","cancelled":"CANCELED"}
        quote_size=kind=="MARKET" and row.get("side")=="buy"
        result=base_order(row.get("orderId"),row.get("cTime"),row.get("uTime") or observed_at,row.get("symbol"),
                          str(row.get("side","")).upper(),kind,statuses.get(row.get("status"),"UNKNOWN"),
                          None if quote_size else row.get("size"),row.get("baseVolume"),row.get("priceAvg"))
        if quote_size:result["notional"]=number(row.get("size"),"quote notional")
        if kind=="LIMIT":result["limit_price"]=number(row.get("price"),"limit price")
        if row.get("clientOid"):result["client_order_id"]=row["clientOid"]
        if row.get("force"):result["time_in_force"]=row["force"]
        result["message"]="Quote asset: "+str(row.get("quoteCoin","unavailable"))
        return result
    if broker=="robinhood":
        kind={"market":"MARKET","limit":"LIMIT","stop_loss":"STOP","stop_limit":"STOP_LIMIT"}.get(row.get("type"),"OTHER")
        cfg=row.get(str(row.get("type"))+"_order_config") or {}
        statuses={"open":"NEW","canceled":"CANCELED","partially_filled":"PARTIALLY_FILLED","filled":"FILLED","failed":"REJECTED"}
        result=base_order(row.get("id"),row.get("created_at"),row.get("updated_at") or observed_at,row.get("symbol"),
                          str(row.get("side","")).upper(),kind,statuses.get(row.get("state"),"UNKNOWN"),
                          cfg.get("asset_quantity"),row.get("filled_asset_quantity"),row.get("average_price"))
        for src,dest in (("limit_price","limit_price"),("stop_price","stop_price"),("quote_amount","notional")):
            if cfg.get(src) is not None:result[dest]=number(cfg[src],src)
        if row.get("client_order_id"):result["client_order_id"]=row["client_order_id"]
        if cfg.get("time_in_force"):result["time_in_force"]=cfg["time_in_force"]
        return result
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
                if broker=="bitget" and (row.get("price") is None or row.get("baseVolume") is None):
                    matches=adapter._request("GET","/api/v2/spot/trade/orderInfo",{"orderId":row.get("orderId")})
                    if not isinstance(matches,list) or len(matches)!=1 or matches[0].get("orderId")!=row.get("orderId"):
                        raise BrokerError("Bitget order detail identity mismatch")
                    row=matches[0]
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
