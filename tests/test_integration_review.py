"""Independent regression checks for dispatch, replay, and calendar-day boundaries."""
from collections import namedtuple
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import Mock, patch

from common.commands import normalize, validate_age
from common.consumer import consume
from common.errors import CommandError, OutcomeUnknown
from common.journal import Journal
from common.runner import Runner
from common.utils import today_window


def command(**updates):
    return {"id": "review-order-1", "broker": "alpaca", "action": "place_order", "symbol": "AAPL",
            "side": "buy", "quantity": "1", "order_type": "limit", "limit_price": "100",
            "time_in_force": "day", **updates}


def fake_kafka():
    module = types.ModuleType("kafka")
    structs = types.ModuleType("kafka.structs")
    module.TopicPartition = namedtuple("TopicPartition", "topic partition")
    structs.OffsetAndMetadata = namedtuple("OffsetAndMetadata", "offset metadata leader_epoch")
    record = types.SimpleNamespace(topic="commands", partition=2, offset=40, timestamp=1234,
                                   value=b'{"id":"query-1","account_id":"account","action":"positions"}')
    instance = Mock()
    instance.__iter__ = Mock(side_effect=lambda: iter([record]))
    instance.poll = Mock(return_value={module.TopicPartition("commands",2):[record]})
    instance.topics = Mock(return_value={"commands","command-results"})
    module.KafkaConsumer = Mock(return_value=instance)
    module.KafkaProducer = Mock()
    return module, structs, instance


