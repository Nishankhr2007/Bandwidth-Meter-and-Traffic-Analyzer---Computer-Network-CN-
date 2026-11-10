#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/pox" >&2
  exit 1
fi
POX_DIR="$1"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cp "$PROJECT_DIR/controller/bandwidth_controller.py" "$POX_DIR/ext/bandwidth_controller.py"
cd "$POX_DIR"
exec sudo python3 pox.py bandwidth_controller openflow.of_01 --port=6633
