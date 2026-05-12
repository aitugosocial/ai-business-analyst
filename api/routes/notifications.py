from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from typing import List, Dict
from datetime import datetime

from database.pg_connections import get_db
from database.pg_models import User, UserNotification, UserAlert, Alert
from api.routes.auth.login import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])

def extract_user_id(current_user):
    """Helper function to extract user_id from current_user"""
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                return user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                return user_data.id
            else:
                return user_data
        else:
            return current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
    else:
        return current_user.id

@router.get("")
async def get_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get all unread notifications and alerts for the user
    """
    user_id = extract_user_id(current_user)
    
    # 1. Fetch UserNotifications (payments, commissions, etc.)
    # We'll fetch all but prioritize unread
    notifications = db.query(UserNotification).filter(
        UserNotification.user_id == user_id
    ).order_by(UserNotification.is_read.asc(), UserNotification.created_at.desc()).limit(20).all()
    
    # 2. Fetch UserAlerts that haven't been attended — joinedload eliminates N+1
    user_alerts = (
        db.query(UserAlert)
        .filter(UserAlert.user_id == user_id, UserAlert.is_attended == False)
        .join(Alert)
        .options(joinedload(UserAlert.alert))  # pre-load Alert in the same query
        .order_by(Alert.created_at.desc())
        .all()
    )
    
    result = []
    
    # Combine them into a unified format
    for n in notifications:
        result.append({
            "id": f"notif_{n.id}",
            "internal_id": n.id,
            "source": "system",
            "type": n.type,
            "title": n.title,
            "message": n.message,
            "link": n.link,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat()
        })
        
    for ua in user_alerts:
        result.append({
            "id": f"alert_{ua.id}",
            "internal_id": ua.id,
            "source": "alert",
            "type": "system_alert",
            "title": f"New Alert: {ua.alert.title}",
            "message": ua.alert.why_act_now[:100] + "..." if len(ua.alert.why_act_now) > 100 else ua.alert.why_act_now,
            "link": f"/dashboard/alerts/detail?id={ua.alert_id}",
            "is_read": ua.is_attended, # In this context, is_attended means "read" for the count
            "created_at": ua.created_at.isoformat()
        })
        
    # Sort unified result by created_at desc
    result.sort(key=lambda x: x["created_at"], reverse=True)
    
    return {"notifications": result}

@router.post("/read-all")
async def mark_all_as_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mark all notifications and alerts as attended/read to clear the count
    """
    user_id = extract_user_id(current_user)
    
    # Mark UserNotifications as read
    db.query(UserNotification).filter(
        UserNotification.user_id == user_id,
        UserNotification.is_read == False
    ).update({"is_read": True})
    
    # Mark UserAlerts as attended
    db.query(UserAlert).filter(
        UserAlert.user_id == user_id,
        UserAlert.is_attended == False
    ).update({"is_attended": True})
    
    db.commit()
    return {"status": "success"}

@router.post("/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mark a specific notification or alert as read
    """
    user_id = extract_user_id(current_user)
    
    if notification_id.startswith("notif_"):
        internal_id = int(notification_id.replace("notif_", ""))
        db.query(UserNotification).filter(
            UserNotification.id == internal_id,
            UserNotification.user_id == user_id
        ).update({"is_read": True})
    elif notification_id.startswith("alert_"):
        internal_id = int(notification_id.replace("alert_", ""))
        db.query(UserAlert).filter(
            UserAlert.id == internal_id,
            UserAlert.user_id == user_id
        ).update({"is_attended": True})
        
    db.commit()
    return {"status": "success"}
