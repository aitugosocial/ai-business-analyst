
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from database.pg_connections import get_db
from database.pg_models import (User, Insight, UserInsight, UserResponse, UserCreate, InsightResponse, InsightCreate,
                            ViewInsightRequest, ShareInsightRequest, UserPinnedInsight, ChopsBreakdown, PinInsightRequest, BusinessAnalysis)
from api.routes.auth.login import get_current_user
from api.routes.user.alerts import is_pro_user
from api.cache import get_cached, set_cached, delete_cached, CacheTTL

from typing import Optional, List


router = APIRouter(tags=["insights"])

# Get current user (simplified - implement your auth)
@router.get("/users/person")
def get_current_user_route(current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "id": current_user.id,
        "email": current_user.email,
        "subscription_status": current_user.subscription_status,
        "total_chops": current_user.total_chops,
        "alert_reading_chops": current_user.alert_reading_chops,
        "alert_sharing_chops": current_user.alert_sharing_chops,
        "insight_reading_chops": current_user.insight_reading_chops,
        "insight_sharing_chops": current_user.insight_sharing_chops,
        "referral_chops": current_user.referral_chops,
        "referral_count": current_user.referral_count,
        "referral_code": current_user.referral_code
    }

@router.get("/user/stats")
def get_user_stats(
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get user statistics including chops breakdown"""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = current_user

    # Get total analyses count for user
    from database.pg_models import BusinessAnalysis
    total_analyses = db.query(BusinessAnalysis).filter(BusinessAnalysis.user_id == user.id).count()

    # Get total insights
    total_insights = db.query(Insight).filter(Insight.is_active == True).count()
    insight_reading_chops = user.insight_reading_chops or 0
    insight_sharing_chops = user.insight_sharing_chops or 0
    total_insight_chops = insight_reading_chops + insight_sharing_chops
    return {
        "total_chops": user.total_chops,
        "is_pro": is_pro_user(user.subscription_status),
        "total_insights": total_insights,
        "total_analyses": total_analyses, # Added this
        "viewed_insight_chops": insight_reading_chops,
        "shared_insight_chops": insight_sharing_chops,
        "total_insight_chops": total_insight_chops
    }

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


@router.post("/insights", response_model=InsightResponse, status_code=status.HTTP_201_CREATED)
def create_alert(insight: InsightCreate, db: Session = Depends(get_db)):
    """Create a new alert"""
    db_insight = Insight(**insight.dict())
    db.add(db_insight)
    db.commit()
    db.refresh(db_insight)
    return db_insight


@router.get("/insights")
async def get_insights(
    page: int = 1,
    limit: int = 5,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get paginated insights with user-specific data"""
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")

        user = current_user

        # Try to get from cache first (5 minute TTL for insights)
        cache_key = f"insights:list:{user.id}:{page}:{limit}"
        cached = await get_cached(cache_key)
        if cached:
            return cached

        # Get all active insights
        query = db.query(Insight).filter(Insight.is_active == True)

        # Get pinned insights for this user
        pinned_insight_ids = [
            p.insight_id for p in db.query(UserPinnedInsight)
            .filter(UserPinnedInsight.user_id == user.id)
            .all()
        ]

        # Get pinned insights
        pinned_insights = query.filter(Insight.id.in_(pinned_insight_ids)).all() if pinned_insight_ids else []

        # Get non-pinned insights
        unpinned_query = query.filter(~Insight.id.in_(pinned_insight_ids)) if pinned_insight_ids else query

        # Calculate pagination for unpinned insights
        total_unpinned = unpinned_query.count()
        skip = (page - 1) * limit
        unpinned_insights = unpinned_query.order_by(Insight.created_at.desc()).offset(skip).limit(limit).all()

        # Combine: pinned first, then unpinned
        all_insights = pinned_insights + unpinned_insights

        # Build response
        insights_response = []
        for insight in all_insights:
            user_insight = db.query(UserInsight).filter(
                UserInsight.user_id == user.id,
                UserInsight.insight_id == insight.id
            ).first()

            # Mark as attended if ANY interaction exists
            is_attended = False
            if user_insight:
                is_attended = user_insight.has_viewed or user_insight.has_shared or user_insight.is_attended

            insight_dict = {
                "id": insight.id,
                "title": insight.title,
                "category": insight.category,
                "read_time": insight.read_time,
                "date": insight.date,
                "source": insight.source,
                "url": insight.url or "",
                "what_changed": insight.what_changed,
                "why_it_matters": insight.why_it_matters,
                "action_to_take": insight.action_to_take,
                "is_read": user_insight.has_viewed if user_insight else False,
                "is_shared": user_insight.has_shared if user_insight else False,
                "is_pinned": insight.id in pinned_insight_ids,
                "is_attended": is_attended
            }
            insights_response.append(insight_dict)

        total_insights = total_unpinned + len(pinned_insights)
        total_pages = max(1, (total_unpinned + limit - 1) // limit)

        result = {
            "insights": insights_response,
            "current_page": page,
            "total_pages": total_pages,
            "total_insights": total_insights,
            "is_pro": is_pro_user(user.subscription_status)
        }

        # Cache the result for 5 minutes
        await set_cached(cache_key, result, CacheTTL.MEDIUM)

        return result

    except Exception as e:
        print(f"Error in get_insights: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching insights: {str(e)}")

@router.get("/insights/{insight_id}", response_model=InsightResponse)
def get_insight(insight_id: int, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a specific insight"""
    user = current_user

    insight = db.query(Insight).filter(Insight.id == insight_id).first()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    user_insight = db.query(UserInsight).filter(
        UserInsight.user_id == user.id,
        UserInsight.insight_id == insight_id
    ).first()

    # check if the insight is pinned or not
    pinned_insight_id = [
        p.insight_id for p in db.query(UserPinnedInsight).filter(
            UserPinnedInsight.user_id == user.id,
            UserPinnedInsight.insight_id == insight_id)
        .all()
    ]

    # Mark as attended if ANY interaction exists
    is_attended = False
    if user_insight:
        is_attended = user_insight.has_viewed or user_insight.has_shared or user_insight.is_attended

    insight_dict = {
        "id": insight.id,
        "title": insight.title,
        "category": insight.category,
        "read_time": insight.read_time,
        "what_changed": insight.what_changed,
        "why_it_matters": insight.why_it_matters,
        "action_to_take": insight.action_to_take,
        "source": insight.source,
        "url": insight.url or "",
        "date": insight.date,
        "total_views": insight.total_views,
        "total_shares": insight.total_shares,
        "is_read": user_insight.has_viewed if user_insight else False,
        "is_shared": user_insight.has_shared if user_insight else False,
        "is_pinned": insight.id in pinned_insight_id,
        "is_attended": is_attended
    }
    return InsightResponse(**insight_dict)



@router.get("/users/{user_id}/insights/stats")
def get_user_insight_stats(user_id: int, db: Session = Depends(get_db)):
    """Get user's alert interaction statistics"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    total_insights = db.query(Insight).filter(Insight.is_active == True).count()

    # All alerts the user has interacted with (viewed OR shared OR attended)
    attended_alert_ids = db.query(UserInsight.insight_id).filter(
        UserInsight.user_id == user_id
    ).filter(
        (UserInsight.has_viewed == True) |
        (UserInsight.has_shared == True) |
        (UserInsight.is_attended == True)
    ).subquery()

    viewed_count = db.query(UserInsight).filter(
        UserInsight.user_id == user_id,
        UserInsight.has_viewed == True
    ).count()

    shared_count = db.query(UserInsight).filter(
        UserInsight.user_id == user_id,
        UserInsight.has_shared == True
    ).count()

    # Count as attended if ANY interaction exists
    unattended_count = db.query(Insight).filter(
        Insight.is_active == True,
        ~Insight.id.in_(attended_alert_ids)  # This is the key!
    ).count()

    attended_count = total_insights - unattended_count

    return {
        "total_insights": total_insights,
        "viewed_count": viewed_count,
        "shared_count": shared_count,
        "attended_count": attended_count,
        "unattended_count": unattended_count
    }


@router.post("/insights/view")
def view_insight( request: ViewInsightRequest, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    # Get user and insight
    user = current_user
    insight = db.query(Insight).filter(Insight.id == request.insight_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    # Check if user_alert record exists
    user_insight = db.query(UserInsight).filter(
        UserInsight.user_id == user.id,
        UserInsight.insight_id == request.insight_id
    ).first()

    if not user_insight:
        # Create new record
        user_insight = UserInsight(
            user_id=user.id,
            insight_id=request.insight_id,
            has_viewed=True,
            is_attended=True,
            viewed_at=datetime.now(timezone.utc)
        )

        # Award chops based on subscription status (active = 5, free = 1)
        chops_to_award = 5 if is_pro_user(user.subscription_status) else 1
        user_insight.chops_earned_from_view = chops_to_award
        user.total_chops += chops_to_award
        user.insight_reading_chops += chops_to_award

        # Update alert view count
        insight.total_views += 1

        db.add(user_insight)
        db.commit()

        return {
            "message": "Insight viewed successfully",
            "chops_earned": chops_to_award,
            "total_chops": user.total_chops,
            "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
            "insight_reading_chops": user.insight_reading_chops,
            "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
        }
    else:
        # Already viewed, just mark as attended if not already
        if not user_insight.has_viewed:
            user_insight.has_viewed = True
            user_insight.viewed_at = datetime.now(timezone.utc)
            insight.total_views += 1

            # Award chops based on subscription status
            chops_to_award = 5 if is_pro_user(user.subscription_status) else 1
            user_insight.chops_earned_from_view = chops_to_award
            user.total_chops += chops_to_award
            user.insight_reading_chops += chops_to_award

            db.commit()

            return {
                "message": "Insight viewed successfully",
                "chops_earned": chops_to_award,
                "total_chops": user.total_chops,
                "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
                "insight_reading_chops": user.insight_reading_chops,
                "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
            }

        if not user_insight.is_attended:
            user_insight.is_attended = True
            db.commit()

        return {
            "message": "Insight already viewed",
            "chops_earned": 0,
            "total_chops": user.total_chops,
            "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
            "insight_reading_chops": user.insight_reading_chops,
            "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
        }


@router.post("/insights/share")
def share_insight( request: ShareInsightRequest, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    # Get user and alert
    user = current_user
    insight = db.query(Insight).filter(Insight.id == request.insight_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not insight:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Check if user insight record exists
    user_insight = db.query(UserInsight).filter(
        UserInsight.user_id == user.id,
        UserInsight.insight_id == request.insight_id
    ).first()

    if not user_insight:
        # Create new record
        user_insight = UserInsight(
            user_id=user.id,
            insight_id=request.insight_id,
            has_shared=True,
            is_attended=True,  # Mark as attended when sharing
            shared_at=datetime.now(timezone.utc)
        )

        # Award chops based on subscription status (active = 10, free = 5)
        chops_to_award = 10 if is_pro_user(user.subscription_status) else 5
        user_insight.chops_earned_from_share = chops_to_award
        user.total_chops += chops_to_award
        user.insight_sharing_chops += chops_to_award

        # Update alert share count
        insight.total_shares += 1

        db.add(user_insight)
        db.commit()

        return {
            "message": "Insight shared successfully",
            "chops_earned": chops_to_award,
            "total_chops": user.total_chops,
            "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
            "insight_reading_chops": user.insight_reading_chops,
            "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
        }
    else:
        # Check if already shared
        if not user_insight.has_shared:
            user_insight.has_shared = True
            user_insight.is_attended = True  # Mark as attended when sharing
            user_insight.shared_at = datetime.now(timezone.utc)

            # Award chops based on subscription status
            chops_to_award = 10 if is_pro_user(user.subscription_status) else 5
            user_insight.chops_earned_from_share = chops_to_award
            user.total_chops += chops_to_award
            user.insight_sharing_chops += chops_to_award

            # Update alert share count
            insight.total_shares += 1

            db.commit()

            return {
                "message": "Alert shared successfully",
                "chops_earned": chops_to_award,
                "total_chops": user.total_chops,
                "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
                "insight_reading_chops": user.insight_reading_chops,
                "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
            }

        return {
            "message": "Alert already shared",
            "chops_earned": 0,
            "total_chops": user.total_chops,
            "insight_sharing_chops": user.insight_sharing_chops,  # ← ADD THIS
            "insight_reading_chops": user.insight_reading_chops,
            "total_insight_chops": user.insight_reading_chops + user.insight_sharing_chops
        }


@router.post("/insights/pin")
def pin_insight(
    request: PinInsightRequest,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Pin or unpin an insight"""
    user = current_user

    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Check if already pinned
    existing = db.query(UserPinnedInsight).filter(
        UserPinnedInsight.user_id == user.id,
        UserPinnedInsight.insight_id == request.insight_id
    ).first()

    if existing:
        # Unpin
        db.delete(existing)
        db.commit()
        return {"message": "Insight unpinned", "is_pinned": False}

    # Pin
    pinned_record = UserPinnedInsight(
        user_id=user.id,
        insight_id=request.insight_id
    )
    db.add(pinned_record)
    db.commit()

    return {"message": "Insight pinned", "is_pinned": True}


