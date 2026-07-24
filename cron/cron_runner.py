#!/usr/bin/env python3
"""
Cron runner — entry point for the Railway cron service container.

Schedule:
  insights          — every 4 hours
  cleanup           — once per day (every 24 hours)
  overdue missions  — every 2 hours
"""

import asyncio
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env.local")
load_dotenv(PROJECT_ROOT / ".env.production")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cron_runner")

INSIGHTS_INTERVAL_SECONDS    = 4 * 60 * 60   # 4 hours
CLEANUP_INTERVAL_SECONDS     = 24 * 60 * 60  # 24 hours
SUBSCRIPTION_INTERVAL_SECONDS = 12 * 60 * 60  # 12 hours
OVERDUE_MISSIONS_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours

# cron_insights.py lives in cron/, not scripts/
CRON_DIR = Path(__file__).parent


def _run_script(script_name: str) -> int:
    # Check cron/ directory first (cron_insights.py lives there), then scripts/
    cron_path = CRON_DIR / script_name
    scripts_path = PROJECT_ROOT / "scripts" / script_name
    script_path = cron_path if cron_path.exists() else scripts_path
    logger.info(f"▶ Running {script_name}")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode == 0:
        logger.info(f"✅ {script_name} completed successfully")
    else:
        logger.error(f"❌ {script_name} exited with code {result.returncode}")
    return result.returncode


def _run_script_at(script_path: Path) -> int:
    logger.info(f"▶ Running {script_path.name}")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode == 0:
        logger.info(f"✅ {script_path.name} completed successfully")
    else:
        logger.error(f"❌ {script_path.name} exited with code {result.returncode}")
    return result.returncode


async def run_insights_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_insights.py")
        except Exception as exc:
            logger.error(f"Insights job error: {exc}", exc_info=True)
        logger.info(f"Next insights run in {INSIGHTS_INTERVAL_SECONDS // 3600}h")
        await asyncio.sleep(INSIGHTS_INTERVAL_SECONDS)


async def run_cleanup_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_cleanup.py")
        except Exception as exc:
            logger.error(f"Cleanup job error: {exc}", exc_info=True)
        logger.info(f"Next cleanup run in {CLEANUP_INTERVAL_SECONDS // 3600}h")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def run_subscription_expiry_loop():
    expiry_script = CRON_DIR / "subscriptions" / "subscription_expiry.py"
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _run_script_at, expiry_script)
        except Exception as exc:
            logger.error(f"Subscription expiry job error: {exc}", exc_info=True)
        logger.info(f"Next subscription expiry run in {SUBSCRIPTION_INTERVAL_SECONDS // 3600}h")
        await asyncio.sleep(SUBSCRIPTION_INTERVAL_SECONDS)


async def run_overdue_missions_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_overdue_missions.py")
        except Exception as exc:
            logger.error(f"Overdue missions job error: {exc}", exc_info=True)
        logger.info(f"Next overdue missions run in {OVERDUE_MISSIONS_INTERVAL_SECONDS // 3600}h")
        await asyncio.sleep(OVERDUE_MISSIONS_INTERVAL_SECONDS)


async def main():
    logger.info("=" * 60)
    logger.info(f"Lavoo Cron Runner starting — {datetime.now().isoformat()}")
    logger.info(f"  Insights            : every {INSIGHTS_INTERVAL_SECONDS // 3600}h")
    logger.info(f"  Cleanup             : every {CLEANUP_INTERVAL_SECONDS // 3600}h")
    logger.info(f"  Subscription Expiry : every {SUBSCRIPTION_INTERVAL_SECONDS // 3600}h")
    logger.info(f"  Overdue Missions    : every {OVERDUE_MISSIONS_INTERVAL_SECONDS // 3600}h")
    logger.info("=" * 60)

    logger.info("Running initial pass of all jobs...")
    await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_insights.py")
    await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_cleanup.py")
    expiry_script = CRON_DIR / "subscriptions" / "subscription_expiry.py"
    await asyncio.get_event_loop().run_in_executor(None, _run_script_at, expiry_script)
    await asyncio.get_event_loop().run_in_executor(None, _run_script, "cron_overdue_missions.py")
    logger.info("Initial pass complete. Entering scheduled loop.")

    await asyncio.gather(
        run_insights_loop(),
        run_cleanup_loop(),
        run_subscription_expiry_loop(),
        run_overdue_missions_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
