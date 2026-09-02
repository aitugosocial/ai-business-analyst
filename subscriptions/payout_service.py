
"""
Payout service for handling commission payouts via Stripe and Flutterwave
"""
import os
import stripe
import requests
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import logging
import json

from sqlalchemy import func
from sqlalchemy.orm import Session
from database.pg_models import (
    User, Commission, Payout, PayoutAccount, 
    CommissionSummary, NotificationType
)
from api.services.notification_service import NotificationService
from fastapi import BackgroundTasks
from emailing import email_service

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Flutterwave config (using NEXT_PUBLIC_ prefix to match .env)
FLUTTERWAVE_SECRET_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY")
FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"


class PayoutService:
    """
    Handles payouts to users via Stripe Connect or Flutterwave Transfers
    """
    
    MIN_PAYOUT_AMOUNT = Decimal("5.00")  # Minimum $10 for payout
    
    @staticmethod
    def create_payout_request(user_id: int, amount: Decimal, payment_method: str,  # 'stripe' or 'flutterwave'   
        db: Session) -> Payout:
        """
        Create a payout request
        
        Args:
            user_id: User requesting payout
            amount: Amount to payout
            payment_method: 'stripe' or 'flutterwave'
            db: Database session
        
        Returns:
            Payout object
        """
        # Validate user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User {user_id} not found")
        
        # Check available balance
        available = db.query(
            func.sum(Commission.amount)
        ).filter(
            Commission.user_id == user_id,
            Commission.status == 'approved',
            Commission.payout_id.is_(None)
        ).scalar() or Decimal("0.00")
        
        if available < amount:
            raise ValueError(
                f"Insufficient balance. Available: ${available}, Requested: ${amount}"
            )
        
        if amount < PayoutService.MIN_PAYOUT_AMOUNT:
            raise ValueError(
                f"Minimum payout amount is ${PayoutService.MIN_PAYOUT_AMOUNT}"
            )
        
        # Check payout account exists
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()
        
        if not payout_account:
            raise ValueError("No payout account configured. Please set up your payout method.")
        
        # Validate payment method
        if payment_method == 'stripe':
            if not payout_account.stripe_account_id:
                raise ValueError("Stripe account not connected")
        elif payment_method == 'flutterwave':
            if not payout_account.bank_name or not payout_account.account_number:
                raise ValueError("Bank details not configured")
        else:
            raise ValueError(f"Invalid payment method: {payment_method}")
        
        # Create payout record
        payout = Payout(
            user_id=user_id,
            amount=amount,
            currency='USD',  # Default to USD
            payment_method=payment_method,
            status='pending',
            recipient_email=user.email,
            recipient_name=user.name,
            requested_at=datetime.now(timezone.utc)
        )
        
        db.add(payout)
        db.flush()
        
        # Link commissions to this payout
        commissions = db.query(Commission).filter(
            Commission.user_id == user_id,
            Commission.status == 'approved',
            Commission.payout_id.is_(None)
        ).order_by(Commission.created_at).all()
        
        total_linked = Decimal("0.00")
        for commission in commissions:
            if total_linked + commission.amount <= amount:
                commission.payout_id = payout.id
                total_linked += commission.amount
            if total_linked >= amount:
                break
        
        db.commit()
        db.refresh(payout)
        
        logger.info(f"Payout request created: {payout.id} for user {user_id}")
        return payout
    

    @staticmethod
    def process_stripe_payout(payout: Payout, background_tasks: BackgroundTasks, db: Session) -> Dict[str, Any]:
        """
        Process payout via Stripe Connect
        
        NOTE: This requires Stripe Connect to be set up.
        For testing, you can use Stripe's test mode.
        """
        try:
            payout_account = db.query(PayoutAccount).filter(
                PayoutAccount.user_id == payout.user_id
            ).first()
            
            if not payout_account or not payout_account.stripe_account_id:
                raise ValueError("Stripe account not configured")
            
            # Convert amount to cents
            amount_cents = int(payout.amount * 100)
            
            # Create Stripe transfer
            # Note: This requires the connected account to be set up
            transfer = stripe.Transfer.create(
                amount=amount_cents,
                currency=payout.currency.lower(),
                destination=payout_account.stripe_account_id,
                description=f"Commission payout for {payout.recipient_name}",
                metadata={
                    "stripe_connect_payout_id": str(payout.id),
                    "user_id": str(payout.user_id)
                }
            )
            # Create Stripe payout
            payment = stripe.Payout.create(
                        amount=amount_cents,
                        currency=payout.currency.lower(),
                        stripe_account=payout_account.stripe_account_id,
                        metadata={
                            "stripe_connect_payout_id": str(payout.id),
                            "user_id": str(payout.user_id)
                        }
            )

            # Update payout record to completed immediately (synchronous flow)
            payout.status = 'completed'
            payout.provider_transfer_id = transfer.id
            payout.provider_payout_id = payment.id
            payout.provider_response = json.dumps({
                                        "transfer": transfer.to_dict(),
                                        "payout": payment.to_dict(),
            })
            payout.processed_at = datetime.now(timezone.utc)
            payout.completed_at = datetime.now(timezone.utc)

            # Update commissions linked to this payout
            commissions = db.query(Commission).filter(
                Commission.payout_id == payout.id
            ).all()
            
            for commission in commissions:
                commission.status = 'paid'
                commission.paid_at = datetime.now(timezone.utc)
            
            # Update user's commission summary
            PayoutService._update_summary_on_payout(payout, db)

            db.flush()

            logger.info(
                f"Stripe payout completed synchronously | payout={payout.id} "
                f"transfer={transfer.id} payment={payment.id}"
            )
            
            background_tasks.add_task(
                email_service.send_payout_success_email,
                payout.user_id,
                payout.amount,
                payout.currency,
                payout.id,
                payout.processed_at
            )
            
            return {
                "status": "success",
                "payout_id": payout.id,
                "stripe_transfer_id": transfer.id,
                "stripe_payout_id": payment.id,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payout initiation failed: {str(e)}")
            payout.status = "failed"
            payout.failure_reason = str(e)
            db.flush()
            raise

        
    

    @staticmethod
    def process_flutterwave_payout(payout: Payout, db: Session) -> Dict[str, Any]:
        """
        Process payout via Flutterwave Transfer API
        """
        logger.info(
            f"[FLW payout] START | payout={payout.id} user={payout.user_id} "
            f"amount={payout.amount} {payout.currency}"
            + (f" (converted from {payout.original_amount} {payout.original_currency} @ rate {payout.fx_rate})"
               if payout.original_currency else "")
        )
        try:
            payout_account = db.query(PayoutAccount).filter(
                PayoutAccount.user_id == payout.user_id
            ).first()

            if not payout_account:
                raise ValueError("Payout account not configured")

            if not payout_account.bank_name or not payout_account.account_number:
                raise ValueError("Bank details not configured")

            logger.info(
                f"[FLW payout] destination | payout={payout.id} bank={payout_account.bank_name} "
                f"bank_code={payout_account.bank_code} account=***{payout_account.account_number[-4:]} "
                f"name={payout_account.account_name}"
            )

            # Prepare transfer payload. No debit_currency override: Flutterwave
            # debits the balance matching the transfer `currency` by default,
            # which is correct here since payout.currency is always set to
            # NGN for a Flutterwave bank transfer (commission_service.py
            # converts any non-NGN commission to NGN before creating this
            # Payout row). A hardcoded "debit_currency": "USD" here
            # previously tried to debit a USD balance Lavoo doesn't actually
            # hold in Flutterwave (dollar/pound revenue arrives via Stripe,
            # not Flutterwave) for what should just be a plain NGN transfer
            # from Lavoo's existing, well-funded NGN balance — the direct
            # cause of a reported payout never arriving.
            payload = {
                "account_bank": payout_account.bank_code or payout_account.bank_name,
                "account_number": payout_account.account_number,
                "amount": float(payout.amount),
                "currency": payout.currency,
                "narration": f"Commission payout - {payout.id}",
                "reference": f"PAYOUT-{payout.id}-{int(datetime.now(timezone.utc).timestamp())}",
                "callback_url": f"{os.getenv('BASE_URL')}/api/payouts/flutterwave/callback",
                "beneficiary_name": payout_account.account_name or payout.recipient_name
            }
            
            headers = {
                "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
                "Content-Type": "application/json"
            }

            logger.info(
                f"[FLW payout] request | payout={payout.id} POST {FLUTTERWAVE_BASE_URL}/transfers "
                f"account_bank={payload['account_bank']} amount={payload['amount']} currency={payload['currency']} "
                f"reference={payload['reference']}"
            )

            # Make transfer request
            response = requests.post(
                f"{FLUTTERWAVE_BASE_URL}/transfers",
                json=payload,
                headers=headers
            )

            logger.info(
                f"[FLW payout] response | payout={payout.id} status={response.status_code} "
                f"body={response.text[:500]}"
            )

            if response.status_code != 200:
                raise ValueError(f"Flutterwave API error: {response.text}")

            data = response.json()

            if data.get("status") != "success":
                raise ValueError(f"Transfer failed: {data.get('message')}")

            transfer_data = data.get("data", {})

            # Update payout record
            payout.status = 'processing'  # Flutterwave transfers are async
            payout.provider_payout_id = str(transfer_data.get("id"))
            payout.provider_response = json.dumps(data)
            payout.processed_at = datetime.now(timezone.utc)

            db.commit()
            db.refresh(payout)

            logger.info(
                f"[FLW payout] SUCCESS | payout={payout.id} transfer_id={transfer_data.get('id')} "
                f"amount={payout.amount} {payout.currency} status={payout.status} — "
                f"Flutterwave transfers are async, watch for the /flutterwave/callback webhook "
                f"to confirm final completion vs. failure"
            )

            return {
                "status": "processing",
                "payout_id": payout.id,
                "transfer_id": transfer_data.get("id"),
                "amount": float(payout.amount),
                "message": "Payout is being processed"
            }

        except Exception as e:
            # Broadened from requests.RequestException only: a non-200 or a
            # non-"success" Flutterwave response (by far the most likely
            # real-world failure — bad account details, insufficient
            # balance, unsupported bank) raises plain ValueError above,
            # which this previously did NOT catch at all — the payout row
            # was left stuck at status='pending' forever with no
            # failure_reason recorded, indistinguishable from "still in
            # progress." Also removed two references to payout.retry_count
            # and payout.failed_at, neither of which exist as columns on
            # Payout — hitting this block would itself raise AttributeError
            # before even reaching db.commit(), so a genuine transfer
            # failure was silently swallowed by a second, hidden crash.
            db.rollback()
            payout.status = 'failed'
            payout.failure_reason = str(e)
            db.commit()

            logger.error(f"[FLW payout] FAILED | payout={payout.id} error={e}", exc_info=True)
            raise ValueError(f"Payout failed: {str(e)}")


    @staticmethod
    def complete_flutterwave_payout( payout_id: int, background_tasks: BackgroundTasks, transfer_status: str, db: Session) -> None:
        """
        Complete Flutterwave payout after webhook confirmation
        """
        payout = db.query(Payout).filter(Payout.id == payout_id).first()
        
        if not payout:
            logger.error(f"Payout {payout_id} not found")
            return
        
        if transfer_status == "successful":
            payout.status = 'completed'
            payout.completed_at = datetime.now(timezone.utc)
            
            # Update commissions
            commissions = db.query(Commission).filter(
                Commission.payout_id == payout.id
            ).all()
            
            for commission in commissions:
                commission.status = 'paid'
                commission.paid_at = datetime.now(timezone.utc)
            
            # Update summary
            PayoutService._update_summary_on_payout(payout, db)
            background_tasks.add_task(
                email_service.send_payout_success_email,
                payout.user_id,
                payout.amount,
                payout.currency,
                payout.id,
                payout.processed_at
            )
        elif transfer_status == "failed":
            payout.status = 'failed'
            payout.failed_at = datetime.now(timezone.utc)
            
            # Revert commissions to 'pending' so they can be paid again
            commissions = db.query(Commission).filter(
                Commission.payout_id == payout.id
            ).all()
            
            for commission in commissions:
                commission.payout_id = None
                commission.status = 'pending'  # Revert to pending for retry
                commission.approved_at = None
        
        db.commit()
        logger.info(f"Flutterwave payout {payout_id} marked as {transfer_status}")
    

    @staticmethod
    def complete_stripe_payout(payout_id: int, background_tasks: BackgroundTasks, status: str, db: Session) -> None:
        """
        Complete Stripe payout (simulated or via potential webhook)
        """
        payout = db.query(Payout).filter(Payout.id == payout_id).first()
        
        if not payout:
            logger.error(f"Payout {payout_id} not found")
            return
        
        if status == "paid":
            payout.status = 'completed'
            payout.completed_at = datetime.now(timezone.utc)
            
            # Update commissions
            commissions = db.query(Commission).filter(
                Commission.payout_id == payout.id
            ).all()
            
            for commission in commissions:
                commission.status = 'paid'
                commission.paid_at = datetime.now(timezone.utc)
            
            # Update summary
            PayoutService._update_summary_on_payout(payout, db)
            
            background_tasks.add_task(
                email_service.send_payout_success_email,
                payout.user_id,
                payout.amount,
                payout.currency,
                payout.id,
                payout.processed_at
            )
        elif status == "failed":
            payout.status = 'failed'
            payout.failed_at = datetime.now(timezone.utc)
            
            # Revert commissions
            commissions = db.query(Commission).filter(
                Commission.payout_id == payout.id
            ).all()
            
            for commission in commissions:
                commission.payout_id = None
                commission.status = 'approved' # Keep as approved so they can be re-payout
        
        db.commit()
        logger.info(f"Stripe payout {payout_id} marked as {status}")
    

    @staticmethod
    def _update_summary_on_payout(payout: Payout, db: Session) -> None:
        """
        Update commission summary when payout is completed
        """
        now = datetime.now(timezone.utc)
        
        # Get all months affected by the commissions in this payout
        commissions = db.query(Commission).filter(
            Commission.payout_id == payout.id
        ).all()
        
        # Group by month
        monthly_amounts = {}
        for commission in commissions:
            year = commission.created_at.year
            month = commission.created_at.month
            key = (year, month)
            
            if key not in monthly_amounts:
                monthly_amounts[key] = Decimal("0.00")
            monthly_amounts[key] += commission.amount
        
        # Update each affected month
        for (year, month), amount in monthly_amounts.items():
            summary = db.query(CommissionSummary).filter(
                CommissionSummary.user_id == payout.user_id,
                CommissionSummary.year == year,
                CommissionSummary.month == month
            ).first()
            
            if summary:
                summary.paid_commissions += amount
                summary.pending_commissions -= amount
                summary.updated_at = now

    @staticmethod
    def reverse_payout(payout_id: int, failure_reason: str, db: Session) -> None:
        """
        Handle a payout that was previously marked as completed but has now failed.
        """
        payout = db.query(Payout).filter(Payout.id == payout_id).first()
        if not payout:
            return

        if payout.status == "failed":
            return

        payout.status = "failed"
        payout.failure_reason = failure_reason or "Funds returned/Reversed"
        payout.failed_at = datetime.now(timezone.utc)

        commissions = db.query(Commission).filter(
            Commission.payout_id == payout.id
        ).all()

        for commission in commissions:
            commission.payout_id = None
            commission.status = 'pending'
            commission.paid_at = None

        PayoutService._reverse_summary_on_payout(payout, db)
        db.commit()

    @staticmethod
    def _reverse_summary_on_payout(payout: Payout, db: Session) -> None:
        now = datetime.now(timezone.utc)
        commissions = db.query(Commission).filter(
            Commission.payout_id == payout.id
        ).all()

        monthly_amounts = {}
        for commission in commissions:
            year = commission.created_at.year
            month = commission.created_at.month
            key = (year, month)
            if key not in monthly_amounts:
                monthly_amounts[key] = Decimal("0.00")
            monthly_amounts[key] += commission.amount

        for (year, month), amount in monthly_amounts.items():
            summary = db.query(CommissionSummary).filter(
                CommissionSummary.user_id == payout.user_id,
                CommissionSummary.year == year,
                CommissionSummary.month == month
            ).first()
            if summary:
                summary.paid_commissions -= amount
                summary.pending_commissions += amount
                summary.updated_at = now