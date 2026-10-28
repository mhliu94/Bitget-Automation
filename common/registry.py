import importlib.util
from pathlib import Path
from .errors import ConfigurationError

BROKERS = ("bitget", "robinhood", "alpaca", "tradestation", "longbridge", "webull", "zhuorui")


def adapter_for(broker, config):
    if broker not in BROKERS:
        raise ConfigurationError("Unknown broker")
    # Broker directories intentionally are not Python packages: official SDKs
    # also use names longbridge/webull/zhuorui/alpaca.
    path = Path(__file__).resolve().parent.parent / broker / "adapter.py"
    spec = importlib.util.spec_from_file_location(f"broker_adapter_{broker}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Adapter(config)
