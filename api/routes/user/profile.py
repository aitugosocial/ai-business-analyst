from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
import base64

from api.routes.auth.login import get_current_user
from api.utils.sub_utils import sync_user_subscription
from database.pg_connections import get_db

# Import PostgreSQL user models
from database.pg_models import User, CommunityDiscussion, DiscussionLike

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdateRequest(BaseModel):
    """Profile update request model"""
    name: Optional[str] = None
    company_name: Optional[str] = None
    industry: Optional[str] = None
    location: Optional[str] = None
    venture_stage: Optional[str] = None
    bio: Optional[str] = None
    expertise: Optional[List[str]] = None
    open_to: Optional[List[str]] = None
    recent_wins: Optional[List[str]] = None


class PinPostsRequest(BaseModel):
    post_ids: List[int]


class PrivacyToggleRequest(BaseModel):
    hide_public_metrics: bool


@router.get("")
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user profile"""
    sync_user_subscription(db, current_user)

    # Fetch pinned build room posts
    pinned_posts = []
    pinned_ids = getattr(current_user, 'pinned_profile_post_ids', None) or []
    if pinned_ids:
        discussions = db.query(CommunityDiscussion).filter(
            CommunityDiscussion.id.in_(pinned_ids),
            CommunityDiscussion.user_id == current_user.id
        ).all()
        for d in discussions:
            pinned_posts.append({
                "id": d.id,
                "title": d.title,
                "content": d.content[:140] if d.content else "",
                "channel": d.channel.name if d.channel else "general",
                "like_count": d.like_count or 0,
                "reply_count": d.reply_count or 0,
                "created_at": d.created_at.isoformat() if d.created_at else None
            })

    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "company_name": current_user.company_name or "Lavoo Creators",
        "industry": current_user.industry or "Software",
        "location": getattr(current_user, 'location', None) or "Lagos, NG",
        "venture_stage": getattr(current_user, 'venture_stage', None) or "Pre-revenue",
        "bio": current_user.bio,
        "avatar_url": current_user.avatar_url,
        "subscription_status": current_user.subscription_status or "Free",
        "subscription_plan": current_user.subscription_plan,
        "is_beta_user": getattr(current_user, 'is_beta_user', False),
        "total_chops": current_user.total_chops or 0,
        "login_streak": current_user.login_streak or 0,
        "hide_public_metrics": getattr(current_user, 'hide_public_metrics', False),
        "expertise": getattr(current_user, 'expertise', None) or ["Product design", "Community", "No-code", "Brand", "Growth loops"],
        "open_to": getattr(current_user, 'open_to', None) or ["Weekly decision swaps", "Co-founder conversations", "Beta testing partnerships", "Warm intros to creators"],
        "recent_wins": getattr(current_user, 'recent_wins', None) or ["Crossed 40 activated beta founders", "Shipped the Build Room v2 prototype", "Featured as top contributor this month"],
        "pinned_posts": pinned_posts,
        "pinned_ids": pinned_ids,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
    }


@router.put("")
def update_profile(
    profile_data: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update user profile"""
    try:
        # Strict validation: expertise tags cannot exceed 5 items
        if profile_data.expertise is not None and len(profile_data.expertise) > 5:
            raise HTTPException(status_code=400, detail="Expertise list cannot exceed 5 items.")

        # Update fields if provided
        if profile_data.name is not None:
            current_user.name = profile_data.name
        if profile_data.company_name is not None:
            current_user.company_name = profile_data.company_name
        if profile_data.industry is not None:
            current_user.industry = profile_data.industry
        if profile_data.location is not None:
            current_user.location = profile_data.location
        if profile_data.venture_stage is not None:
            current_user.venture_stage = profile_data.venture_stage
        if profile_data.bio is not None:
            current_user.bio = profile_data.bio
        if profile_data.expertise is not None:
            current_user.expertise = profile_data.expertise
        if profile_data.open_to is not None:
            current_user.open_to = profile_data.open_to
        if profile_data.recent_wins is not None:
            current_user.recent_wins = profile_data.recent_wins

        db.commit()
        db.refresh(current_user)

        return {
            "success": True,
            "message": "Profile updated successfully",
            "data": {
                "id": current_user.id,
                "name": current_user.name,
                "email": current_user.email,
                "company_name": current_user.company_name,
                "industry": current_user.industry,
                "location": current_user.location,
                "venture_stage": current_user.venture_stage,
                "bio": current_user.bio,
                "expertise": current_user.expertise,
                "open_to": current_user.open_to,
                "recent_wins": current_user.recent_wins,
                "subscription_status": current_user.subscription_status or "Free",
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {str(e)}")


@router.put("/privacy")
def update_profile_privacy(
    payload: PrivacyToggleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Toggle public metric privacy (Pro/Paid feature only)."""
    sub_status = (current_user.subscription_status or "Free").lower()
    is_subscribed = sub_status in ("active", "trialing", "pro", "enterprise")

    if not is_subscribed:
        raise HTTPException(
            status_code=403,
            detail="Public metric privacy customization is a Pro feature. Please upgrade your account to enable."
        )

    current_user.hide_public_metrics = payload.hide_public_metrics
    db.commit()
    db.refresh(current_user)
    return {
        "success": True,
        "hide_public_metrics": current_user.hide_public_metrics,
        "message": "Public metric privacy updated."
    }


@router.put("/pin-posts")
def update_pinned_posts(
    payload: PinPostsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Pin up to 3 favorite Build Room posts to profile (Pro/Paid feature only)."""
    sub_status = (current_user.subscription_status or "Free").lower()
    is_subscribed = sub_status in ("active", "trialing", "pro", "enterprise")

    if not is_subscribed:
        raise HTTPException(
            status_code=403,
            detail="Pinning favorite decision posts is a Pro feature. Please upgrade your account to enable."
        )

    if len(payload.post_ids) > 3:
        raise HTTPException(status_code=400, detail="You can pin a maximum of 3 decision posts.")

    # Verify posts belong to the current user
    if payload.post_ids:
        owned_count = db.query(CommunityDiscussion).filter(
            CommunityDiscussion.id.in_(payload.post_ids),
            CommunityDiscussion.user_id == current_user.id
        ).count()
        if owned_count != len(payload.post_ids):
            raise HTTPException(status_code=400, detail="One or more selected posts do not belong to you.")

    current_user.pinned_profile_post_ids = payload.post_ids
    db.commit()
    db.refresh(current_user)
    return {
        "success": True,
        "pinned_ids": current_user.pinned_profile_post_ids,
        "message": "Pinned decision posts updated successfully."
    }


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload a user profile avatar. Accepts image files up to 5MB."""
    # Validate file type
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    # Read file content
    content = await file.read()

    # Limit to 5MB
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")

    # Convert to base64 data URL for simple storage (no external storage required)
    mime_type = file.content_type
    b64 = base64.b64encode(content).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    # Store in avatar_url field
    current_user.avatar_url = data_url
    db.commit()

    return {"message": "Avatar uploaded successfully", "avatar_url": data_url}
