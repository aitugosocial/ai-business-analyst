from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_
from datetime import datetime

from database.pg_connections import get_db
from database.pg_models import User, UserRole
from api.routes.dependencies import admin_required

router = APIRouter(prefix="/control/permissions", tags=["admin-permissions"])


@router.get("")
async def get_users_for_permissions(
    limit: int = 10,
    page: int = 1,
    search: str = "",
    role: str = "",          # filter: "moderator" | "normal_user" | None = all
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """
    Return paginated users for the permissions management page.
    Includes role field so admin can see and toggle moderator status.
    """
    offset = (page - 1) * limit
    query = db.query(User)

    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(User.name.ilike(term), User.email.ilike(term))
        )

    if role == "moderator":
        query = query.filter(User.role == UserRole.MODERATOR.value)
    elif role == "normal_user":
        query = query.filter(User.role == UserRole.NORMAL.value)

    total = query.count()

    users = (
        query.order_by(desc(User.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    for user in users:
        result.append({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "is_admin": user.is_admin,
            "is_active": user.is_active,
            "role": user.role or UserRole.NORMAL.value,
            "subscription_status": user.subscription_status or "none",
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "avatar": "".join(
                [n[0] for n in (user.name or "U").split(" ")[:2]]
            ).upper(),
        })

    return {
        "users": result,
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit,
    }


@router.patch("/{user_id}/role")
async def toggle_user_role(
    user_id: int,
    current_user: User = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """
    Toggle a user's community role between normal_user and moderator.
    Cannot change your own role or another admin's role.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Resolve the calling admin's id regardless of dict vs ORM object
    caller_id = (
        current_user.get("id") or current_user.get("user", {}).get("id")
        if isinstance(current_user, dict)
        else current_user.id
    )

    if user.id == caller_id:
        raise HTTPException(
            status_code=400, detail="Cannot change your own role"
        )

    if user.is_admin:
        raise HTTPException(
            status_code=400, detail="Cannot change the role of a platform admin"
        )

    # Toggle between normal_user ↔ moderator
    if user.role == UserRole.MODERATOR.value:
        user.role = UserRole.NORMAL.value
        new_role = UserRole.NORMAL.value
        action = "removed as moderator"
    else:
        user.role = UserRole.MODERATOR.value
        new_role = UserRole.MODERATOR.value
        action = "made a moderator"

    db.commit()

    return {
        "status": "success",
        "message": f"{user.name} has been {action}",
        "new_role": new_role,
        "user_id": user.id,
    }