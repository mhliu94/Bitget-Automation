#!/usr/bin/env python3
"""Read Kafka metadata and bounded samples without joining a group or committing."""
import argparse
import json
from kafka import KafkaConsumer, TopicPartition


def inspect(bootstrap, topic, count=20):
    consumer = KafkaConsumer(bootstrap_servers=bootstrap, group_id=None,
                             enable_auto_commit=False, request_timeout_ms=30000, session_timeout_ms=10000,
                             bootstrap_timeout_ms=10000,
                             consumer_timeout_ms=5000, allow_auto_create_topics=False)
    try:
        topics = sorted(consumer.topics())
        print(json.dumps({"topics": topics}))
        if topic not in topics:
            raise SystemExit(f"Topic {topic!r} does not exist")
        partitions = [TopicPartition(topic, p) for p in sorted(consumer.partitions_for_topic(topic))]
        consumer.assign(partitions)
        begins = consumer.beginning_offsets(partitions)
        ends = consumer.end_offsets(partitions)
        for tp in partitions:
            print(json.dumps({"topic": topic, "partition": tp.partition,
                              "begin": begins[tp], "end": ends[tp]}))
            consumer.seek(tp, max(begins[tp], ends[tp] - count))
        remaining = sum(min(count, ends[tp] - begins[tp]) for tp in partitions)
        while remaining:
            batches = consumer.poll(timeout_ms=5000, max_records=remaining)
            if not batches:
                break
            for tp, records in batches.items():
                for record in records:
                    if record.offset >= ends[tp]:
                        continue
                    remaining -= 1
                    raw = record.value.decode("utf-8", errors="replace") if record.value else None
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError):
                        value = raw
                    print(json.dumps({"partition": tp.partition, "offset": record.offset,
                                      "timestamp": record.timestamp, "value": value}, default=str))
            if all(consumer.position(tp) >= ends[tp] for tp in partitions):
                break
    finally:
        consumer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", default="45.32.121.19:9092")
    parser.add_argument("--topic", default="trading-commands")
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("count must be between 1 and 100")
    inspect(args.bootstrap, args.topic, args.count)
