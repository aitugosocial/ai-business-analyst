from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, or_
from datetime import datetime, timedelta, timezone

from database.pg_connections import get_db
from database.pg_models import User, Subscriptions, BusinessAnalysis
from api.routes.dependencies import admin_required

router = APIRouter(prefix="/control/users", tags=["admin-users"])


def format_relative_time(dt: datetime) -> str:
    """Format datetime as relative time (e.g., '2 hours ago')"""
    if not dt:
        return "Never"

    # Normalize to naive for arithmetic
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)

    diff = datetime.utcnow() - dt
    seconds = int(diff.total_seconds())

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds} sec{'s' if seconds != 1 else ''} ago"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hr{'s' if hours != 1 else ''} ago"
    else:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"


def to_naive_utc(dt: datetime) -> datetime:
    """Strip timezone info so all comparisons use naive UTC — matching DB column type."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def is_user_inactive(user: User) -> bool:
    """Check if user is inactive (no login for 30 days)"""
    if not user.last_login and not user.updated_at:
        return True

    last_activity = to_naive_utc(user.last_login or user.updated_at)
    cutoff = datetime.utcnow() - timedelta(days=30)
    return last_activity < cutoff


# ── /stats must be declared before /{user_id} to avoid route shadowing ───────
@router.get("/stats")
async def get_user_stats(
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get user statistics"""
    try:
        total_users = db.query(func.count(User.id)).scalar()

        pro_users = db.query(func.count(User.id)).filter(
            User.subscription_status == "active"
        ).scalar()

        free_users = db.query(func.count(User.id)).filter(
            or_(
                User.subscription_status != "active",
                User.subscription_status.is_(None),
            )
        ).scalar()

        deactivated_users = db.query(func.count(User.id)).filter(
            User.is_active == False
        ).scalar()

        # ✅ naive UTC — matches the DB column type (no timezone.utc)
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        inactive_users = db.query(func.count(User.id)).filter(
            User.is_active == True,
            or_(
                User.last_login < cutoff_date,
                User.last_login.is_(None),
            ),
        ).scalar()

        return {
            "total": total_users,
            "pro": pro_users,
            "free": free_users,
            "deactivated": deactivated_users,
            "inactive": inactive_users,
        }

    except Exception as e:
        print(f"Error in user stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── List route (empty string = /control/users) ────────────────────────────────
@router.get("")
async def get_users(
    limit: int = 10,
    page: int = 1,
    search: str = None,
    status: str = None,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get users with server-side pagination and filtering"""
    offset = (page - 1) * limit

    query = db.query(User)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (User.name.ilike(search_term)) | (User.email.ilike(search_term))
        )

    # ✅ naive UTC — matches DB column type, avoids offset-naive/aware crash
    cutoff_date = datetime.utcnow() - timedelta(days=30)

    if status and status != "all":
        if status == "active":
            query = query.filter(
                User.is_active == True,
                or_(
                    User.last_login >= cutoff_date,
                    User.last_login.is_(None),
                ),
            )
        elif status == "inactive":
            query = query.filter(
                User.is_active == True,
                or_(
                    User.last_login < cutoff_date,
                    User.last_login.is_(None),
                ),
            )
        elif status in ("suspended", "deactivated"):
            query = query.filter(User.is_active == False)
        elif status == "free":
            query = query.filter(
                or_(
                    User.subscription_status != "active",
                    User.subscription_status.is_(None),
                )
            )
        elif status == "pro":
            query = query.filter(User.subscription_status == "active")

    total = query.count()

    # ✅ Single joined query — eliminates the N+1 per-user DB hit
    analysis_counts = (
        db.query(
            BusinessAnalysis.user_id,
            func.count(BusinessAnalysis.id).label("count"),
        )
        .group_by(BusinessAnalysis.user_id)
        .subquery()
    )

    rows = (
        query.outerjoin(analysis_counts, User.id == analysis_counts.c.user_id)
        .add_columns(func.coalesce(analysis_counts.c.count, 0).label("analysis_count"))
        .order_by(desc(User.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    for row in rows:
        user = row[0]
        analysis_count = row[1]

        if not user.is_active:
            user_status = "suspended"
        elif is_user_inactive(user):
            user_status = "inactive"
        else:
            user_status = "active"

        last_active_dt = user.last_login or user.updated_at
        last_active = format_relative_time(last_active_dt)

        result.append(
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": "admin" if user.is_admin else "user",
                "plan": user.subscription_plan or "Free",
                "subscription_status": user.subscription_status or "none",
                "status": user_status,
                "is_active": user.is_active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "joinDate": user.created_at.strftime("%Y-%m-%d") if user.created_at else None,
                "lastActive": last_active,
                "last_active": last_active,
                "analyses": analysis_count,
                "avatar": "".join(
                    [n[0] for n in (user.name or "U").split(" ")[:2]]
                ).upper(),
            }
        )

    return {
        "users": result,
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit,
    }


# ── Dynamic route AFTER static routes (/stats must not be shadowed) ───────────
@router.get("/{user_id}")
async def get_user_details(
    user_id: int,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get full user details for modal"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from api.utils.sub_utils import sync_user_subscription
    user = sync_user_subscription(db, user)

    days_remaining = 0
    if user.subscription_status == "active" and user.subscriptions:
        first_sub = (
            db.query(Subscriptions)
            .filter(Subscriptions.user_id == user.id)
            .order_by(Subscriptions.created_at.asc(), Subscriptions.id.asc())
            .first()
        )
        if first_sub and first_sub.end_date:
            # ✅ Normalize both sides to naive UTC before comparing
            end_date = to_naive_utc(first_sub.end_date)
            delta = end_date - datetime.utcnow()
            days_remaining = max(0, delta.days)

    from database.pg_models import Referral
    referrals = db.query(Referral).filter(Referral.referrer_id == user.id).all()
    referral_names = [ref.referred_user.name for ref in referrals if ref.referred_user]

    if not user.is_active:
        status = "suspended"
    elif is_user_inactive(user):
        status = "inactive"
    else:
        status = "active"

    last_active_dt = user.last_login or user.updated_at
    last_active = format_relative_time(last_active_dt)

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "avatar": "".join([n[0] for n in user.name.split(" ")[:2]]).upper(),
        "joinDate": user.created_at.isoformat() if user.created_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "lastActive": last_active,
        "last_active": last_active,
        "status": status,
        "is_active": user.is_active,
        "subscription_status": user.subscription_status,
        "subscription_plan": user.subscription_plan or "Free",
        "total_chops": user.total_chops or 0,
        "referral_chops": user.referral_chops or 0,
        "alert_read_chops": user.alert_reading_chops or 0,
        "alert_share_chops": user.alert_sharing_chops or 0,
        "insight_read_chops": user.insight_reading_chops or 0,
        "insight_share_chops": user.insight_sharing_chops or 0,
        "referral_code": user.referral_code,
        "total_referrals": user.referral_count or 0,
        "referred_users": referral_names,
        "days_remaining": days_remaining,
    }


@router.patch("/{user_id}/status")
async def update_user_status(
    user_id: int,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Toggle user active status (Deactivate/Activate)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_id_from_token = (
        current_user.get("id") or current_user.get("user", {}).get("id")
        if isinstance(current_user, dict)
        else current_user.id
    )

    if user.id == user_id_from_token:
        raise HTTPException(
            status_code=400, detail="Cannot deactivate your own admin account"
        )

    user.is_active = not user.is_active
    db.commit()

    action = "activated" if user.is_active else "deactivated"
    new_status = "active" if user.is_active else "suspended"

    return {
        "status": "success",
        "message": f"User {user.email} has been {action}",
        "new_status": new_status,
    }


@router.post("/generate-referral-codes")
async def generate_missing_referral_codes(
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Generate referral codes for all users who don't have one"""
    import random
    import string

    def generate_referral_code(length=8):
        chars = string.ascii_uppercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    users_without_codes = db.query(User).filter(
        or_(User.referral_code.is_(None), User.referral_code == "")
    ).all()

    updated_count = 0
    for user in users_without_codes:
        new_code = generate_referral_code()
        while db.query(User).filter(User.referral_code == new_code).first():
            new_code = generate_referral_code()
        user.referral_code = new_code
        updated_count += 1

    db.commit()

    return {
        "status": "success",
        "message": f"Generated referral codes for {updated_count} users",
        "updated_count": updated_count,
    }


@router.post("/sync-subscriptions")
async def sync_subscription_statuses(
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Sync subscription statuses for all users"""
    users = db.query(User).all()
    updated_count = 0

    for user in users:
        original_status = user.subscription_status
        original_plan = user.subscription_plan

        active_sub = None
        if user.subscriptions:
            for sub in user.subscriptions:
                if sub.status == "active" and sub.end_date:
                    # ✅ Normalize to naive UTC before comparing
                    end_date = to_naive_utc(sub.end_date)
                    if end_date > datetime.utcnow():
                        active_sub = sub
                        break
                    else:
                        sub.status = "expired"

        if active_sub:
            user.subscription_status = "active"
            user.subscription_plan = active_sub.plan_type or "Pro"
        else:
            user.subscription_status = "Free"
            user.subscription_plan = "Free"

        if (
            user.subscription_status != original_status
            or user.subscription_plan != original_plan
        ):
            updated_count += 1

    db.commit()

    return {
        "status": "success",
        "message": f"Synced subscription statuses for {updated_count} users",
        "updated_count": updated_count,
    }