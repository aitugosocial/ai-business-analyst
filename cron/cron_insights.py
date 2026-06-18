#!/usr/bin/env python3
"""
Cron Job: Generate Insights
Runs every 4 hours (6 times per day)
Generates 3 insights per run
"""

import sys
import asyncio
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(".env.local")
load_dotenv(".env.production")

from decision_engine.content_generator import run_content_generation
from config.logging import setup_logging
import logging

# Setup logging
setup_logging(level=logging.INFO)
logger = logging.getLogger("cron.insights")

async def main():
    """Run insights generation cron job."""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info(f"🕐 CRON JOB START: Insights Generation - {start_time}")
    logger.info("=" * 60)

    try:
        # Generate 3 insights, 0 alerts
        await run_content_generation(insight_count=3, alert_count=0)

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.info("=" * 60)
        logger.info(f"✅ CRON JOB COMPLETE: Insights Generation")
        logger.info(f"   Duration: {duration:.2f} seconds")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"❌ CRON JOB FAILED: {str(e)}", exc_info=True)
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
