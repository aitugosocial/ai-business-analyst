from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime

from database.pg_connections import get_db
from database.pg_models import (User, Alert, Referral, UserAlert, UserResponse, UserCreate, AlertResponse, AlertCreate,
                            ViewAlertRequest, ShareAlertRequest, ChopsBreakdown, PinAlertRequest, UserPinnedAlert)
from api.routes.auth.login import get_current_user, get_admin_user
from api.cache import get_cached, set_cached, delete_cached, CacheTTL
from api.utils.subscription_sync import sync_user_subscription

from typing import Optional, List

from sqlalchemy import or_, func

router = APIRouter(tags=["alerts"])

def is_pro_user(subscription_status: str) -> bool:
    return subscription_status == "active"


# API Endpoints
@router.get("/users/me")
def get_current_user_route(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    user = current_user  # extract actual user object

    # Sync subscription status (non-blocking, called lazily on dashboard load)
    sync_user_subscription(user, db)

    result = {
        "id": user.id,
        "email": user.email,
        "subscription_status": user.subscription_status,
        "total_chops": user.total_chops,
        "alert_reading_chops": user.alert_reading_chops,
        "alert_sharing_chops": user.alert_sharing_chops,
        "insight_reading_chops": user.insight_reading_chops,
        "insight_sharing_chops": user.insight_sharing_chops,
        "referral_chops": user.referral_chops,
        "referral_count": user.referral_count
    }
    
    print(f"[DEBUG] /users/me for user_id={user.id}: referral_count={result['referral_count']}, total_chops={result['total_chops']}")
    
    return result


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Get user details including chops breakdown"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/users/email/{email}", response_model=UserResponse)
def get_user_by_email(email: str, db: Session = Depends(get_db)):
    """Get user details by email"""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/users/{user_id}/chops", response_model=ChopsBreakdown)
def get_user_chops(user_id: int, db: Session = Depends(get_db)):
    """Get detailed chops breakdown for a user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return ChopsBreakdown(
        total_chops=user.total_chops,
        alert_reading_chops=user.alert_reading_chops,
        alert_sharing_chops=user.alert_sharing_chops,
        insight_reading_chops=user.insight_reading_chops,
        insight_sharing_chops=user.insight_sharing_chops,
        referral_chops=user.referral_chops,
        referral_count=user.referral_count
    )


@router.post("/alerts", response_model=AlertResponse, status_code=status.HTTP_201_CREATED)
def create_alert(alert: AlertCreate, db: Session = Depends(get_db)):
    """Create a new alert"""
    db_alert = Alert(**alert.dict())
    db.add(db_alert)
    db.commit()
    db.refresh(db_alert)
    return db_alert


@router.get("/alerts", response_model=List[AlertResponse])
async def get_alerts(
    user_id: Optional[int] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all alerts with optional filtering and user interaction data"""
    # Use authenticated user's ID if not provided
    if not user_id:
        user_id = current_user.id

    # Try to get from cache first (2 minute TTL)
    cache_key = f"alerts:list:{user_id}:{category or 'all'}:{priority or 'all'}:{skip}:{limit}"
    cached = await get_cached(cache_key)
    if cached:
        return cached

    query = db.query(Alert).filter(Alert.is_active == True)

    if category:
        query = query.filter(Alert.category == category)
    if priority:
        query = query.filter(Alert.priority == priority)

    alerts = query.order_by(Alert.created_at.desc()).offset(skip).limit(limit).all()

    alert_ids = [a.id for a in alerts]

    # Single bulk query for pinned status — replaces per-alert lookup
    pinned_alert_ids = {
        pid[0]
        for pid in db.query(UserPinnedAlert.alert_id)
        .filter(UserPinnedAlert.user_id == user_id, UserPinnedAlert.alert_id.in_(alert_ids))
        .all()
    }

    # Single bulk query for user-alert interactions — eliminates N+1
    user_alerts_map = {
        ua.alert_id: ua
        for ua in db.query(UserAlert).filter(
            UserAlert.user_id == user_id,
            UserAlert.alert_id.in_(alert_ids),
        ).all()
    }

    result = []
    for alert in alerts:
        user_alert = user_alerts_map.get(alert.id)

        is_attended = False
        has_viewed = False
        has_shared = False

        if user_alert:
            has_viewed = user_alert.has_viewed
            has_shared = user_alert.has_shared
            is_attended = user_alert.is_attended or has_viewed or has_shared

        alert_dict = {
            "id": alert.id,
            "title": alert.title,
            "category": alert.category,
            "priority": alert.priority,
            "score": alert.score,
            "time_remaining": alert.time_remaining,
            "why_act_now": alert.why_act_now,
            "potential_reward": alert.potential_reward,
            "action_required": alert.action_required,
            "source": alert.source,
            "url": alert.url or "",
            "date": alert.date,
            "posted_date": alert.created_at.strftime("%Y-%m-%d") if alert.created_at else alert.date,
            "total_views": alert.total_views,
            "total_shares": alert.total_shares,
            "has_viewed": has_viewed,
            "has_shared": has_shared,
            "is_attended": is_attended,
            "is_pinned": alert.id in pinned_alert_ids
        }
        result.append(AlertResponse(**alert_dict))

    # Sort: pinned alerts first, then by created_at
    result.sort(key=lambda x: (not x.is_pinned, alerts[[a.id for a in alerts].index(x.id)].created_at), reverse=True)

    # Cache the result for 2 minutes
    result_data = [alert.model_dump() for alert in result]
    await set_cached(cache_key, result_data, CacheTTL.MEDIUM)

    return result


