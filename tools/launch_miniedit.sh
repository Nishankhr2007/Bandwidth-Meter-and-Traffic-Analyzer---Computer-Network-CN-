#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MINIEDIT="${1:-}"

if [[ -z "$MINIEDIT" ]]; then
  for candidate in \
    /usr/lib/python3/dist-packages/mininet/examples/miniedit.py \
    /usr/share/mininet/examples/miniedit.py \
    /usr/local/lib/python3.12/dist-packages/mininet/examples/miniedit.py; do
    if [[ -f "$candidate" ]]; then MINIEDIT="$candidate"; break; fi
  done
fi

if [[ -z "$MINIEDIT" || ! -f "$MINIEDIT" ]]; then
  echo "MiniEdit not found. Run: find /usr -name miniedit.py 2>/dev/null"
  echo "Then run: sudo bash tools/launch_miniedit.sh /full/path/miniedit.py"
  exit 1
fi

sudo python3 "$PROJECT_DIR/tools/fix_miniedit.py" --path "$MINIEDIT"
echo "Opening MiniEdit. Use File > Open and select:"
echo "$PROJECT_DIR/miniedit/bandwidth_meter_topology.mn"
exec sudo python3 "$MINIEDIT"
