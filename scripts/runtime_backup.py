#!/usr/bin/env python3
"""Compatibility wrapper for the installed runtime-backup implementation."""

from mdv.runtime_backup import main


if __name__ == "__main__":
    raise SystemExit(main())
