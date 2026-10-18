"""Run every offline test module (each in its own interpreter: the harness
installs module-level HA/paho stubs and a fake clock). Stdlib only.

Run (from anywhere):  python3 -I -B tests/offline/run_all.py
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    total = passed = 0
    bad = []
    for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
        name = os.path.basename(path)
        p = subprocess.run([sys.executable, "-I", "-B", path, *sys.argv[1:]],
                           capture_output=True, text=True)
        out = p.stdout + p.stderr
        for line in out.splitlines():
            if line.startswith("FAIL "):
                print(f"{name}: {line}")
        m = re.search(r"^(\d+)/(\d+) passed$", p.stdout, re.M)
        if m:
            passed += int(m.group(1))
            total += int(m.group(2))
            print(f"{name}: {m.group(0)}")
        else:
            print(f"{name}: did not complete\n{out[-2000:]}")
        if p.returncode or not m:
            bad.append(name)
    print(f"\nTOTAL {passed}/{total} passed" + (f"; failing: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
