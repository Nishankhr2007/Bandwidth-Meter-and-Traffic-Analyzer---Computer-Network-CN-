#!/usr/bin/env python3
"""Apply the Python 3.10+ dict_items compatibility fix to installed MiniEdit."""
import argparse
from pathlib import Path
import shutil
import sys

OLD = "self.appPrefs = dict(self.appPrefs.items() + loadedTopology['application'].items())"
NEW = "self.appPrefs = dict(list(self.appPrefs.items()) + list(loadedTopology['application'].items()))"
CANDIDATES = (
    Path("/usr/lib/python3/dist-packages/mininet/examples/miniedit.py"),
    Path("/usr/share/mininet/examples/miniedit.py"),
    Path("/usr/local/lib/python3.12/dist-packages/mininet/examples/miniedit.py"),
)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", help="Explicit path to miniedit.py")
    parser.add_argument("--check", action="store_true", help="Check only; do not modify")
    args = parser.parse_args()

    candidates = [Path(args.path)] if args.path else list(CANDIDATES)
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        print("MiniEdit not found in standard locations.")
        print("Locate it with: find /usr -name miniedit.py 2>/dev/null")
        print("Then rerun with: sudo python3 tools/fix_miniedit.py --path /full/path/miniedit.py")
        return 2

    source = target.read_text()
    if NEW in source:
        print(f"Already Python-compatible: {target}")
        return 0
    if OLD not in source:
        print(f"No known Python 3 compatibility patch is needed for: {target}")
        print("No file was changed; continuing with this MiniEdit version.")
        return 0
    if args.check:
        print(f"Needs compatibility patch: {target}")
        return 1

    backup = target.with_suffix(target.suffix + ".bw_loop.bak")
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(source.replace(OLD, NEW))
    print(f"Patched: {target}")
    print(f"Backup:  {backup}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
