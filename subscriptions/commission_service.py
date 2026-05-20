
from sqlalchemy.orm import Session
from sqlalchemy import func
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from database.pg_models import Commission, Referral, CommissionSummary, User, NotificationType
from api.services.notification_service import NotificationService
import logging

logger = logging.getLogger(__name__)

COMMISSION_RATE = Decimal("0.50")  # 50%

class CommissionService:
    
    @staticmethod
    def calculate_commission(subscription, db: Session):
        """
        Calculate and create commission when a referred user makes a payment
        """
        try:
            # Check if user was referred
            referral = db.query(Referral).filter(
                Referral.referred_user_id == subscription.user_id
            ).first()
            
            if not referral:
                logger.info(f"No referral found for user {subscription.user_id}")
                return None
            
            # Check if commission already exists
            existing = db.query(Commission).filter(
                Commission.subscription_id == subscription.id
            ).first()
            
            if existing:
                logger.info(f"Commission already exists for subscription {subscription.id}")
                return existing
            
            # Calculate commission amount
            original_amount = Decimal(str(subscription.amount))
            commission_amount = original_amount * COMMISSION_RATE
            
            # Create commission
            commission = Commission(
                user_id=referral.referrer_id,
                referred_user_id=subscription.user_id,
                subscription_id=subscription.id,
                amount=commission_amount,
                original_amount=original_amount,
                currency=subscription.currency,
                commission_rate=COMMISSION_RATE * 100,
                status='pending',  # Starts as pending
                created_at=datetime.now(timezone.utc)
            )
            
            db.add(commission)
            db.flush()
            
            # Update monthly summary
            CommissionService._update_monthly_summary(
                referral.referrer_id, 
                commission_amount, 
                db
            )
            
            db.flush()
            # db.refresh(commission) # We can use flush + already in session
            
            logger.info(
                f"✅ Commission created: ${commission_amount} for user {referral.referrer_id}"
            )
            
            # Notify referrer about builder bonus in real time
            cur = getattr(subscription, "currency", "USD") or "USD"
            NotificationService.create_notification(
                db=db,
                user_id=referral.referrer_id,
                type=NotificationType.COMMISSION_EARNED.value,
                title="🎉 Builder Bonus Earned!",
                message=(
                    f"You earned a {cur} {commission_amount:.2f} builder bonus "
                    f"from a referral's subscription payment."
                ),
                link="/dashboard/earnings",
            )
            
            return commission
            
        except Exception as e:
            logger.error(f"❌ Commission creation error: {str(e)}")
            db.rollback()
            raise
    
    @staticmethod
    def _update_monthly_summary(user_id: int, amount: Decimal, db: Session):
        """Update or create monthly commission summary"""
        now = datetime.now(timezone.utc)
        
        summary = db.query(CommissionSummary).filter(
            CommissionSummary.user_id == user_id,
            CommissionSummary.year == now.year,
            CommissionSummary.month == now.month
        ).first()
        
        if summary:
            summary.total_commissions += amount
            summary.pending_commissions += amount
            summary.commission_count += 1
            summary.updated_at = now
        else:
            summary = CommissionSummary(
                user_id=user_id,
                year=now.year,
                month=now.month,
                total_commissions=amount,
                pending_commissions=amount,
                paid_commissions=Decimal("0.00"),
                commission_count=1,
                currency='USD'
            )
            db.add(summary)
    
    @staticmethod
    def approve_commission(commission_id: int, db: Session):
        """Approve a pending commission"""
        commission = db.query(Commission).filter(
            Commission.id == commission_id
        ).first()
        
        if not commission:
            raise ValueError(f"Commission {commission_id} not found")
        
        if commission.status != 'pending':
            raise ValueError(f"Commission is not pending (status: {commission.status})")
        
        commission.status = 'approved'
        commission.approved_at = datetime.now(timezone.utc)
        
        # Update summary
        now = datetime.now(timezone.utc)
        summary = db.query(CommissionSummary).filter(
            CommissionSummary.user_id == commission.user_id,
            CommissionSummary.year == now.year,
            CommissionSummary.month == now.month
        ).first()
        
        if summary:
            summary.pending_commissions -= commission.amount
            # Note: Not moved to paid yet, that happens on actual payout
        
        db.flush()
        # db.refresh(commission)
        
        logger.info(f"✅ Commission {commission_id} approved")
        return commission
    
    @staticmethod
    def auto_approve_commissions(db: Session, days_old: int = 0):
        """
        Auto-approve commissions that are X days old
        Useful for automated approval after verification period
        """
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_old)
        
        commissions = db.query(Commission).filter(
            Commission.status == 'pending',
            Commission.created_at <= cutoff_date
        ).all()
        
        count = 0
        for commission in commissions:
            commission.status = 'approved'
            commission.approved_at = datetime.now(timezone.utc)
            count += 1
        
        db.flush()
        
        logger.info(f"✅ Auto-approved {count} commissions")
        return count
    
    @staticmethod
    def get_user_earnings(user_id: int, db: Session):
        """Get comprehensive earnings data for a user"""
        
        # Total commissions
        totals = db.query(
            func.coalesce(func.sum(Commission.amount), 0).label('total'),
            func.count(Commission.id).label('count')
        ).filter(
            Commission.user_id == user_id
        ).first()
        
        # Approved but unpaid (available for payout)
        available = db.query(
            func.coalesce(func.sum(Commission.amount), 0)
        ).filter(
            Commission.user_id == user_id,
            Commission.status == 'approved',
            Commission.payout_id.is_(None)
        ).scalar() or Decimal("0.00")
        
        # Pending approval
        pending = db.query(
            func.coalesce(func.sum(Commission.amount), 0)
        ).filter(
            Commission.user_id == user_id,
            Commission.status == 'pending'
        ).scalar() or Decimal("0.00")
        
        # Already paid
        paid = db.query(
            func.coalesce(func.sum(Commission.amount), 0)
        ).filter(
            Commission.user_id == user_id,
            Commission.status == 'paid'
        ).scalar() or Decimal("0.00")
        
        return {
            "total_earned": float(totals.total or 0),
            "commission_count": totals.count or 0,
            "available_for_payout": float(available),
            "pending_approval": float(pending),
            "already_paid": float(paid),
            "currency": "USD"
        }