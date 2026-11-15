#!/usr/bin/env python3
"""CLI entrypoint for managing repository author/contributor metadata via who-is-who.json."""

from pathlib import Path
import sys

# Ensure `python/` directory is importable regardless of current working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from who_is_who.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
