from unittest.mock import Mock
from common.errors import BrokerError
from common.reporting import RuntimeSnapshots


def runner():
    r=Mock(broker="alpaca",lock_path=None)
    r.config={"routing":{"account_id":"ACC","account_num_id":1}}
    r.trading_enabled.return_value=False
    return r


def test_account_error_does_not_stop_orders_and_pending_retry(monkeypatch):
    monkeypatch.setattr("common.reporting.collect_account",Mock(side_effect=BrokerError("account unavailable")))
    monkeypatch.setattr("common.reporting.collect_orders",Mock(side_effect=BrokerError("orders unavailable")))
    publisher=Mock()
    result=RuntimeSnapshots(runner(),publisher).refresh("startup")
    assert result["account_published"] is False and len(result["errors"])==2
    publisher.publish_orders.assert_called_once_with([])


def test_persistent_trading_state_overrides_upstream_snapshot_state(monkeypatch):
    monkeypatch.setattr("common.reporting.collect_account",lambda *_:{"cash":0,"positions":[],"cash_by_currency":{},"trading_enabled":True})
    monkeypatch.setattr("common.reporting.collect_orders",lambda *_,**kwargs:[])
    publisher=Mock()
    result=RuntimeSnapshots(runner(),publisher).refresh("post_action","x")
    payload=publisher.publish_account.call_args.args[0]
    assert payload["account_id"]=="ACC" and payload["account_num_id"]==1
    assert payload["trading_enabled"] is False and result["command_id"]=="x"


def test_account_binding_cannot_be_overwritten(monkeypatch):
    monkeypatch.setattr("common.reporting.collect_account",lambda *_:{"account_id":"wrong","cash":100,"positions":[]})
    monkeypatch.setattr("common.reporting.collect_orders",lambda *_,**kwargs:[])
    publisher=Mock()
    result=RuntimeSnapshots(runner(),publisher).refresh("startup")
    assert result["account_published"] is False
    publisher.publish_account.assert_not_called()
