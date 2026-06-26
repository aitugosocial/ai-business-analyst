"""
Subscription Expiry Cron Job
Runs periodically to expire old subscriptions and sync user statuses.
Prevents manual checks on every request.

Schedule: Run daily at 2 AM UTC
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# File lives at cron/subscriptions/ — go up 3 levels to reach project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv('.env.local')

from sqlalchemy.orm import Session
from database.pg_connections import get_db
from database.pg_models import User, Subscriptions
from api.utils.subscription_sync import sync_user_subscription
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def expire_old_subscriptions():
    """
    Check all subscriptions and expire those past their end_date.
    Also sync user subscription statuses.
    """
    db: Session = next(get_db())

    try:
        now = datetime.now(timezone.utc)
        logger.info(f"Starting subscription expiry check at {now}")

        # Find all active subscriptions with past end_date
        expired_subs = db.query(Subscriptions).filter(
            Subscriptions.status == 'active',
            Subscriptions.end_date < now
        ).all()

        expired_count = len(expired_subs)
        logger.info(f"Found {expired_count} expired subscriptions")

        # Mark them as expired
        for sub in expired_subs:
            sub.status = 'expired'
            logger.info(f"Marked subscription {sub.id} as expired (user {sub.user_id})")

        db.commit()

        # Now sync all affected users
        if expired_count > 0:
            affected_user_ids = {sub.user_id for sub in expired_subs}
            logger.info(f"Syncing {len(affected_user_ids)} affected users")

            for user_id in affected_user_ids:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    sync_user_subscription(user, db)
                    logger.info(f"Synced user {user_id} subscription status")

        logger.info(f"Subscription expiry check complete: {expired_count} expired")
        return expired_count

    except Exception as e:
        logger.error(f"Error in subscription expiry cron: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    start_time = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info(f"🕐 CRON JOB START: Subscription Expiry - {start_time}")
    logger.info("=" * 60)

    try:
        expired = expire_old_subscriptions()
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info("=" * 60)
        logger.info(f"✅ CRON JOB COMPLETE: Subscription Expiry")
        logger.info(f"   Duration: {duration:.2f} seconds")
        logger.info(f"   Subscriptions expired: {expired}")
        logger.info(f"   Users synced: {expired}")
        logger.info("=" * 60)
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ CRON JOB FAILED: {e}")
        sys.exit(1)
