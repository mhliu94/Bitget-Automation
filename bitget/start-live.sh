#!/usr/bin/env bash
# Run this manually to enable live trading and launch the Bitget worker.
set -euo pipefail
umask 077

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_dir="$HOME/bitget-configs"
python="$repo/.venv/bin/python"
config="$config_dir/config.json"
credentials="$config_dir/config.properties"
log="$config_dir/bitget.log"
pid_file="$config_dir/bitget.pid"
cd "$repo"

# Do not launch a second worker when the existing health record is still live.
"$python" - <<'PY'
from pathlib import Path
from common.health import inspect

status = Path('.state/bitget.status.json')
if status.exists():
    report = inspect(status)
    if report.get('process_alive_verified'):
        raise SystemExit(f"Bitget is already running (PID {report['pid']}).")
PY

printf 'Running read-only Bitget and Kafka startup checks...\n'
timeout 180s "$python" bitget/start.py \
  --credentials "$credentials" --config "$config" --check

# This change happens only when the owner runs this launcher, after checks pass.
"$python" - "$config" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
config = json.loads(path.read_text())
config['brokers']['bitget']['trading_enabled'] = True
fd, temporary = tempfile.mkstemp(prefix='.config-', suffix='.json', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as output:
        json.dump(config, output, indent=2)
        output.write('\n')
    os.replace(temporary, path)
finally:
    Path(temporary).unlink(missing_ok=True)
PY

nohup "$python" -u bitget/start.py \
  --credentials "$credentials" --config "$config" --execute \
  >> "$log" 2>&1 < /dev/null &
worker_pid=$!
sleep 3
if ! kill -0 "$worker_pid" 2>/dev/null; then
  printf 'Bitget exited during startup. See %s\n' "$log" >&2
  wait "$worker_pid" || exit "$?"
  exit 1
fi
printf '%s\n' "$worker_pid" > "$pid_file"
printf 'Bitget process started with PID %s. Log: %s\n' "$worker_pid" "$log"
printf 'Check health with: %s %s/tools/runner_control.py status --broker bitget\n' "$python" "$repo"
