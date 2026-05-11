
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List, Optional

from database.pg_connections import get_db
from database.pg_models import User, Referral, ReferralResponse, ReferralStats
from api.routes.auth.login import get_current_user

router = APIRouter(tags=["referrals"])


def extract_user_id(current_user):
    """Helper function to extract user_id from current_user"""
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user
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


@router.get("/referrals/stats")
async def get_referral_stats(current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get referral statistics for the current user"""
    try:
        user_id = extract_user_id(current_user)
        
        # Count total referrals made by this user
        total_referrals = db.query(Referral).filter(
            Referral.referrer_id == user_id
        ).count()
        
        # Get total chops earned from referrals
        total_chops_from_referrals = db.query(Referral).filter(
            Referral.referrer_id == user_id
        ).with_entities(Referral.chops_awarded).all()
        
        total_chops = sum(chop[0] for chop in total_chops_from_referrals if chop[0])
        
        # Get referrals from this month
        from datetime import date
        today = date.today()
        first_day_of_month = today.replace(day=1)
        
        referrals_this_month = db.query(Referral).filter(
            Referral.referrer_id == user_id,
            Referral.created_at >= first_day_of_month
        ).count()
        
        # Get recent referrals (last 5)
        recent_referrals = db.query(Referral).filter(
            Referral.referrer_id == user_id
        ).order_by(Referral.created_at.desc()).limit(5).all()
        
        recent_referrals_data = []
        for referral in recent_referrals:
            referred_user = db.query(User).filter(User.id == referral.referred_user_id).first()
            if referred_user:
                recent_referrals_data.append({
                    "id": referral.id,
                    "referred_user_email": referred_user.email,
                    "referred_user_name": referred_user.username if hasattr(referred_user, 'username') else referred_user.email.split('@')[0],
                    "chops_awarded": referral.chops_awarded,
                    "created_at": referral.created_at.isoformat()
                })
        
        return {
            "total_referrals": total_referrals,
            "total_chops_earned": total_chops,
            "referrals_this_month": referrals_this_month,
            "recent_referrals": recent_referrals_data
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Error fetching referral stats: {str(e)}"
        )


@router.get("/referrals")
async def get_user_referrals(current_user = Depends(get_current_user), db: Session = Depends(get_db), skip: int = 0, limit: int = 50):
    """Get all referrals made by the current user"""
    try:
        user_id = extract_user_id(current_user)
        
        referrals = db.query(Referral).filter(
            Referral.referrer_id == user_id
        ).order_by(Referral.created_at.desc()).offset(skip).limit(limit).all()
        
        referrals_data = []
        for referral in referrals:
            referred_user = db.query(User).filter(User.id == referral.referred_user_id).first()
            if referred_user:
                referrals_data.append({
                    "id": referral.id,
                    "referred_user_id": referral.referred_user_id,
                    "referred_user_email": referred_user.email,
                    "referred_user_name": referred_user.username if hasattr(referred_user, 'username') else referred_user.email.split('@')[0],
                    "chops_awarded": referral.chops_awarded,
                    "created_at": referral.created_at.isoformat(),
                    "is_active": referred_user.subscription_status == "active"
                })
        
        return {
            "referrals": referrals_data,
            "total": len(referrals_data)
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching referrals: {str(e)}"
        )


@router.get("/users/{user_id}/referral-count")
async def get_user_referral_count(user_id: int, db: Session = Depends(get_db)):
    """Get referral count for a specific user"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        referral_count = db.query(Referral).filter(
            Referral.referrer_id == user_id
        ).count()
        
        return {
            "user_id": user_id,
            "referral_count": referral_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching referral count: {str(e)}"
        )


# Health check
@router.get("/referrals/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Referrals API",
        "timestamp": datetime.now(timezone.utc)
    }