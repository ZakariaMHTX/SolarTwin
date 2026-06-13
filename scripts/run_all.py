from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent

PHASES = [
    ("inspect", SCRIPT_DIR / "00_inspect.py"),
    ("ingest", SCRIPT_DIR / "01_ingest.py"),
    ("physics", SCRIPT_DIR / "02_physics.py"),
    ("twin", SCRIPT_DIR / "03_twin.py"),
    ("ledger", SCRIPT_DIR / "04_attribution_ledger.py"),
    ("report", SCRIPT_DIR / "06_report.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--through",
        choices=[name for name, _ in PHASES],
        default="report",
        help="Run phases up to and including this phase.",
    )
    args = parser.parse_args()

    for name, script in PHASES:
        if not script.exists():
            print(f"[skip] {name}: {script.name} not implemented yet")
        else:
            print(f"[run] {name}: {script}")
            subprocess.run([sys.executable, str(script)], check=True)
        if name == args.through:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
