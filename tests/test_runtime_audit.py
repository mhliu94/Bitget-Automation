"""Integration regressions found by independent runtime/contract review."""
import json
from types import SimpleNamespace

import pytest

from common.commands import normalize
from common.errors import CommandError
from common.publication import SnapshotPublisher


class AcknowledgedProducer:
    def __init__(self):
        self.values = []

    def send(self, topic, *, key, value):
        self.values.append(json.loads(value))
        return SimpleNamespace(get=lambda timeout: SimpleNamespace(partition=0, offset=1))


def test_account_publication_preserves_documented_negative_diluted_cost(tmp_path):
    # Longbridge: https://longbridge.com/nz/support/topics/48l80xd/fwyeg9
    # Diluted position cost can be negative after partial sales. This is a
    # position accounting basis, not a negative exchange execution price.
    producer = AcknowledgedProducer()
    publisher = SnapshotPublisher(tmp_path / "snapshot.db", {"account_id": "longbridge-test"}, producer)
    try:
        publisher.publish_account({"account_id": "longbridge-test", "cash": 120,
            "cash_by_currency": {"USD": 120}, "trading_enabled": False,
            "ts": "2026-10-25T12:00:00Z", "positions": [
                {"symbol": "AAPL", "qty": 10, "avg_price": -5.25, "cost_basis": "broker_account_cost_setting"}]})
        assert producer.values[0]["positions"][0]["avg_price"] == -5.25
    finally:
        publisher.close()


@pytest.mark.parametrize("field,value", [("direction", "buy"), ("shares", 10), ("limitPrice", 100),
                                         ("limit", 100), ("notional", 100), ("notionalUsd", 100),
                                         ("dollar_amount", 100), ("dollars", 100)])
def test_cancellation_cannot_silently_ignore_submission_field_aliases(field, value):
    with pytest.raises(CommandError):
        normalize({"id": "cancel-1", "type": "CANCEL_ORDER", "order_id": "broker-order", field: value},
                  "alpaca", {})


@pytest.mark.parametrize("field", ["ticker", "code", "orderReference"])
def test_account_enable_control_rejects_all_order_selector_aliases(field):
    with pytest.raises(CommandError):
        normalize({"id": "enable-1", "type": "SET_TRADING_ENABLED", "trading_enabled": True,
                   field: "AAPL"}, "alpaca", {})