@router.get("/alerts/unread-count")
async def get_unread_alerts_count(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Returns the count of active alerts the current user has not yet viewed,
    scoped to alerts published on or after their account creation date.
    A user only 'owns' alerts that existed when they joined or arrived later.
    Polled every 2 minutes; also triggered after each page is viewed.
    """
    user_id = current_user.id
    viewed_ids = (
        db.query(UserAlert.alert_id)
        .filter(UserAlert.user_id == user_id, UserAlert.has_viewed == True)
    )
    count = (
        db.query(func.count(Alert.id))
        .filter(
            Alert.is_active == True,
            Alert.created_at >= current_user.created_at,
            ~Alert.id.in_(viewed_ids),
        )
        .scalar()
    ) or 0
    return {"count": count}


@router.get("/alerts/paginated")
async def get_alerts_paginated(
    page: int = 1,
    limit: int = 7,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    today_only: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Paginated alerts endpoint.
    Returns { data, total, page, pages } so the frontend can drive
    pagination from the backend instead of fetching all records.
    When today_only=true only alerts created since midnight UTC today
    are returned — used by the home-feed dashboard preview.
    """
    user_id = current_user.id
    page = max(1, page)
    limit = max(1, min(limit, 50))
    skip = (page - 1) * limit

    base_query = db.query(Alert).filter(
        Alert.is_active == True,
        # Only surface alerts that existed on or after the user's join date.
        # For users registered before all alerts this is all alerts.
        # For new users this scopes to alerts published after they signed up,
        # keeping the list and the unread-count consistent with each other.
        Alert.created_at >= current_user.created_at,
    )
    if category:
        base_query = base_query.filter(Alert.category == category)
    if priority:
        base_query = base_query.filter(Alert.priority == priority)
    if today_only:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        base_query = base_query.filter(Alert.created_at >= today_start)

    total = base_query.count()
    pages = max(1, -(-total // limit))  # ceiling division

    alerts = base_query.order_by(Alert.created_at.desc()).offset(skip).limit(limit).all()

    alert_ids_page = [a.id for a in alerts]

    # Single bulk query for pinned status
    pinned_ids = {
        pid[0]
        for pid in db.query(UserPinnedAlert.alert_id)
        .filter(UserPinnedAlert.user_id == user_id, UserPinnedAlert.alert_id.in_(alert_ids_page))
        .all()
    }

    # Single bulk query for user interactions — eliminates N+1
    ua_map = {
        ua.alert_id: ua
        for ua in db.query(UserAlert).filter(
            UserAlert.user_id == user_id,
            UserAlert.alert_id.in_(alert_ids_page),
        ).all()
    }

    result = []
    for alert in alerts:
        ua = ua_map.get(alert.id)
        has_viewed = ua.has_viewed if ua else False
        has_shared = ua.has_shared if ua else False
        result.append(AlertResponse(**{
            "id": alert.id,
            "title": alert.title,
            "category": alert.category,
            "priority": alert.priority,
            "score": alert.score,
            "time_remaining": alert.time_remaining,
            "why_act_now": alert.why_act_now,
            "potential_reward": alert.potential_reward,
            "action_required": alert.action_required,
            "source": alert.source,
            "url": alert.url or "",
            "date": alert.date,
            "posted_date": alert.created_at.strftime("%Y-%m-%d") if alert.created_at else alert.date,
            "total_views": alert.total_views,
            "total_shares": alert.total_shares,
            "has_viewed": has_viewed,
            "has_shared": has_shared,
            "is_attended": ua.is_attended if ua else False,
            "is_pinned": alert.id in pinned_ids,
        }))

    return {
        "data": [r.model_dump() for r in result],
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit,
    }


@router.get("/alerts/{alert_id}", response_model=AlertResponse)
def get_alert(alert_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Get a specific alert"""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    if user_id:
        user_alert = db.query(UserAlert).filter(
            UserAlert.user_id == user_id,
            UserAlert.alert_id == alert_id
        ).first()

        # Mark as attended if ANY interaction exists
        is_attended = False
        if user_alert:
            is_attended = user_alert.has_viewed or user_alert.has_shared or user_alert.is_attended

        alert_dict = {
            "id": alert.id,
            "title": alert.title,
            "category": alert.category,
            "priority": alert.priority,
            "score": alert.score,
            "time_remaining": alert.time_remaining,
            "why_act_now": alert.why_act_now,
            "potential_reward": alert.potential_reward,
            "action_required": alert.action_required,
            "source": alert.source,
            "url": alert.url or "",
            "date": alert.date,
            "posted_date": alert.created_at.strftime("%Y-%m-%d") if alert.created_at else alert.date,
            "total_views": alert.total_views,
            "total_shares": alert.total_shares,
            "has_viewed": user_alert.has_viewed if user_alert else False,
            "has_shared": user_alert.has_shared if user_alert else False,
            "is_attended": is_attended
        }
        return AlertResponse(**alert_dict)

    return alert


@router.post("/api/alerts/view")
async def view_alert(request: ViewAlertRequest, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    """Mark alert as viewed and award chops if first time"""
    # Get user and alert
    user = current_user
    alert = db.query(Alert).filter(Alert.id == request.alert_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Invalidate user's alerts cache on any view/share/pin action
    await delete_cached(f"alerts:list:{user.id}:all:all:0:100")

    # Check if user_alert record exists
    user_alert = db.query(UserAlert).filter(
        UserAlert.user_id == user.id,
        UserAlert.alert_id == request.alert_id
    ).first()

    chops_earned = 0

    if not user_alert:
        # Create new record - FIRST TIME VIEWING
        chops_to_award = 5 if is_pro_user(user.subscription_status) else 1

        user_alert = UserAlert(
            user_id=user.id,
            alert_id=request.alert_id,
            has_viewed=True,
            is_attended=True,
            viewed_at=datetime.utcnow(),
            chops_earned_from_view=chops_to_award
        )

        # Award chops
        # user.total_chops += chops_to_award
        # user.alert_reading_chops += chops_to_award
        chops_earned = 0

        # Update alert view count
        alert.total_views += 1

        db.add(user_alert)
        db.commit()
        db.refresh(user)  # Refresh to get updated values

        return {
            "message": "Alert viewed successfully",
            "chops_earned": chops_earned,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }

    elif not user_alert.has_viewed:
        # Record exists but not viewed yet - FIRST TIME VIEWING
        chops_to_award = 5 if is_pro_user(user.subscription_status) else 1

        user_alert.has_viewed = True
        user_alert.is_attended = True
        user_alert.viewed_at = datetime.utcnow()
        user_alert.chops_earned_from_view = chops_to_award

        # Award chops
        user.total_chops += chops_to_award
        user.alert_reading_chops += chops_to_award
        chops_earned = chops_to_award

        # Update alert view count
        alert.total_views += 1

        db.commit()
        db.refresh(user)  # Refresh to get updated values

        return {
            "message": "Alert viewed successfully",
            "chops_earned": chops_earned,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }

    else:
        # Already viewed - NO CHOPS AWARDED
        if not user_alert.is_attended:
            user_alert.is_attended = True
            db.commit()

        db.refresh(user)  # Refresh to get current values

        return {
            "message": "Alert already viewed",
            "chops_earned": 0,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }


@router.post("/api/alerts/share")
def share_alert(request: ShareAlertRequest, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    """Mark alert as shared and award chops if first time"""
    # Get user and alert
    user = current_user
    alert = db.query(Alert).filter(Alert.id == request.alert_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Check if user_alert record exists
    user_alert = db.query(UserAlert).filter(
        UserAlert.user_id == user.id,
        UserAlert.alert_id == request.alert_id
    ).first()

    chops_earned = 0

    if not user_alert:
        # Create new record - FIRST TIME SHARING
        chops_to_award = 10 if is_pro_user(user.subscription_status) else 5

        user_alert = UserAlert(
            user_id=user.id,
            alert_id=request.alert_id,
            has_shared=True,
            is_attended=True,
            shared_at=datetime.utcnow(),
            chops_earned_from_share=chops_to_award
        )

        # Award chops
        user.total_chops += chops_to_award
        user.alert_sharing_chops += chops_to_award
        chops_earned = chops_to_award

        # Update alert share count
        alert.total_shares += 1

        db.add(user_alert)
        db.commit()
        db.refresh(user)  # Refresh to get updated values

        return {
            "message": "Alert shared successfully",
            "chops_earned": chops_earned,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }

    elif not user_alert.has_shared:
        # Record exists but not shared yet - FIRST TIME SHARING
        chops_to_award = 10 if is_pro_user(user.subscription_status) else 5

        user_alert.has_shared = True
        user_alert.is_attended = True
        user_alert.shared_at = datetime.utcnow()
        user_alert.chops_earned_from_share = chops_to_award

        # Award chops
        user.total_chops += chops_to_award
        user.alert_sharing_chops += chops_to_award
        chops_earned = chops_to_award

        # Update alert share count
        alert.total_shares += 1

        db.commit()
        db.refresh(user)  # Refresh to get updated values

        return {
            "message": "Alert shared successfully",
            "chops_earned": chops_earned,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }

    else:
        # Already shared - NO CHOPS AWARDED
        db.refresh(user)  # Refresh to get current values

        return {
            "message": "Alert already shared",
            "chops_earned": 0,
            "total_chops": user.total_chops,
            "alert_reading_chops": user.alert_reading_chops,
            "alert_sharing_chops": user.alert_sharing_chops
        }


@router.get("/api/users/{user_id}/alerts/stats")
def get_user_alert_stats(user_id: int, current_user = Depends(get_current_user), db: Session = Depends(get_db)):

    user = current_user

    if user.id != user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Total number of active alerts
    total_active_alerts = db.query(Alert).filter(Alert.is_active == True).count()

    # COUNT alerts where user has actually attended (your real metric)
    attended_count = db.query(UserAlert.alert_id).join(Alert).filter(
        UserAlert.user_id == user.id,
        Alert.is_active == True,
        UserAlert.is_attended == True
    ).distinct().count()

    unattended_count = max(0, total_active_alerts - attended_count)

    # Extra: still useful breakdowns if needed
    viewed_count = db.query(UserAlert.alert_id).join(Alert).filter(
        UserAlert.user_id == user.id,
        Alert.is_active == True,
        UserAlert.has_viewed == True
    ).distinct().count()

    shared_count = db.query(UserAlert.alert_id).join(Alert).filter(
        UserAlert.user_id == user.id,
        Alert.is_active == True,
        UserAlert.has_shared == True
    ).distinct().count()

    return {
        "total_alerts": total_active_alerts,
        "attended_count": attended_count,
        "unattended_count": unattended_count,
        "viewed_count": viewed_count,
        "shared_count": shared_count
    }




@router.post("/api/alerts/pin")
def pin_alert(
    request: PinAlertRequest,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Pin or unpin an alert"""
    user = current_user

    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Verify alert exists
    alert = db.query(Alert).filter(Alert.id == request.alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Check if already pinned
    existing = db.query(UserPinnedAlert).filter(
        UserPinnedAlert.user_id == user.id,
        UserPinnedAlert.alert_id == request.alert_id
    ).first()

    if existing:
        # Unpin
        db.delete(existing)
        db.commit()
        return {"message": "Alert unpinned", "is_pinned": False}

    # Pin
    pinned_record = UserPinnedAlert(
        user_id=user.id,
        alert_id=request.alert_id
    )
    db.add(pinned_record)
    db.commit()

    return {"message": "Alert pinned", "is_pinned": True}

class MarkPageReadRequest(BaseModel):
    alert_ids: List[int]

@router.post("/alerts/mark-page-read")
async def mark_page_alerts_read(
    body: MarkPageReadRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mark a specific set of alert IDs as viewed for the current user.
    Called after each page of alerts is rendered so the sidebar badge
    decrements page-by-page rather than resetting all at once.
    """
    if not body.alert_ids:
        return {"status": "ok", "marked": 0}

    user_id = current_user.id
    existing_map = {
        ua.alert_id: ua
        for ua in db.query(UserAlert).filter(
            UserAlert.user_id == user_id,
            UserAlert.alert_id.in_(body.alert_ids),
        ).all()
    }
    now = datetime.utcnow()
    newly_marked = 0
    for alert_id in body.alert_ids:
        existing = existing_map.get(alert_id)
        if not existing:
            db.add(UserAlert(
                user_id=user_id,
                alert_id=alert_id,
                has_viewed=True,
                is_attended=True,
                viewed_at=now,
                chops_earned_from_view=0,
            ))
            newly_marked += 1
        elif not existing.has_viewed:
            existing.has_viewed = True
            existing.viewed_at = now
            newly_marked += 1
    db.commit()
    return {"status": "ok", "marked": newly_marked}


@router.post("/alerts/mark-all-read")
async def mark_all_alerts_read(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mark every alert as viewed for the current user.
    Called when the user opens the Opportunity Alerts page so the
    sidebar badge counter resets immediately and stays reset across sessions.
    """
    user = current_user
    all_alert_ids = [a.id for a in db.query(Alert.id).filter(Alert.is_active == True).all()]

    # Bulk fetch existing UserAlert rows — eliminates N+1
    existing_map = {
        ua.alert_id: ua
        for ua in db.query(UserAlert).filter(
            UserAlert.user_id == user.id,
            UserAlert.alert_id.in_(all_alert_ids),
        ).all()
    }

    now = datetime.utcnow()
    for alert_id in all_alert_ids:
        existing = existing_map.get(alert_id)
        if not existing:
            db.add(UserAlert(
                user_id=user.id,
                alert_id=alert_id,
                has_viewed=True,
                is_attended=True,
                viewed_at=now,
                chops_earned_from_view=0,
            ))
        elif not existing.has_viewed:
            existing.has_viewed = True
            existing.viewed_at = now

    db.commit()
    # Clear all alert cache variants for this user
    await delete_cached(f"alerts:list:{user.id}:all:all:0:100")
    return {"status": "ok", "message": "All alerts marked as read"}


@router.post("/alerts/admin/backfill-viewed")
async def backfill_viewed_alerts(
    db: Session = Depends(get_db),
    _admin=Depends(get_admin_user),
):
    """
    One-time migration: for every user who has zero UserAlert rows, create
    has_viewed=True records for all currently active alerts.

    These are legacy accounts whose mark-all-read calls never reached the
    backend (double /api prefix bug). They appear to have 229 unread alerts
    but have interacted with the platform normally — they just lack the DB
    records to prove it. After this runs, their unread count becomes 0 and
    the badge behaves correctly going forward.

    Safe to run multiple times: only targets users with NO UserAlert rows.
    """
    # Users who already have at least one UserAlert record — skip them.
    users_with_records = {
        row[0]
        for row in db.query(UserAlert.user_id).distinct().all()
    }

    all_user_ids = [row[0] for row in db.query(User.id).all()]
    legacy_ids = [uid for uid in all_user_ids if uid not in users_with_records]

    if not legacy_ids:
        return {"status": "ok", "seeded_users": 0, "seeded_rows": 0}

    all_alert_ids = [
        row[0]
        for row in db.query(Alert.id).filter(Alert.is_active == True).all()
    ]

    now = datetime.utcnow()
    rows = 0
    for user_id in legacy_ids:
        for alert_id in all_alert_ids:
            db.add(UserAlert(
                user_id=user_id,
                alert_id=alert_id,
                has_viewed=True,
                is_attended=True,
                viewed_at=now,
                chops_earned_from_view=0,
            ))
            rows += 1

    db.commit()
    return {
        "status": "ok",
        "seeded_users": len(legacy_ids),
        "seeded_rows": rows,
    }


# Health check
@router.get("/")
def health_check():
    return {"status": "healthy", "service": "Alerts Management API"}
