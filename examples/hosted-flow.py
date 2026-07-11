#!/usr/bin/env python3
"""Source-checkout wrapper for the packaged offline hosted flow."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisppy.demo import main


if __name__ == "__main__":
    raise SystemExit(main())
