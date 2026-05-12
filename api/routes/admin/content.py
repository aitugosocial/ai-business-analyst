import logging
from datetime import date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.pg_connections import get_db, SessionLocal
from database.pg_models import Alert, Insight, User, UserNotification
from api.routes.dependencies import admin_required
from api.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/content", tags=["admin-content"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AlertCreate(BaseModel):
    title: str
    category: str
    priority: str
    score: int
    time_remaining: str
    why_act_now: str
    potential_reward: str
    action_required: str
    source: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None  # defaults to today


class AlertUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    score: Optional[int] = None
    time_remaining: Optional[str] = None
    why_act_now: Optional[str] = None
    potential_reward: Optional[str] = None
    action_required: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None
    is_active: Optional[bool] = None


class InsightCreate(BaseModel):
    title: str
    category: str
    read_time: Optional[str] = None  # accepts "5 min" string or numeric minutes
    source: Optional[str] = None
    url: Optional[str] = None
    what_changed: str
    why_it_matters: str
    action_to_take: str
    date: Optional[str] = None  # defaults to today


class InsightUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    read_time: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    what_changed: Optional[str] = None
    why_it_matters: Optional[str] = None
    action_to_take: Optional[str] = None
    date: Optional[str] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Alert endpoints
# ---------------------------------------------------------------------------

@router.get("/alerts")
def list_alerts(
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """List all alerts (paginated), including inactive ones."""
    alerts = db.query(Alert).order_by(Alert.created_at.desc()).offset(skip).limit(limit).all()
    total = db.query(Alert).count()
    result = []
    for a in alerts:
        result.append({
            "id": a.id,
            "title": a.title,
            "category": a.category,
            "priority": a.priority,
            "score": a.score,
            "date": a.date,
            "is_active": a.is_active,
            "total_views": a.total_views,
            "total_shares": a.total_shares,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return {"items": result, "total": total, "skip": skip, "limit": limit}


def _fan_out_alert_notifications(alert_id: int, alert_title: str, alert_message: str):
    """
    Background task: notify every active user about a new opportunity alert.
    Runs after the HTTP response is already sent so it never blocks the admin.
    Uses its own DB session to avoid interfering with the request session.
    """
    with SessionLocal() as bg_db:
        try:
            user_ids = [
                row[0]
                for row in bg_db.query(User.id).filter(User.is_active == True).all()
            ]
            short_msg = (alert_message[:97] + "…") if len(alert_message) > 100 else alert_message
            for uid in user_ids:
                NotificationService.create_notification(
                    db=bg_db,
                    user_id=uid,
                    type="new_alert",
                    title=f"🆕 {alert_title}",
                    message=short_msg,
                    link="/dashboard/opportunity-alerts",
                )
            logger.info(f"[alert fan-out] notified {len(user_ids)} users for alert id={alert_id}")
        except Exception as exc:
            logger.error(f"[alert fan-out] failed for alert id={alert_id}: {exc}")


@router.post("/alerts", status_code=201)
def create_alert(
    payload: AlertCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Manually create a new opportunity alert and fan-out notifications to all users."""
    alert_date = payload.date or date.today().isoformat()
    alert = Alert(
        title=payload.title,
        category=payload.category,
        priority=payload.priority,
        score=payload.score,
        time_remaining=payload.time_remaining,
        why_act_now=payload.why_act_now,
        potential_reward=payload.potential_reward,
        action_required=payload.action_required,
        source=payload.source,
        url=payload.url,
        date=alert_date,
        is_active=True,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    logger.info(f"Admin {current_user.email} created alert id={alert.id}")
    # Fan-out runs after response is sent — zero latency for the admin
    background_tasks.add_task(
        _fan_out_alert_notifications,
        alert.id,
        alert.title,
        alert.why_act_now or "",
    )
    return {"id": alert.id, "message": "Alert created successfully"}


@router.put("/alerts/{alert_id}")
def update_alert(
    alert_id: int,
    payload: AlertUpdate,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Update an existing alert."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    update_dict = payload.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(alert, key, value)

    db.commit()
    db.refresh(alert)
    logger.info(f"Admin {current_user.email} updated alert id={alert_id}")
    return {"id": alert.id, "message": "Alert updated successfully"}


@router.delete("/alerts/{alert_id}")
def delete_alert(
    alert_id: int,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Deactivate (soft-delete) an alert."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.is_active = False
    db.commit()
    logger.info(f"Admin {current_user.email} deactivated alert id={alert_id}")
    return {"message": "Alert deactivated successfully"}


# ---------------------------------------------------------------------------
# Insight endpoints
# ---------------------------------------------------------------------------

@router.get("/insights")
def list_insights(
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """List all insights (paginated), including inactive ones."""
    insights = db.query(Insight).order_by(Insight.created_at.desc()).offset(skip).limit(limit).all()
    total = db.query(Insight).count()
    result = []
    for ins in insights:
        result.append({
            "id": ins.id,
            "title": ins.title,
            "category": ins.category,
            "read_time": ins.read_time,
            "date": ins.date,
            "source": ins.source,
            "is_active": ins.is_active,
            "total_views": ins.total_views,
            "total_shares": ins.total_shares,
            "created_at": ins.created_at.isoformat() if ins.created_at else None,
        })
    return {"items": result, "total": total, "skip": skip, "limit": limit}


@router.post("/insights", status_code=201)
def create_insight(
    payload: InsightCreate,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Manually create a new AI insight."""
    insight_date = payload.date or date.today().isoformat()
    # Normalise read_time: accept int minutes or string like "5 min"
    raw_rt = payload.read_time
    if raw_rt is None:
        read_time_val = "5 min"
    elif str(raw_rt).isdigit():
        read_time_val = f"{raw_rt} min"
    else:
        read_time_val = str(raw_rt)
    insight = Insight(
        title=payload.title,
        category=payload.category,
        read_time=read_time_val,
        source=payload.source,
        url=payload.url,
        what_changed=payload.what_changed,
        why_it_matters=payload.why_it_matters,
        action_to_take=payload.action_to_take,
        date=insight_date,
        is_active=True,
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    logger.info(f"Admin {current_user.email} created insight id={insight.id}")
    return {"id": insight.id, "message": "Insight created successfully"}


@router.put("/insights/{insight_id}")
def update_insight(
    insight_id: int,
    payload: InsightUpdate,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Update an existing insight."""
    insight = db.query(Insight).filter(Insight.id == insight_id).first()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    update_dict = payload.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(insight, key, value)

    db.commit()
    db.refresh(insight)
    logger.info(f"Admin {current_user.email} updated insight id={insight_id}")
    return {"id": insight.id, "message": "Insight updated successfully"}


@router.delete("/insights/{insight_id}")
def delete_insight(
    insight_id: int,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db)
):
    """Deactivate (soft-delete) an insight."""
    insight = db.query(Insight).filter(Insight.id == insight_id).first()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    insight.is_active = False
    db.commit()
    logger.info(f"Admin {current_user.email} deactivated insight id={insight_id}")
    return {"message": "Insight deactivated successfully"}
