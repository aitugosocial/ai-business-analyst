from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Dict
from datetime import datetime
import math

from database.pg_connections import get_db
from database.pg_models import User, UserNotification, UserAlert, Alert
from api.routes.auth.login import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


def extract_user_id(current_user):
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                return user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                return user_data.id
            return user_data
        return current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
    return current_user.id


def _format_notification(n: UserNotification) -> dict:
    return {
        "id": f"notif_{n.id}",
        "internal_id": n.id,
        "source": "system",
        "type": n.type,
        "title": n.title,
        "message": n.message,
        "link": n.link,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat(),
    }


@router.get("")
async def get_notifications(
    page: int = 1,
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Paginated notifications for the current user.
    Returns { notifications, total, page, pages }.
    """
    user_id = extract_user_id(current_user)
    page = max(1, page)
    limit = max(1, min(limit, 50))
    skip = (page - 1) * limit

    base = db.query(UserNotification).filter(UserNotification.user_id == user_id)
    total = base.count()
    pages = max(1, math.ceil(total / limit))

    rows = (
        base.order_by(UserNotification.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return {
        "notifications": [_format_notification(n) for n in rows],
        "total": total,
        "page": page,
        "pages": pages,
    }


@router.post("/read-all")
async def mark_all_as_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = extract_user_id(current_user)
    db.query(UserNotification).filter(
        UserNotification.user_id == user_id,
        UserNotification.is_read == False,
    ).update({"is_read": True})
    db.commit()
    return {"status": "success"}


@router.post("/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = extract_user_id(current_user)
    if notification_id.startswith("notif_"):
        internal_id = int(notification_id.replace("notif_", ""))
        db.query(UserNotification).filter(
            UserNotification.id == internal_id,
            UserNotification.user_id == user_id,
        ).update({"is_read": True})
        db.commit()
    return {"status": "success"}
