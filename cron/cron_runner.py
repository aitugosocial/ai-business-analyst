#!/usr/bin/env python3
"""
Entry point for the Railway cron service container.
Startup command: python cron/cron_runner.py
Delegates to scripts/cron_runner.py which contains the full scheduler.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Re-use the canonical runner that already lives in scripts/
exec(open(PROJECT_ROOT / "scripts" / "cron_runner.py").read())
