#!/usr/bin/env python3
"""Run from a checkout without installing Scout or any third-party Python packages."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "src/scout/login_client.py"),
        run_name="__main__",
    )
