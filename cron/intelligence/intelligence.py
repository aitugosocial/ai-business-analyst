#!/usr/bin/env python3
"""
Cron Job: Generate Lavoo Intelligence Feed (Insights + Opportunities)
Runs the unified LAVOO INTELLIGENCE prompt once per run and saves both
insights and opportunities from the single response.

This replaces the separate cron/insights/insights.py and cron/alerts/alerts.py
jobs - point the scheduler at this entrypoint instead of those two.
"""

import sys
import asyncio
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(".env.local")
load_dotenv(".env.production")

from cron.intelligence.generator import run_content_generation
from config.logging import setup_logging
import logging

# Setup logging
setup_logging(level=logging.INFO)
logger = logging.getLogger("cron.intelligence")


async def main():
    """Run Lavoo Intelligence generation cron job."""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info(f"🕐 CRON JOB START: Lavoo Intelligence Generation - {start_time}")
    logger.info("=" * 60)

    try:
        await run_content_generation()

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.info("=" * 60)
        logger.info("✅ CRON JOB COMPLETE: Lavoo Intelligence Generation")
        logger.info(f"   Duration: {duration:.2f} seconds")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"❌ CRON JOB FAILED: {str(e)}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
