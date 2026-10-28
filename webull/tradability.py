#!/usr/bin/env python3
"""webull tradability entry point."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.cli import main

if __name__ == "__main__":
    raise SystemExit(main('webull', 'tradability'))
