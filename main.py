#!/usr/bin/env python3
from __future__ import annotations
import argparse, sys, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def _run(cmd: list[str]) -> int:
    print(">", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))

def morning() -> int:
    return _run([sys.executable, str(ROOT / "telegram_all_tips_ticket.py")])

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["morning"], default="morning")
    args = ap.parse_args()
    sys.exit(morning())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
