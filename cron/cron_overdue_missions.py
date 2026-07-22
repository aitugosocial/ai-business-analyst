#!/usr/bin/env python3
"""
Cron Job: Overdue Mission Notifications
Runs periodically. Scans activated missions for a due date that has passed
without the current step being completed, and sends a one-time notification
per overdue day.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(".env.local")
load_dotenv(".env.production")

from sqlalchemy.orm import Session
from database.pg_connections import get_db
from database.pg_models import BusinessAnalysis, UserNotification, NotificationHistory
from api.routes.user.missions import _ensure_dict, _flatten_roadmap_tasks
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger("cron.overdue_missions")


def _due_date_for_day(mission_config: dict, day_num: int):
    """Mirrors lib/mission-display.ts::dueDateForDay — end-of-day due date for
    a given mission day, from the cumulative day_plans durations starting at
    start_date."""
    start_date_str = mission_config.get('start_date')
    day_plans = mission_config.get('day_plans')
    if not start_date_str or not day_plans:
        return None
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    offset = 0
    for i in range(1, day_num + 1):
        plan = next((p for p in day_plans if p.get('day') == i), None)
        offset += (plan.get('durationDays') if plan else None) or 1
    due = start + timedelta(days=offset - 1)
    return due.replace(hour=23, minute=59, second=59, microsecond=999000)


def _current_overdue_day(analysis: BusinessAnalysis, mission_config: dict, completed_actions: list, now: datetime):
    """Mirrors lib/mission-display.ts::computeDisplayStep's day-picking loop.
    Returns the day number if that day is not done and its due date has
    passed, else None.
    """
    total_steps = len(_flatten_roadmap_tasks(analysis))
    if total_steps == 0:
        return None

    for day in range(1, total_steps + 1):
        due = _due_date_for_day(mission_config, day)
        step_id = f"{analysis.id}_action_{day}"
        is_done = step_id in completed_actions
        window_expired = due is not None and due < now

        if is_done and window_expired:
            continue
        if is_done and day == total_steps:
            return None
        if not is_done and window_expired:
            return day
        return None

    return None


def check_overdue_missions():
    db: Session = next(get_db())
    now = datetime.now(timezone.utc)
    notified = 0
    checked = 0

    try:
        analyses = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.user_progress.isnot(None)
        ).all()

        for analysis in analyses:
            user_progress = _ensure_dict(analysis.user_progress, "user_progress")
            mission_config = _ensure_dict(user_progress.get('mission_config'), "mission_config")
            if not mission_config.get('activated_at'):
                continue

            checked += 1
            completed_actions = user_progress.get('completed_actions', [])
            if not isinstance(completed_actions, list):
                completed_actions = []

            overdue_day = _current_overdue_day(analysis, mission_config, completed_actions, now)
            if overdue_day is None:
                continue

            history_key = f"mission_overdue_{analysis.id}_{overdue_day}"
            already_sent = db.query(NotificationHistory).filter(
                NotificationHistory.user_id == analysis.user_id,
                NotificationHistory.notification_type == history_key,
            ).first()
            if already_sent:
                continue

            mission_name = mission_config.get('mission_name') or (analysis.business_goal or "your mission")
            db.add(UserNotification(
                user_id=analysis.user_id,
                type="mission_overdue",
                title="Mission overdue",
                message=f"Day {overdue_day} of \"{mission_name[:60]}\" is overdue. Complete it to keep your progress going.",
                link=f"/dashboard/decision-engine/result/{analysis.id}",
                is_read=False,
            ))
            db.add(NotificationHistory(
                user_id=analysis.user_id,
                notification_type=history_key,
            ))
            db.commit()
            notified += 1
            logger.info(f"Notified user {analysis.user_id}: analysis {analysis.id} day {overdue_day} overdue")

        logger.info(f"Checked {checked} activated missions, sent {notified} overdue notifications")

    except Exception as exc:
        db.rollback()
        logger.error(f"Overdue mission check failed: {exc}", exc_info=True)
    finally:
        db.close()


if __name__ == "__main__":
    check_overdue_missions()
