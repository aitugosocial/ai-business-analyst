from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import logging

from database.pg_models import User, NotificationType
from api.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

LOGIN_STREAK_TARGET = 7
LOGIN_STREAK_CHOPS_REWARD = 20


def update_login_streak(db: Session, user: User) -> None:
    """
    Update the user's consecutive daily-login streak. Call this once per
    login (any auth provider) before db.commit().

    - Same calendar day as last counted login -> no change (avoid double-count).
    - Exactly one day after the last counted login -> streak += 1.
    - Any other gap (or first-ever login) -> streak resets to 1.
    - On reaching LOGIN_STREAK_TARGET, award LOGIN_STREAK_CHOPS_REWARD chops,
      reset the streak to 0 (the next login starts a fresh cycle at 1), and
      create a notification for the user.
    """
    today = datetime.now(timezone.utc).date()
    last_date = user.login_streak_date

    if last_date == today:
        return

    if last_date == today - timedelta(days=1):
        user.login_streak = (user.login_streak or 0) + 1
    else:
        user.login_streak = 1

    user.login_streak_date = today

    if user.login_streak >= LOGIN_STREAK_TARGET:
        user.total_chops = (user.total_chops or 0) + LOGIN_STREAK_CHOPS_REWARD
        user.login_streak = 0
        logger.info(f"[Streak] User {user.id} completed a {LOGIN_STREAK_TARGET}-day login streak, awarded {LOGIN_STREAK_CHOPS_REWARD} chops")
        NotificationService.create_notification(
            db=db,
            user_id=user.id,
            type=NotificationType.LOGIN_STREAK_REWARD.value,
            title="7-Day Streak Bonus!",
            message=f"You've earned {LOGIN_STREAK_CHOPS_REWARD} chops for logging in {LOGIN_STREAK_TARGET} days in a row. Keep it up!",
            link="/dashboard/decision-engine/progress",
        )