class IntegrationReviewTests(unittest.TestCase):
    def test_dry_run_creates_neither_journal_nor_network_calls(self):
        adapter = Mock()
        adapter.build_order.return_value = {"planned": True}
        with TemporaryDirectory() as tmp, patch("common.runner.adapter_for", return_value=adapter):
            path = Path(tmp) / "journal.sqlite"
            runner = Runner("alpaca", {}, execute=False, journal_path=path)
            result = runner.process(command())
            runner.close()
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(path.exists())
            adapter.place_order.assert_not_called()

    def test_replay_after_routing_metadata_change_does_not_resubmit(self):
        adapter = Mock()
        adapter.build_order.return_value = {"planned": True}
        adapter.place_order.return_value = {"id": "broker-id"}
        with TemporaryDirectory() as tmp, patch("common.runner.adapter_for", return_value=adapter):
            path = Path(tmp) / "journal.sqlite"
            config = {"trading_enabled": True, "journal_account": "alpaca-paper-stable-account",
                      "routing": {"account_id": "stable-account", "server_id": "worker-1"}}
            first = Runner("alpaca", config, execute=True, journal_path=path)
            self.assertEqual(first.process(command())["status"], "submitted")
            first.close()
            changed = {**config, "routing": {"account_id": "stable-account", "server_id": "worker-2"}}
            second = Runner("alpaca", changed, execute=True, journal_path=path)
            try:
                result = second.process(command())
                self.assertTrue(result.get("duplicate"))
                self.assertEqual(adapter.place_order.call_count, 1)
            finally:
                second.close()

    def test_stale_redelivery_returns_original_result_without_resubmitting(self):
        adapter = Mock()
        adapter.build_order.return_value = {"planned": True}
        adapter.place_order.return_value = {"id": "broker-id"}
        with TemporaryDirectory() as tmp, patch("common.runner.adapter_for", return_value=adapter):
            runner = Runner("alpaca", {"trading_enabled": True, "journal_account": "alpaca-paper-stable-account"},
                            True, Path(tmp) / "journal.sqlite")
            try:
                with patch("common.commands.time.time", return_value=1000):
                    first = runner.process(command(), kafka_timestamp_ms=1_000_000)
                with patch("common.commands.time.time", return_value=1500):
                    duplicate = runner.process(command(), kafka_timestamp_ms=1_000_000)
                self.assertEqual(first["data"], duplicate["data"])
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(adapter.place_order.call_count, 1)
            finally:
                runner.close()

    def test_pending_crash_and_conflicting_payload_cannot_replay(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite"
            journal = Journal(path)
            journal.reserve("account", command())
            journal.close()
            restored = Journal(path)
            try:
                with self.assertRaises(OutcomeUnknown):
                    restored.reserve("account", command())
                with self.assertRaises(CommandError):
                    restored.reserve("account", command(quantity="2"))
            finally:
                restored.close()

    def test_today_window_tracks_dst_length(self):
        start, end = today_window("America/New_York", datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(hours=25))
        self.assertEqual(start.hour, 4)
        start, end = today_window("America/New_York", datetime(2026, 3, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(hours=23))
        self.assertEqual(start.hour, 5)

    def test_luld_intent_respects_configured_non_equity_scope(self):
        with self.assertRaises(CommandError):
            normalize(command(broker="tradestation", symbol="ESH27", auction_intent="luld_reopening"),
                      "tradestation", {"asset_class": "futures"})

    def test_malformed_expiration_is_a_rejected_command_not_a_consumer_crash(self):
        with self.assertRaises(CommandError):
            validate_age(command(expires_at="not-a-timestamp"))

    def test_server_targeted_consumers_have_distinct_groups(self):
        kafka, structs, consumer = fake_kafka()
        with patch.dict("sys.modules", {"kafka": kafka, "kafka.structs": structs}), redirect_stdout(io.StringIO()):
            for server in ["worker-1", "worker-2"]:
                runner = Mock(broker="alpaca", execute=False)
                runner.config = {"routing": {"account_id": "account", "server_id": server}}
                runner.process.return_value = {"status": "ignored"}
                consume(runner, {"kafka": {}}, max_messages=1)
        groups = [call.kwargs["group_id"] for call in kafka.KafkaConsumer.call_args_list]
        self.assertEqual(len(set(groups)), 2)

    def test_five_numeric_accounts_receive_independent_live_and_preview_groups(self):
        kafka, structs, consumer = fake_kafka()
        with patch.dict("sys.modules", {"kafka": kafka, "kafka.structs": structs}), redirect_stdout(io.StringIO()):
            for execute in (False, True):
                for numeric in range(1, 6):
                    runner = Mock(broker="bitget", execute=execute)
                    # A repeated account label must not merge numeric account groups.
                    runner.config = {"routing": {"account_id": "BITGET", "account_num_id": numeric}}
                    runner.process.return_value = {"status": "ignored"}
                    consume(runner, {"kafka": {"publish_snapshots": False}}, max_messages=1)
        groups = [call.kwargs["group_id"] for call in kafka.KafkaConsumer.call_args_list]
        self.assertEqual(len(set(groups)), 10)
        self.assertEqual({group.rsplit(".", 1)[1] for group in groups}, {"1", "2", "3", "4", "5"})

    def test_accounts_without_numeric_id_keep_existing_group(self):
        kafka, structs, consumer = fake_kafka()
        runner = Mock(broker="bitget", execute=False)
        runner.config = {"routing": {"account_id": "account"}}
        runner.process.return_value = {"status": "ignored"}
        with patch.dict("sys.modules", {"kafka": kafka, "kafka.structs": structs}), redirect_stdout(io.StringIO()):
            consume(runner, {"kafka": {}}, max_messages=1)
        self.assertEqual(kafka.KafkaConsumer.call_args.kwargs["group_id"],
                         "broker-api-scripts.bitget.account.default.preview")

    def test_consumer_commits_only_the_processed_record(self):
        kafka, structs, consumer = fake_kafka()
        runner = Mock(broker="alpaca", execute=False)
        runner.config = {"routing": {"account_id": "account"}}
        runner.process.return_value = {"status": "ignored"}
        with patch.dict("sys.modules", {"kafka": kafka, "kafka.structs": structs}), redirect_stdout(io.StringIO()):
            consume(runner, {"kafka": {}}, max_messages=1)
        offsets = consumer.commit.call_args.args[0]
        self.assertEqual(list(offsets), [kafka.TopicPartition("commands", 2)])
        self.assertEqual(next(iter(offsets.values())).offset, 41)

    def test_failed_result_publication_keeps_input_offset_uncommitted(self):
        kafka, structs, consumer = fake_kafka()
        producer = kafka.KafkaProducer.return_value
        producer.send.return_value.get.side_effect = TimeoutError("simulated publication failure")
        runner = Mock(broker="alpaca", execute=True)
        runner.config = {"routing": {"account_id": "account"}}
        runner.process.return_value = {"status": "submitted", "data": {"id": "broker-id"}}
        with patch.dict("sys.modules", {"kafka": kafka, "kafka.structs": structs}), redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                consume(runner, {"kafka": {"result_topic": "command-results", "publish_snapshots":False}}, max_messages=1)
        consumer.commit.assert_not_called()
        consumer.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
