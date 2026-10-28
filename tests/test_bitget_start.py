import json
from unittest.mock import Mock

import pytest

from bitget import start
from common.errors import ConfigurationError


def config_file(tmp_path, *, account="BITGET_DENGLEI", numeric=2, kafka="45.32.121.19:9092"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "brokers": {"bitget": {"routing": {"account_id": account, "account_num_id": numeric}}},
        "kafka": {"bootstrap_servers": kafka},
    }))
    return path


DEPLOYMENT = {"string_id": "BITGET_DENGLEI", "numerical_id": "2", "kafka_ip": "45.32.121.19:9092"}


@pytest.mark.parametrize("kafka", ["45.32.121.19:9092", ["45.32.121.19:9092"]])
def test_matching_deployment(tmp_path, kafka):
    start.validate_deployment(config_file(tmp_path, kafka=kafka), DEPLOYMENT)


@pytest.mark.parametrize("updates", [
    {"account": "BITGET_LYJ"}, {"numeric": 1}, {"numeric": True},
    {"numeric": "2"}, {"kafka": "different-broker:9092"},
])
def test_copied_or_invalid_deployment_rejected(tmp_path, updates):
    with pytest.raises(ConfigurationError):
        start.validate_deployment(config_file(tmp_path, **updates), DEPLOYMENT)


@pytest.mark.parametrize("numeric", ["", "0", "-1", "2.0", "true"])
def test_invalid_property_numeric_id_rejected(tmp_path, numeric):
    with pytest.raises(ConfigurationError):
        start.validate_deployment(config_file(tmp_path), {**DEPLOYMENT, "numerical_id": numeric})


def test_credentials_only_files_remain_supported(tmp_path):
    start.validate_deployment(config_file(tmp_path), {})


def test_properties_mismatch_stops_before_any_broker_or_kafka_call(tmp_path, monkeypatch, capsys):
    properties = tmp_path / "config.properties"
    properties.write_text("client_id=test-key\nclient_secret=test-secret\npassphrase=test-passphrase\n"
                          "string_id=BITGET_DENGLEI\nnumerical_id=2\nkafka_ip=45.32.121.19:9092\n")
    for name in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    check = Mock()
    monkeypatch.setattr(start, "check", check)
    monkeypatch.setattr("sys.argv", ["start.py", "--check", "--credentials", str(properties),
                                    "--config", str(config_file(tmp_path, account="BITGET_LYJ", numeric=1))])
    assert start.main() == 2
    check.assert_not_called()
    output = capsys.readouterr().err
    assert "string_id" in output
    assert all(secret not in output for secret in ("test-key", "test-secret", "test-passphrase"))
