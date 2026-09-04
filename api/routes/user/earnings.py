from fastapi import APIRouter, Depends, HTTPException, Header, Cookie
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, case, extract
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError
import traceback
from decimal import Decimal

from database.pg_connections import get_db
from database.pg_models import User, Referral, Subscriptions, Commission
from subscriptions.commission_service import CommissionService

from api.routes.auth.login import get_current_user

router = APIRouter(prefix="", tags=["earnings"])

COMMISSION_RATE = 0.4           # 40% — fallback/legacy flat rate; see CommissionService._get_rate_for_referrer
COMMISSION_RATE_PARTNER = 0.5   # 50% — actual payout for partners/staff (not shown)


# Remove local get_user_id_from_request and risky SECRET_KEY fallback


def get_month_ranges():
    """Pre-calculate month ranges to avoid repeated calculations"""
    today = datetime.now()
    first_day_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if today.month == 1:
        last_month = today.replace(year=today.year-1, month=12, day=1)
    else:
        # Reset day to 1 FIRST to avoid day-out-of-range errors
        # e.g. March 31 -> replace(month=2) would fail since Feb has no day 31
        last_month = today.replace(day=1, month=today.month-1)

    first_day_last_month = last_month.replace(day=1)
    last_day_last_month = (last_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    return {
        'this_month_start': first_day_this_month,
        'last_month_start': first_day_last_month,
        'last_month_end': last_day_last_month
    }



@router.get("/user/me")
async def get_current_user_data(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user's data - OPTIMIZED"""
    # current_user is already the User object from DB
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "total_chops": current_user.total_chops or 0,
        "referral_chops": current_user.referral_chops or 0,
        "referral_count": current_user.referral_count or 0,
        "referral_code": current_user.referral_code or "",
        "subscription_status": current_user.subscription_status or "inactive",
        "subscription_plan": current_user.subscription_plan or "free",
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None
    }
        



@router.get("/earnings/summary")
async def get_earnings_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get earnings summary - commission-based model"""
    user_id = current_user.id
    try:
        print(f"[/earnings/summary] Starting for user {user_id}")
        date_ranges = get_month_ranges()
        
        referral_user_ids_subquery = db.query(Referral.referred_user_id).filter(
            Referral.referrer_id == user_id
        ).subquery()

        # Count paid referrals (users who have an 'active' subscription status)
        paid_referrals = db.query(func.count(User.id)).filter(
            User.id.in_(db.query(referral_user_ids_subquery.c.referred_user_id)),
            func.lower(User.subscription_status) == "active"
        ).scalar() or 0
        
        # Calculate commissions from the Commission table (more accurate).
        # 'auto_settled' (a Flutterwave subaccount split, or the new
        # immediate-payout path) counts as paid — it previously matched
        # neither case here, so a freshly auto-settled commission vanished
        # from both totals until something else (like an admin action)
        # happened to touch it. It's later flipped to 'paid' once the
        # payout webhook confirms completion; both statuses mean money
        # already moved, so both belong in the paid bucket.
        commission_stats = db.query(
            func.sum(case((Commission.status.in_(['paid', 'auto_settled']), Commission.amount), else_=0)).label('paid_amount'),
            func.sum(case((Commission.status.in_(['pending', 'processing', 'approved']), Commission.amount), else_=0)).label('pending_amount'),
            func.count(case((Commission.status.in_(['paid', 'auto_settled']), 1), else_=None)).label('paid_count')
        ).filter(Commission.user_id == user_id).first()
        
        print(f"[DEBUG] Raw Commission Stats for User {user_id}: {commission_stats}")
        
        paid_commissions = float(commission_stats.paid_amount or 0)

        pending_commissions = float(commission_stats.pending_amount or 0)
        total_commissions = paid_commissions + pending_commissions
        
        # Count distinct paid referrals from Commission table if possible, or fallback to subscription query
        # Actually, using the previous query for paid_referrals count is fine as it counts users who successfully subscribed
        
        print(f"[/earnings/summary] Commissions: Paid=${paid_commissions}, Pending=${pending_commissions}, Total=${total_commissions}")
        
        # Get referral stats
        stats = db.query(
            func.count(Referral.id).label('total_referrals'),
            func.coalesce(func.sum(Referral.chops_awarded), 0).label('referral_chops'),
            func.sum(case((Referral.created_at >= date_ranges['this_month_start'], 1), else_=0)).label('this_month'),
            func.sum(case((and_(
                Referral.created_at >= date_ranges['last_month_start'],
                Referral.created_at <= date_ranges['last_month_end']
            ), 1), else_=0)).label('last_month')
        ).filter(Referral.referrer_id == user_id).first()
        
        total_referrals = stats.total_referrals or 0
        referral_chops = int(stats.referral_chops or 0)
        this_month_count = int(stats.this_month or 0)
        last_month_count = int(stats.last_month or 0)
        
        # Calculate growth rate
        if last_month_count == 0 and this_month_count == 0:
            growth_rate = 0.0
        elif last_month_count == 0 and this_month_count > 0:
            growth_rate = 100.0
        elif last_month_count > 0 and this_month_count == 0:
            growth_rate = -100.0
        else:
            growth_rate = round(((this_month_count - last_month_count) / last_month_count) * 100, 1)
        
        print(f"[/earnings/summary] Growth rate: {growth_rate}%")
        
        result_payload = {
            "totalCommissions": total_commissions,
            "paidCommissions": paid_commissions,
            "pendingCommissions": pending_commissions,
            "totalPaidReferrals": paid_referrals,
            "referralChops": referral_chops,
            "growthRate": growth_rate,
            "totalRevenue": total_commissions,
            "transactions": total_referrals,
            "avgOrderValue": round(total_commissions / paid_referrals, 2) if paid_referrals > 0 else 0,
            # 40% for a subscribed/active referrer (and for partners — their
            # actual 50% payout stays hidden, same as before); 15% for a
            # referrer on the free plan. See _get_rate_for_referrer.
            "commissionRate": int(CommissionService._get_rate_for_referrer(user_id, db) * 100) if not getattr(current_user, "is_partner", False) else 40
        }
        print(f"[/earnings/summary] RETURNING PAYLOAD: {result_payload}")
        return result_payload

    except Exception as e:
        print(f"[ERROR] /earnings/summary: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch earnings summary: {str(e)}")


@router.get("/api/referrals/stats")
async def get_referral_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get referral statistics"""
    user_id = current_user.id
    try:
        print(f"[/api/referrals/stats] Starting for user {user_id}")
        date_ranges = get_month_ranges()
        
        # Basic referral stats
        stats = db.query(
            func.count(Referral.id).label('total'),
            func.coalesce(func.sum(Referral.chops_awarded), 0).label('chops'),
            func.sum(case((Referral.created_at >= date_ranges['this_month_start'], 1), else_=0)).label('this_month')
        ).filter(Referral.referrer_id == user_id).first()
        
        # Get recent referrals with user info
        recent_refs = db.query(Referral).options(
            joinedload(Referral.referred_user)
        ).filter(
            Referral.referrer_id == user_id
        ).order_by(
            Referral.created_at.desc()
        ).limit(10).all()
        
        recent_referrals = [
            {
                "id": ref.id,
                "referred_user_email": ref.referred_user.email if ref.referred_user else "Unknown",
                "referred_user_name": ref.referred_user.name if ref.referred_user else "Unknown",
                "chops_awarded": ref.chops_awarded or 0,
                "created_at": ref.created_at.isoformat() if ref.created_at else None
            }
            for ref in recent_refs
        ]
        
        print(f"[/api/referrals/stats] ✅ Complete")
        return {
            "total_referrals": stats.total or 0,
            "total_chops_earned": int(stats.chops or 0),
            "referrals_this_month": int(stats.this_month or 0),
            "recent_referrals": recent_referrals
        }
        
    except Exception as e:
        print(f"[ERROR] /api/referrals/stats: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch referral stats: {str(e)}")


@router.get("/earnings/monthly")
async def get_monthly_performance(
    months: int = 6,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get monthly performance with commission calculations"""
    user_id = current_user.id
    try:
        print(f"[/earnings/monthly] Starting for user {user_id}")
        user_commission_rate = CommissionService._get_rate_for_referrer(user_id, db)
        today = datetime.now()
        monthly_data = []
        
        # Build month ranges
        month_ranges = []
        for i in range(months):
            if today.month - i <= 0:
                month = today.month - i + 12
                year = today.year - 1
            else:
                month = today.month - i
                year = today.year
            
            month_start = datetime(year, month, 1)
            if month == 12:
                month_end = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = datetime(year, month + 1, 1) - timedelta(days=1)
            
            month_ranges.append((month_start, month_end))
        
        oldest_date = month_ranges[-1][0]
        
        # Get all referrals in date range
        all_refs = db.query(
            Referral.id,
            Referral.created_at,
            Referral.referred_user_id,
            Referral.chops_awarded
        ).filter(
            Referral.referrer_id == user_id,
            Referral.created_at >= oldest_date
        ).all()
        
        print(f"[/earnings/monthly] Found {len(all_refs)} referrals")
        
        # Get referred user IDs
        referred_user_ids = [ref.referred_user_id for ref in all_refs]
        
        # Get subscription payments for referred users
        if referred_user_ids:
            subscription_payments = db.query(
                Subscriptions.user_id,
                Subscriptions.amount,
                Subscriptions.created_at,
                Subscriptions.status
            ).filter(
                Subscriptions.user_id.in_(referred_user_ids),
                Subscriptions.status == "successful",
                Subscriptions.created_at >= oldest_date
            ).all()
            
            print(f"[/earnings/monthly] Found {len(subscription_payments)} successful payments")
        else:
            subscription_payments = []
        
        # Process each month
        for month_start, month_end in reversed(month_ranges):
            # Referrals made this month
            month_refs = [
                ref for ref in all_refs
                if month_start <= ref.created_at <= month_end
            ]
            
            # Chops earned this month
            month_chops = sum(ref.chops_awarded or 0 for ref in month_refs)
            
            # Payments made by referred users this month
            month_payments = [
                payment for payment in subscription_payments
                if month_start <= payment.created_at <= month_end
            ]
            
            # Calculate commissions for this month, at this user's own actual
            # rate (40% subscribed / 15% free / 50% partner) rather than a
            # flat assumption — see CommissionService._get_rate_for_referrer.
            month_subscription_total = sum(float(p.amount) for p in month_payments)
            month_commissions = round(month_subscription_total * float(user_commission_rate), 2)
            
            # Count paid users this month (users who made payment)
            paid_users_this_month = len(set(p.user_id for p in month_payments))
            
            monthly_data.append({
                "month": month_start.strftime("%b"),
                "year": month_start.year,
                "referral_count": len(month_refs),
                "paid_referral_count": db.query(func.count(User.id)).filter(
                    User.id.in_([p.user_id for p in month_payments] or [0]),
                    User.subscription_status == "active"
                ).scalar() or 0,
                "referral_chops": month_chops,
                "commission": month_commissions,
                "revenue": month_commissions
            })
        
        print(f"[/earnings/monthly] ✅ Complete: {len(monthly_data)} months")
        return monthly_data
        
    except Exception as e:
        print(f"[ERROR] /earnings/monthly: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch monthly performance: {str(e)}")


@router.get("/earnings/available-years")
async def get_available_years(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get list of years from 2025 onwards up to current year"""
    user_id = current_user.id
    try:
        print(f"[/earnings/available-years] Starting for user {user_id}")
        
        current_year = datetime.now().year
        
        # Always start from 2025 and go up to current year
        start_year = 2025
        years = list(range(start_year, current_year + 1))
        
        print(f"[/earnings/available-years] ✅ Years: {years}")
        return {"years": sorted(years, reverse=True)}  # Most recent first
        
    except Exception as e:
        print(f"[ERROR] /earnings/available-years: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch available years: {str(e)}")


@router.get("/earnings/monthly/{year}/{month}")
async def get_monthly_metrics_for_period(
    year: int,
    month: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get metrics for a specific month and year"""
    user_id = current_user.id
    try:
        print(f"[/earnings/monthly/{year}/{month}] Starting for user {user_id}")
        
        # Validate month
        if month < 1 or month > 12:
            raise HTTPException(status_code=400, detail="Month must be between 1 and 12")
        
        # Validate year (not too far in past or future)
        current_year = datetime.now().year
        if year < 2024 or year > current_year + 1:
            raise HTTPException(status_code=400, detail=f"Year must be between 2024 and {current_year + 1}")
        
        # Calculate month boundaries
        month_start = datetime(year, month, 1)
        if month == 12:
            month_end = datetime(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = datetime(year, month + 1, 1) - timedelta(days=1)
        
        print(f"[/earnings/monthly/{year}/{month}] Date range: {month_start} to {month_end}")
        
        # Total Referrals: Users who registered with this user's referral code in this month
        month_referrals = db.query(
            func.count(Referral.id).label('referral_count'),
            func.coalesce(func.sum(Referral.chops_awarded), 0).label('referral_chops')
        ).filter(
            Referral.referrer_id == user_id,
            Referral.created_at >= month_start,
            Referral.created_at <= month_end
        ).first()
        
        referral_count = month_referrals.referral_count or 0
        referral_chops = int(month_referrals.referral_chops or 0)
        
        print(f"[/earnings/monthly/{year}/{month}] Referrals created in month: {referral_count}, Chops: {referral_chops}")
        
        # Paid Referrals: ALL referred users whose CURRENT subscription_status is "active"
        # (NOT limited to those referred in this specific month)
        all_referred_user_ids = db.query(Referral.referred_user_id).filter(
            Referral.referrer_id == user_id
        ).subquery()
        
        # Count ALL referred users with active subscriptions
        paid_referral_count = db.query(func.count(User.id)).filter(
            User.id.in_(db.query(all_referred_user_ids.c.referred_user_id)),
            func.lower(User.subscription_status) == "active"
        ).scalar() or 0
        
        print(f"[/earnings/monthly/{year}/{month}] Paid referrals (ALL active, not just from this month): {paid_referral_count}")
        
        # Calculate commission based on actual subscription payments made in this month
        # by ANY referred users (not just those referred this month)
        all_referred_user_ids = db.query(Referral.referred_user_id).filter(
            Referral.referrer_id == user_id
        ).subquery()
        
        # Use explicit select_from to avoid join ambiguity
        month_payments = db.query(
            func.coalesce(func.sum(Subscriptions.amount), 0).label('total_amount')
        ).select_from(Subscriptions).filter(
            Subscriptions.user_id.in_(db.query(all_referred_user_ids.c.referred_user_id)),
            Subscriptions.status == "successful",
            Subscriptions.created_at >= month_start,
            Subscriptions.created_at <= month_end
        ).first()
        
        total_subscription_amount = float(month_payments.total_amount or 0)
        user_commission_rate = CommissionService._get_rate_for_referrer(user_id, db)
        month_commission = round(total_subscription_amount * float(user_commission_rate), 2)
        
        print(f"[/earnings/monthly/{year}/{month}] Commission from payments in month: ${month_commission}")
        
        # Get month name
        month_name = month_start.strftime("%B")
        
        result = {
            "month": month_name,
            "month_number": month,
            "year": year,
            "referral_count": referral_count,
            "paid_referral_count": paid_referral_count,
            "referral_chops": referral_chops,
            "commission": month_commission,
            "revenue": month_commission
        }
        
        print(f"[/earnings/monthly/{year}/{month}] ✅ Complete")
        print(f"[/earnings/monthly/{year}/{month}] RETURNING PAYLOAD: {result}")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] /earnings/monthly/{year}/{month}: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch monthly metrics: {str(e)}")


@router.get("/earnings/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Earnings API",
        "commission_rate": "40%",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# ============= DATABASE INDEXES (run once) =============
"""
Ensure these indexes exist for optimal performance:

CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referrals_created_at ON referrals(created_at);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer_created ON referrals(referrer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_created_at ON subscriptions(created_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status);
"""