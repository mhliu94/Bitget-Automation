from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from common.consumer import consume
from common.errors import BrokerError,ConfigurationError


def setup(monkeypatch,tmp_path,*,refresh_error=False,auth_error=False):
    import kafka
    calls=[];clock=[0.0]
    record=SimpleNamespace(topic="trading-commands",partition=0,offset=4,timestamp=1800000000000,value=b'{"id":"x","type":"MARKET_ORDER"}')
    consumer=Mock()
    consumer.topics.return_value={"trading-commands","account-details","order-status"}
    polls=[{}, {"partition":[record]}]
    def poll(**kwargs):
        calls.append("poll");clock[0]+=1
        return polls.pop(0)
    consumer.poll.side_effect=poll
    consumer.commit.side_effect=lambda *_:calls.append("commit")
    producer=Mock();publisher=Mock()
    monkeypatch.setattr(kafka,"KafkaConsumer",Mock(return_value=consumer))
    monkeypatch.setattr(kafka,"KafkaProducer",Mock(return_value=producer))
    monkeypatch.setattr("common.publication.SnapshotPublisher",Mock(return_value=publisher))
    class Reporter:
        def __init__(self,*_):pass
        def refresh(self,trigger,command_id=None):
            calls.append(trigger)
            if refresh_error and trigger=="post_action":raise BrokerError("publication unavailable")
            return {"errors":[{"error_type":"AuthenticationError"}] if auth_error else [],"trigger":trigger}
    monkeypatch.setattr("common.reporting.RuntimeSnapshots",Reporter)
    monkeypatch.setattr("common.consumer.time.monotonic",lambda:clock[0])
    def sleep(seconds):calls.append("delay");clock[0]+=seconds
    monkeypatch.setattr("common.consumer.time.sleep",sleep)
    runner=SimpleNamespace(broker="alpaca",execute=True,lock_path=None,journal_path=tmp_path/"alpaca.sqlite3",
        config={"routing":{"account_id":"ACC"},"journal_account":"paper"},
        process=Mock(side_effect=lambda *_args,**_kwargs:(calls.append("execute") or {"id":"x","action":"place_order","status":"submitted"})))
    config={"kafka":{"snapshot_interval_seconds":0.5,"post_action_delay_seconds":2}}
    return runner,config,consumer,producer,publisher,calls


def test_startup_idle_periodic_and_post_action_refresh(monkeypatch,tmp_path):
    runner,config,consumer,producer,publisher,calls=setup(monkeypatch,tmp_path)
    consume(runner,config,max_messages=1)
    assert calls==["startup","poll","periodic","poll","execute","commit","delay","post_action"]
    consumer.close.assert_called_once();publisher.close.assert_called_once();producer.close.assert_called_once()


def test_post_action_reporting_failure_does_not_replay_completed_order(monkeypatch,tmp_path):
    runner,config,consumer,_,_,calls=setup(monkeypatch,tmp_path,refresh_error=True)
    consume(runner,config,max_messages=1)
    assert calls.index("commit")<calls.index("post_action")
    runner.process.assert_called_once();consumer.commit.assert_called_once()


def test_startup_authentication_failure_stops_before_intake(monkeypatch,tmp_path):
    runner,config,consumer,_,_,calls=setup(monkeypatch,tmp_path,auth_error=True)
    consume(runner,config,max_messages=1)
    assert calls==["startup"]
    consumer.poll.assert_not_called();consumer.commit.assert_not_called();runner.process.assert_not_called()


def test_authentication_command_leaves_offset_uncommitted(monkeypatch,tmp_path):
    runner,config,consumer,_,_,calls=setup(monkeypatch,tmp_path)
    runner.process.side_effect=None
    runner.process.return_value={"id":"x","status":"authentication_required"}
    consume(runner,config,max_messages=1)
    consumer.commit.assert_not_called()


def test_output_topics_must_be_preprovisioned(monkeypatch,tmp_path):
    runner,config,consumer,_,_,calls=setup(monkeypatch,tmp_path)
    consumer.topics.return_value={"trading-commands"}
    with pytest.raises(ConfigurationError):consume(runner,config,max_messages=1)
    runner.process.assert_not_called();consumer.close.assert_called_once()
