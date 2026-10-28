from datetime import datetime,timezone
from unittest.mock import Mock
import pytest
from common.snapshots import collect_account,collect_orders,map_order
from common.publication import validate_order_snapshot
from common.errors import BrokerError,ConfigurationError

NOW=datetime(2026,10,25,15,tzinfo=timezone.utc)
STAMP="2026-10-25T14:00:00Z"


def canonical(row):
    return validate_order_snapshot({**row,"schema_version":1,"account_id":"ACC"})


def test_alpaca_account_and_negative_short_position():
    adapter=Mock()
    adapter.positions.return_value=[{"symbol":"AAPL","qty":"-3","avg_entry_price":"12.5","market_value":"-42"}]
    adapter.http.request.return_value={"currency":"USD","cash":"0"}
    data=collect_account(adapter,"alpaca")
    assert data["cash"]==0 and data["positions"][0]["qty"]==-3
    assert data["positions"][0]["marketValue"]==-42


def test_trade_station_cash_and_account_isolation():
    adapter=Mock(config={"account_id":"a"})
    adapter._accounts.return_value=["a"]
    adapter._request.return_value={"Balances":[{"AccountID":"a","CashBalance":"42.5"}]}
    adapter.positions.return_value=[{"AccountID":"a","Symbol":"AAPL","Quantity":"2","AveragePrice":"5"}]
    assert collect_account(adapter,"tradestation")["cash"]==42.5
    adapter._accounts.return_value=["a","b"]
    with pytest.raises(ConfigurationError):collect_account(adapter,"tradestation")


def test_crypto_balances_never_assume_stablecoin_usd_peg():
    adapter=Mock()
    adapter.positions.return_value=[{"coin":"USDT","available":"90","frozen":"10","locked":"2"}]
    data=collect_account(adapter,"bitget")
    assert data["cash"]==0
    assert data["positions"]==[{"symbol":"USDT","qty":102,"avg_price":None,"available_qty":90}]
    adapter.positions.return_value=[{"coin":"BTC","available":"1","frozen":"0"}]
    with pytest.raises(BrokerError):collect_account(adapter,"bitget")


def test_robinhood_requires_explicit_cash_proxy_and_matching_account():
    adapter=Mock(config={"account_number":"a"},prefix="/v2/")
    adapter.positions.return_value=[{"account_number":"a","asset_code":"BTC","total_quantity":"0.1","quantity_available_for_trading":"0.09"}]
    adapter._pages.return_value=iter([{"account_number":"a","buying_power":"100","buying_power_currency":"USD"}])
    with pytest.raises(ConfigurationError):collect_account(adapter,"robinhood")
    adapter.config["snapshot_cash_basis"]="buying_power"
    data=collect_account(adapter,"robinhood")
    assert data["cash"]==100 and data["cash_basis"]=="crypto_buying_power_explicit_proxy"
    assert data["positions"][0]["avg_price"] is None


def test_zhuorui_bridge_decimals_are_json_numbers_without_id_conversion():
    adapter=Mock(config={})
    adapter._bridge.return_value={"cash":"10.12","cash_by_currency":{"USD":"10.12"},"positions":[{"symbol":"AAPL","qty":"3","avg_price":"-2"}]}
    data=collect_account(adapter,"zhuorui")
    assert data["cash"]==10.12 and data["positions"][0]["avg_price"]==-2
    adapter._bridge.return_value=[{"order_id":"0000123","created_at":STAMP,"updated_at":STAMP,"symbol":"AAPL","side":"BUY","order_type":"LIMIT","status":"PARTIALLY_FILLED","quantity":"3","filled_quantity":"1","average_fill_price":"2.5","limit_price":"3"}]
    rows=collect_orders(adapter,"zhuorui",NOW,NOW)
    assert rows[0]["order_id"]=="0000123" and canonical(rows[0])["filled_quantity"]==1


def test_alpaca_order_cumulative_fills_and_notional():
    raw={"id":"001","submitted_at":STAMP,"updated_at":STAMP,"symbol":"AAPL","side":"buy","type":"limit","status":"canceled","qty":"3","filled_qty":"1","filled_avg_price":"10","limit_price":"11"}
    data=canonical(map_order("alpaca",raw,NOW))
    assert data["status"]=="CANCELED" and data["filled_quantity"]==1
    with pytest.raises(BrokerError):map_order("alpaca",{**raw,"filled_avg_price":None},NOW)
    data=canonical(map_order("alpaca",{**raw,"type":"market","qty":None,"notional":"100"},NOW))
    assert data["quantity"] is None and data["notional"]==100


def test_trade_station_single_leg_uses_top_level_average_not_last_execution():
    raw={"OrderID":"a","OpenedDateTime":STAMP,"OrderType":"Limit","Status":"FLP","FilledPrice":"10.4","LimitPrice":"11","Legs":[{"Symbol":"AAPL","BuyOrSell":"SellShort","QuantityOrdered":"3","ExecQuantity":"2","ExecutionPrice":"10.1"}]}
    data=canonical(map_order("tradestation",raw,NOW))
    assert data["average_fill_price"]==10.4 and data["status"]=="CANCELED" and data["side"]=="SELL"
    with pytest.raises(BrokerError):map_order("tradestation",{**raw,"Legs":raw["Legs"]*2},NOW)


def test_bitget_market_buy_quote_size_is_not_base_quantity():
    raw={"orderId":"123","cTime":"1792936800000","uTime":"1792936801000","symbol":"BTCUSDT","side":"buy","orderType":"market","status":"filled","size":"100","baseVolume":"0.001","priceAvg":"100000","quoteCoin":"USDT"}
    data=canonical(map_order("bitget",raw,NOW))
    assert data["quantity"] is None and data["notional"]==100 and data["filled_quantity"]==0.001
    assert "currency" not in data


def test_robinhood_order_maps_native_cumulative_fields():
    raw={"id":"id","created_at":STAMP,"updated_at":STAMP,"symbol":"BTC-USD","type":"limit","side":"buy","state":"partially_filled","limit_order_config":{"asset_quantity":"0.002","limit_price":"10000"},"filled_asset_quantity":"0.001","average_price":"9990"}
    data=canonical(map_order("robinhood",raw,NOW))
    assert data["filled_quantity"]==0.001 and data["average_fill_price"]==9990
