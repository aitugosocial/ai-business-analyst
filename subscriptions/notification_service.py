from datetime import datetime, timezone
from sqlalchemy.orm import Session
from database.pg_models import User, UserNotification, NotificationHistory


class NotificationService:

    @staticmethod
    def create_notification(
        db: Session,
        user_id: int,
        title: str,
        message: str,
        link: str = None,
        # Accept BOTH spellings — stripe.py uses type=, other callers use notification_type=
        notification_type: str = None,
        type: str = None,
    ):
        """
        Create an in-app notification.

        Accepts `type` as an alias for `notification_type` so that all callers
        in stripe.py (which use type=) work without modification.

        Notifications are created with is_read=False and persist until the user
        explicitly reads them. The counter only disappears when the user acts.
        """
        resolved_type = notification_type or type or "general"

        notification = UserNotification(
            user_id=user_id,
            type=resolved_type,
            title=title,
            message=message,
            link=link,
            is_read=False,          # Must stay False until user reads it
            created_at=datetime.now(timezone.utc)
        )
        db.add(notification)

        # Track that the notification was sent (for rate-limiting reminders)
        history = NotificationHistory(
            user_id=user_id,
            notification_type=resolved_type,
            sent_at=datetime.now(timezone.utc)
        )
        db.add(history)

        db.commit()

        # Broadcast via WebSocket (Fire-and-forget)
        try:
            # Local import to avoid circular dependency
            from api.routes.support.customer_service import notification_manager
            import json
            import asyncio

            payload = {
                "type": "new_notification",
                "payload": {
                    "id": notification.id,
                    "type": resolved_type,
                    "title": title,
                    "message": message,
                    "link": link,
                    "is_read": False,
                    "created_at": notification.created_at.isoformat()
                }
            }
            
            # Schedule the broadcast if an event loop is running (FastAPI/Uvicorn context)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(notification_manager.send_personal_message(
                        json.dumps(payload), user_id
                    ))
            except RuntimeError:
                # No event loop in this thread (e.g. running from a script or background task)
                pass
        except Exception:
            # Never let notification broadcast failure crash the main transaction
            pass

        return notification

    # -------------------------------------------------------------------------
    # Read state management
    # -------------------------------------------------------------------------

    @staticmethod
    def mark_as_read(db: Session, notification_id: int, user_id: int) -> bool:
        """
        Mark a single notification as read.
        Returns True if found and updated, False if not found.
        Call this when the user clicks or views the notification.
        """
        notification = db.query(UserNotification).filter(
            UserNotification.id == notification_id,
            UserNotification.user_id == user_id
        ).first()

        if not notification:
            return False

        notification.is_read = True
        notification.read_at = datetime.now(timezone.utc)
        db.commit()
        return True

    @staticmethod
    def mark_all_as_read(db: Session, user_id: int) -> int:
        """
        Mark all unread notifications as read for a user.
        Returns the count of notifications updated.
        Call this when the user opens the notification panel.
        """
        unread = db.query(UserNotification).filter(
            UserNotification.user_id == user_id,
            UserNotification.is_read == False
        ).all()

        now = datetime.now(timezone.utc)
        for n in unread:
            n.is_read = True
            n.read_at = now

        db.commit()
        return len(unread)

    @staticmethod
    def get_unread(db: Session, user_id: int) -> list:
        """
        Return all unread notifications for a user, newest first.
        Use this to populate the notification bell counter.
        """
        return (
            db.query(UserNotification)
            .filter(
                UserNotification.user_id == user_id,
                UserNotification.is_read == False
            )
            .order_by(UserNotification.created_at.desc())
            .all()
        )

    # -------------------------------------------------------------------------
    # Domain-specific helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def send_beta_card_reminder(db: Session, user: User):
        """Send reminder to save card during beta."""
        from api.services.beta_service import BetaService

        if not BetaService.should_send_reminder(user, "beta_card_reminder", db):
            return None

        return NotificationService.create_notification(
            db=db,
            user_id=user.id,
            notification_type="beta_card_reminder",
            title="💳 Save Your Card for Launch Access",
            message="Secure your access today! Save your card now to ensure uninterrupted access on launch day.",
            link="/dashboard/upgrade"
        )

    @staticmethod
    def send_grace_period_warning(db: Session, user: User):
        """Send warning during grace period."""
        from api.services.beta_service import BetaService

        if not BetaService.should_send_reminder(user, "grace_period_warning", db):
            return None

        if user.grace_period_ends_at:
            days_remaining = (user.grace_period_ends_at - datetime.now(timezone.utc)).days
        else:
            days_remaining = BetaService.get_grace_period_days()

        return NotificationService.create_notification(
            db=db,
            user_id=user.id,
            notification_type="grace_period_warning",
            title="⏰ Action Required - Add Payment Method",
            message=f"You have {days_remaining} days left to add a payment method. After this, your access will be paused.",
            link="/dashboard/upgrade"
        )

    @staticmethod
    def send_card_saved_success(db: Session, user: User):
        """Notify user that card was saved successfully."""
        from .beta_service import BetaService

        if BetaService.is_beta_mode():
            message = "Congratulations on becoming a part of the Lavoo Community! Your card is securely saved for launch."
        else:
            message = "Congratulations on becoming a part of the Lavoo Community! Your subscription is now active."

        return NotificationService.create_notification(
            db=db,
            user_id=user.id,
            notification_type="card_saved",
            title="✅ Card Saved Successfully",
            message=message,
            link="/dashboard"
        )