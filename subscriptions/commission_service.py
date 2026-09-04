
from sqlalchemy.orm import Session
from sqlalchemy import func
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from database.pg_models import Commission, Referral, CommissionSummary, User, NotificationType
from api.services.notification_service import NotificationService
import logging

logger = logging.getLogger(__name__)

COMMISSION_RATE_STANDARD = Decimal("0.40")   # 40% — shown on screen, paid to subscribed regular users
COMMISSION_RATE_PARTNER  = Decimal("0.50")   # 50% — paid internally to partners/staff (screen still shows 40%)
COMMISSION_RATE_FREE     = Decimal("0.15")   # 15% — paid to a referrer who is on the free plan themselves

# Keep legacy alias so any other callsite referencing COMMISSION_RATE still works
COMMISSION_RATE = COMMISSION_RATE_STANDARD


class CommissionService:

    @staticmethod
    def _get_rate_for_referrer(referrer_id: int, db: Session) -> Decimal:
        """Return the actual payout rate for a referrer.
        Partners/staff earn 50% regardless of their own subscription (the
        displayed rate on the earnings dashboard still shows 40% for them,
        same as before). Otherwise the rate depends on the referrer's own
        subscription: an active/paying referrer earns the standard 40%; a
        referrer on the free plan earns 15%.
        """
        referrer = db.query(User).filter(User.id == referrer_id).first()
        if not referrer:
            return COMMISSION_RATE_STANDARD
        if getattr(referrer, "is_partner", False):
            return COMMISSION_RATE_PARTNER
        if (referrer.subscription_status or "").lower() == "active":
            return COMMISSION_RATE_STANDARD
        return COMMISSION_RATE_FREE

    @staticmethod
    def calculate_commission(subscription, db: Session, already_settled: bool = False):
        """
        Calculate and create a Commission record when a referred user makes
        a payment. Regular users receive 40% of the subscription amount.
        Partners/staff receive 50% (screen always shows 40%).

        already_settled=True means the actual charge this Commission is
        for was placed with a Flutterwave split (see flutterwave_split.py)
        — the referrer's share already landed in their own account at
        payment time, so this row is a read-only history/earnings-page
        record, not something owed. It's created with status='auto_settled'
        and paid_at set immediately, skipping the pending -> approved ->
        payout pipeline entirely (no "Request Payout" action applies to it).
        already_settled=False (the default, and the only behaviour that
        existed before Flutterwave splits) means the referrer has no
        verified payout account to split to yet — this Commission starts
        'pending' and is only ever paid via the existing manual
        approve/payout flow once they add one.
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

            # Determine rate: partners get 50%, everyone else 40%
            actual_rate = CommissionService._get_rate_for_referrer(referral.referrer_id, db)

            # Calculate commission amount
            original_amount = Decimal(str(subscription.amount))
            commission_amount = original_amount * actual_rate

            # Create commission — always store the actual rate used
            now = datetime.now(timezone.utc)
            commission = Commission(
                user_id=referral.referrer_id,
                referred_user_id=subscription.user_id,
                subscription_id=subscription.id,
                amount=commission_amount,
                original_amount=original_amount,
                currency=subscription.currency,
                commission_rate=actual_rate * 100,
                status='auto_settled' if already_settled else 'pending',
                created_at=now,
                paid_at=now if already_settled else None,
            )

            db.add(commission)
            db.flush()

            # Automated payout, no admin click, the moment the money is
            # earned — per explicit direction: "the admin does not have to
            # click to send the user the money... the moment the money
            # lands." already_settled (Flutterwave subaccount split) already
            # covers same-currency NGN charges to a referrer with a
            # Flutterwave subaccount — this covers everything else a
            # subaccount split structurally cannot: a Stripe (USD/GBP)
            # commission owed to a referrer whose only real payout method is
            # Flutterwave/NGN, converted via a live FX rate and sent as a
            # plain transfer, right here, synchronously.
            settled_now = already_settled
            payout_id = None
            if not already_settled:
                payout_id = CommissionService._attempt_immediate_payout(commission, db)
                if payout_id:
                    commission.status = 'auto_settled'
                    commission.paid_at = now
                    commission.payout_id = payout_id
                    settled_now = True
                    db.flush()

            # Update monthly summary — a settled commission (subaccount
            # split OR the immediate-payout path above) counts straight into
            # paid_commissions, not pending_commissions, since nothing is
            # owed on it (see _update_monthly_summary).
            CommissionService._update_monthly_summary(
                referral.referrer_id,
                commission_amount,
                db,
                already_settled=settled_now,
            )

            db.flush()
            # db.refresh(commission) # We can use flush + already in session

            logger.info(
                f"[BUILDER BONUS] created | subscription={subscription.id} referred_user={subscription.user_id} "
                f"referrer={referral.referrer_id} amount={commission_amount} {subscription.currency} "
                f"rate={actual_rate * 100}% status={commission.status} settled_now={settled_now} "
                f"commission_id={commission.id} payout_id={payout_id}"
            )

            # Notify referrer about builder bonus in real time
            cur = getattr(subscription, "currency", "USD") or "USD"
            bonus_message = (
                f"You earned a {cur} {commission_amount:.2f} builder bonus from a "
                f"referral's subscription payment — sent straight to your account."
                if settled_now else
                f"You earned a {cur} {commission_amount:.2f} builder bonus "
                f"from a referral's subscription payment."
            )
            logger.info(f"[BUILDER BONUS] notification sent to user={referral.referrer_id} | \"{bonus_message}\"")
            NotificationService.create_notification(
                db=db,
                user_id=referral.referrer_id,
                type=NotificationType.COMMISSION_EARNED.value,
                title="🎉 Builder Bonus Earned!",
                message=bonus_message,
                link="/dashboard/earnings",
            )

            return commission

        except Exception as e:
            logger.error(f"❌ Commission creation error: {str(e)}")
            db.rollback()
            raise

    @staticmethod
    def _attempt_immediate_payout(commission: "Commission", db: Session) -> "int | None":
        """Pay a freshly-created commission out immediately, no admin
        approval step, when the referrer's only usable payout method is a
        verified Flutterwave bank account — covering exactly the case a
        Flutterwave subaccount split structurally cannot: a commission in a
        currency the referrer can't be split into at charge time (a Stripe
        USD/GBP charge, when the referrer only has NGN banking), converted
        via a live FX rate and sent as a plain transfer instead.

        Returns the new Payout's id on a confirmed successful transfer,
        None on anything else (no Flutterwave account configured yet, rate
        lookup failed, transfer failed) — the commission is left exactly as
        the caller already has it (status='pending') for the existing
        manual approve/payout flow to pick up later, same as before this
        existed. Never raises: a payout failure here must never take down
        the commission record it's trying to settle, let alone the
        subscription payment that's already been charged for real.
        """
        logger.info(
            f"[BUILDER BONUS] immediate payout check | commission={commission.id} "
            f"referrer={commission.user_id} amount={commission.amount} {commission.currency}"
        )
        try:
            from database.pg_models import Payout, PayoutAccount
            from subscriptions.flutterwave_split import get_flutterwave_fx_rate
            from subscriptions.payout_service import PayoutService

            payout_account = (
                db.query(PayoutAccount)
                .filter(
                    PayoutAccount.user_id == commission.user_id,
                    PayoutAccount.payment_method == "flutterwave",
                    PayoutAccount.bank_name.isnot(None),
                    PayoutAccount.account_number.isnot(None),
                    PayoutAccount.bank_code.isnot(None),
                )
                .order_by(PayoutAccount.updated_at.desc().nullslast(), PayoutAccount.created_at.desc())
                .first()
            )
            if not payout_account:
                logger.info(
                    f"[BUILDER BONUS] immediate payout skipped | commission={commission.id} "
                    f"referrer={commission.user_id} has no verified Flutterwave bank account on file — "
                    f"leaving pending for manual payout"
                )
                return None

            logger.info(
                f"[BUILDER BONUS] immediate payout method found | commission={commission.id} "
                f"payout_account={payout_account.id} bank={payout_account.bank_name} "
                f"account=***{payout_account.account_number[-4:]}"
            )

            commission_currency = (commission.currency or "USD").upper()
            commission_amount = Decimal(str(commission.amount))
            fx_rate = None

            if commission_currency == "NGN":
                payout_amount = commission_amount
                logger.info(f"[BUILDER BONUS] no conversion needed | commission={commission.id} already NGN")
            else:
                logger.info(f"[BUILDER BONUS] fetching FX rate | commission={commission.id} {commission_currency}->NGN")
                fx_rate = get_flutterwave_fx_rate(commission_currency, "NGN")
                if fx_rate is None:
                    logger.warning(
                        f"[BUILDER BONUS] Could not fetch {commission_currency}->NGN rate for "
                        f"commission {commission.id} — leaving pending for manual payout"
                    )
                    return None
                payout_amount = (commission_amount * fx_rate).quantize(Decimal("0.01"))
                logger.info(
                    f"[BUILDER BONUS] FX rate fetched | commission={commission.id} rate={fx_rate} "
                    f"{commission_amount} {commission_currency} -> {payout_amount} NGN"
                )

            payout = Payout(
                user_id=commission.user_id,
                amount=payout_amount,
                currency="NGN",
                status="pending",
                provider="flutterwave",
                payment_method="flutterwave",
                recipient_name=payout_account.account_name,
                account_details=(
                    f"Bank: {payout_account.bank_name}, "
                    f"Account: ****{payout_account.account_number[-4:] if payout_account.account_number else 'N/A'}"
                ),
                original_currency=commission_currency if commission_currency != "NGN" else None,
                original_amount=commission_amount if commission_currency != "NGN" else None,
                fx_rate=fx_rate,
                requested_at=datetime.now(timezone.utc),
            )
            db.add(payout)
            db.flush()

            PayoutService.process_flutterwave_payout(payout, db)
            # process_flutterwave_payout sets payout.status='processing' on a
            # confirmed-accepted transfer request and commits internally; any
            # failure raises, caught below. Flutterwave transfers are async
            # (webhook confirms final completion), so 'processing' here means
            # "successfully handed to Flutterwave," which is the correct
            # point to mark this commission auto_settled — same trust
            # boundary the existing admin-approved payout flow already uses.
            logger.info(
                f"[BUILDER BONUS] immediate payout initiated | commission={commission.id} "
                f"payout={payout.id} amount={payout_amount} NGN "
                f"(from {commission_amount} {commission_currency}"
                + (f" @ rate {fx_rate}" if fx_rate else "") + ")"
            )
            return payout.id

        except Exception as e:
            logger.error(f"[BUILDER BONUS] Immediate payout attempt failed for commission {commission.id}: {e}", exc_info=True)
            return None

    @staticmethod
    def _update_monthly_summary(user_id: int, amount: Decimal, db: Session, already_settled: bool = False):
        """Update or create monthly commission summary.

        already_settled routes `amount` into paid_commissions instead of
        pending_commissions — a Flutterwave-split commission has nothing
        owed on it, so it must not appear as payout-requestable balance.
        """
        now = datetime.now(timezone.utc)

        summary = db.query(CommissionSummary).filter(
            CommissionSummary.user_id == user_id,
            CommissionSummary.year == now.year,
            CommissionSummary.month == now.month
        ).first()

        if summary:
            summary.total_commissions += amount
            if already_settled:
                summary.paid_commissions += amount
            else:
                summary.pending_commissions += amount
            summary.commission_count += 1
            summary.updated_at = now
        else:
            summary = CommissionSummary(
                user_id=user_id,
                year=now.year,
                month=now.month,
                total_commissions=amount,
                pending_commissions=Decimal("0.00") if already_settled else amount,
                paid_commissions=amount if already_settled else Decimal("0.00"),
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