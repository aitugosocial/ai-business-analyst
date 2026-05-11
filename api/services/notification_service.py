from sqlalchemy.orm import Session
from datetime import datetime, timezone
from database.pg_models import UserNotification, NotificationType
from api.routes.support.customer_service import notification_manager
import json
import logging

logger = logging.getLogger(__name__)

class NotificationService:
    @staticmethod
    def create_notification(
        db: Session,
        user_id: int,
        type: str,
        title: str,
        message: str,
        link: str = None
    ):
        """
        Create a new notification and notify user via WebSocket if connected
        """
        try:
            notification = UserNotification(
                user_id=user_id,
                type=type,
                title=title,
                message=message,
                link=link,
                created_at=datetime.now(timezone.utc)
            )
            db.add(notification)
            db.commit()
            db.refresh(notification)

            # Map type to icon/color if needed for frontend or just send payload
            payload = {
                "type": "new_notification",
                "payload": {
                    "id": notification.id,
                    "type": notification.type,
                    "title": notification.title,
                    "message": notification.message,
                    "link": notification.link,
                    "created_at": notification.created_at.isoformat(),
                    "is_read": notification.is_read
                }
            }

            # Import the notification manager from customer_service
            # Note: We use the already established WebSocket infrastructure
            import asyncio
            asyncio.create_task(notification_manager.send_personal_message(json.dumps(payload), user_id))

            return notification
        except Exception as e:
            logger.error(f"Error creating notification: {e}")
            db.rollback()
            return None
