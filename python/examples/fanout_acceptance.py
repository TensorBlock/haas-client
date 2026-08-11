#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

source_path = Path(__file__).resolve().parents[1] / "src"
if source_path.exists():
    sys.path.insert(0, str(source_path))

from haas_client.fanout_acceptance import main


if __name__ == "__main__":
    main()
