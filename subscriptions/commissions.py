
# api/routes/commissions.py
"""
API endpoints for commission and payout management
"""
from fastapi import APIRouter, HTTPException, Depends, status, Request, Header, BackgroundTasks
import logging
logger = logging.getLogger(__name__)
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timezone
from decimal import Decimal

from database.pg_connections import get_db
from database.pg_models import (User, Commission, Payout, PayoutAccount, CommissionResponse, PayoutAccountCreate, PayoutResponse, 
                    PayoutRequest)   
from api.routes.auth.login import get_current_user
from subscriptions.commission_service import CommissionService
from subscriptions.payout_service import PayoutService

router = APIRouter(prefix="/commissions", tags=["commissions"])


def extract_user_id(current_user):
    """Extract user_id from various token formats"""
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                user_id = user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                user_id = user_data.id
            else:
                user_id = user_data
        else:
            user_id = current_user.get("id") or current_user.get("user_id")
    else:
        user_id = current_user.id
    
    return user_id


@router.get("/earnings")
async def get_user_earnings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive earnings data for the authenticated user
    """
    try:
        user_id = extract_user_id(current_user)
        earnings = CommissionService.get_user_earnings(user_id, db)
        
        return {
            "status": "success",
            "data": earnings
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch earnings: {str(e)}"
        )


@router.get("/history", response_model=List[CommissionResponse])
async def get_commission_history(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get commission history for the authenticated user
    """
    try:
        user_id = extract_user_id(current_user)
        
        commissions = db.query(Commission).filter(
            Commission.user_id == user_id
        ).order_by(
            Commission.created_at.desc()
        ).limit(limit).offset(offset).all()
        
        return commissions
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch commission history: {str(e)}"
        )


@router.post("/payout-account")
async def setup_payout_account(
    account_data: PayoutAccountCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Set up or update payout account information
    """
    try:
        user_id = extract_user_id(current_user)
        
        logger.info("[payout-account] POST setup user=%s", user_id)
        logger.info("[payout-account] method=%s", account_data.payment_method)
        
        # Validate payment method
        if account_data.payment_method not in ['stripe', 'flutterwave', 'paypal']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid payment method. Must be 'stripe', 'flutterwave', or 'paypal'"
            )
        
        # Validate required fields based on payment method
        if account_data.payment_method == 'stripe':
            if not account_data.stripe_account_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Stripe account ID is required for Stripe payouts"
                )
        elif account_data.payment_method == 'flutterwave':
            if not all([account_data.bank_name, account_data.account_number, account_data.account_name]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Bank name, account number, and account name are required for Flutterwave payouts"
                )
        elif account_data.payment_method == 'paypal':
            if not account_data.paypal_email:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="PayPal email is required for PayPal payouts"
                )
        
        # Check if account exists
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()
        
        if payout_account:
            # Update existing
            logger.info("[payout-account] updating existing row")
            
            if account_data.stripe_account_id:
                payout_account.stripe_account_id = account_data.stripe_account_id
            if account_data.bank_name:
                payout_account.bank_name = account_data.bank_name
            if account_data.account_number:
                payout_account.account_number = account_data.account_number
            if account_data.account_name:
                payout_account.account_name = account_data.account_name
            if account_data.bank_code:
                payout_account.bank_code = account_data.bank_code
            if account_data.paypal_email:
                payout_account.paypal_email = account_data.paypal_email
            
            payout_account.payment_method = account_data.payment_method
            payout_account.updated_at = datetime.now(timezone.utc)
        else:
            # Create new
            logger.info("[payout-account] creating new row")
            
            payout_account = PayoutAccount(
                user_id=user_id,
                stripe_account_id=account_data.stripe_account_id,
                bank_name=account_data.bank_name,
                account_number=account_data.account_number,
                account_name=account_data.account_name,
                bank_code=account_data.bank_code,
                paypal_email=account_data.paypal_email,
                payment_method=account_data.payment_method
            )
            db.add(payout_account)
        
        db.commit()
        db.refresh(payout_account)

        # Real-time notification so the bell badge increments immediately
        from api.services.notification_service import NotificationService
        method_label = account_data.payment_method.replace('_', ' ').title()
        NotificationService.create_notification(
            db=db,
            user_id=user_id,
            type="payout_account_setup",
            title="✅ Payout Account Configured",
            message=(
                f"Your payout account ({method_label}) has been set up. "
                f"You will now receive referral earnings automatically."
            ),
            link="/dashboard/upgrade",
        )

        logger.info("[payout-account] configured for user=%s method=%s", user_id, account_data.payment_method)
        return {
            "status": "success",
            "message": "Payout account configured successfully",
            "data": {
                "payment_method": payout_account.payment_method,
                "is_verified": payout_account.is_verified,
                "has_stripe": bool(payout_account.stripe_account_id),
                "has_bank_details": bool(payout_account.bank_name and payout_account.account_number),
                "has_paypal": bool(payout_account.paypal_email)
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("[payout-account] POST failed: %s", str(e), exc_info=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set up payout account: {str(e)}"
        )


@router.get("/payout-account")
async def get_payout_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get current payout account information
    """
    try:
        user_id = extract_user_id(current_user)
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found in token")
            
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()
        
        if not payout_account:
            return {
                "status": "not_configured",
                "message": "No payout account configured"
            }
        
        account_num = str(payout_account.account_number) if payout_account.account_number else ""
        has_bank = bool(getattr(payout_account, "bank_name", None) and account_num)
        payload = {
            "payment_method":  getattr(payout_account, "payment_method", "stripe"),
            "is_verified":     bool(getattr(payout_account, "is_verified", False)),
            "has_stripe":      bool(getattr(payout_account, "stripe_account_id", None)),
            "has_bank_details": has_bank,
            "has_paypal":      bool(getattr(payout_account, "paypal_email", None)),
            # Full details for the user's own account view
            "bank_name":       getattr(payout_account, "bank_name", None),
            "bank_code":       getattr(payout_account, "bank_code", None),
            "account_name":    getattr(payout_account, "account_name", None),
            "account_number":  account_num,   # full number — user is viewing their own data
            "account_last_4":  account_num[-4:] if len(account_num) >= 4 else account_num,
            "paypal_email":    getattr(payout_account, "paypal_email", None),
        }
        logger.info(
            "[payout-account] GET user=%s method=%s has_bank=%s bank_name=%s account_name=%s",
            user_id, payload["payment_method"], has_bank,
            payload["bank_name"], payload["account_name"],
        )
        return {"status": "success", "data": payload}

    except Exception as e:
        logger.error("[payout-account] GET failed user=%s: %s", user_id if 'user_id' in dir() else '?', str(e), exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch payout account: {str(e)}"
        )


@router.delete("/payout-account/bank")
async def remove_bank_payout_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove bank/Flutterwave payout account details."""
    try:
        user_id = extract_user_id(current_user)
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        if not payout_account or not (payout_account.bank_name or payout_account.account_number):
            raise HTTPException(status_code=404, detail="No bank account configured")

        payout_account.bank_name = None
        payout_account.account_number = None
        payout_account.account_name = None
        payout_account.bank_code = None

        if payout_account.payment_method in ("flutterwave", None) and not payout_account.stripe_account_id:
            payout_account.payment_method = None
            payout_account.is_verified = False

        payout_account.updated_at = datetime.now(timezone.utc)
        db.commit()

        from api.services.notification_service import NotificationService
        NotificationService.create_notification(
            db=db,
            user_id=user_id,
            type="payout_account_removed",
            title="🏦 Bank Account Removed",
            message="Your bank payout account has been removed. Add a new one to keep receiving referral earnings.",
            link="/dashboard/upgrade",
        )
        logger.info("[payout-account] bank removed for user=%s", user_id)
        return {"status": "success", "message": "Bank account removed successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove bank account: {str(e)}",
        )


@router.post("/request-payout")
async def request_payout(
    payout_data: PayoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Request a payout of earned commissions
    """
    try:
        user_id = extract_user_id(current_user)
        amount = Decimal(str(payout_data.amount))
        
        # Create payout request
        payout = PayoutService.create_payout_request(
            user_id=user_id,
            amount=amount,
            payment_method=payout_data.payment_method,
            db=db
        )
        
        return {
            "status": "success",
            "message": "Payout request created successfully",
            "data": {
                "payout_id": payout.id,
                "amount": float(payout.amount),
                "status": payout.status,
                "payment_method": payout.payment_method
            }
        }
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create payout request: {str(e)}"
        )


@router.get("/payouts", response_model=List[PayoutResponse])
async def get_payout_history(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get payout history for the authenticated user
    """
    try:
        user_id = extract_user_id(current_user)
        
        payouts = db.query(Payout).filter(
            Payout.user_id == user_id
        ).order_by(
            Payout.requested_at.desc()
        ).limit(limit).offset(offset).all()
        
        return payouts
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch payout history: {str(e)}"
        )

# ============= ADMIN ENDPOINTS =============

@router.post("/admin/approve/{commission_id}")
async def admin_approve_commission(
    commission_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin endpoint to approve a commission (requires admin role)
    """
    # Extract user
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                user = db.query(User).filter(User.id == user_data.get("id")).first()
            elif hasattr(user_data, 'id'):
                user = user_data
            else:
                user = db.query(User).filter(User.id == user_data).first()
        else:
            user = db.query(User).filter(User.id == current_user.get("id")).first()
    else:
        user = current_user
    
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    try:
        commission = CommissionService.approve_commission(commission_id, db)
        
        return {
            "status": "success",
            "message": "Commission approved",
            "data": {
                "commission_id": commission.id,
                "amount": float(commission.amount),
                "status": commission.status
            }
        }
        

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/admin/process-payout/{payout_id}")
async def admin_process_payout(
    payout_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin endpoint to process a pending payout
    """
    # Check admin
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                user = db.query(User).filter(User.id == user_data.get("id")).first()
            elif hasattr(user_data, 'id'):
                user = user_data
            else:
                user = db.query(User).filter(User.id == user_data).first()
        else:
            user = db.query(User).filter(User.id == current_user.get("id")).first()
    else:
        user = current_user
    
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    try:
        payout = db.query(Payout).filter(Payout.id == payout_id).first()
        
        if not payout:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Payout not found"
            )
        
        if payout.status != 'pending':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Payout is not pending (status: {payout.status})"
            )
        
        # Process based on payment method
        if payout.payment_method == 'stripe':
            result = PayoutService.process_stripe_payout(payout, background_tasks, db)
        elif payout.payment_method == 'flutterwave':
            result = PayoutService.process_flutterwave_payout(payout, db)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported payment method: {payout.payment_method}"
            )
        
        return {
            "status": "success",
            "message": "Payout processed",
            "data": result
        }
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process payout: {str(e)}"
        )


@router.post("/admin/auto-approve")
async def admin_auto_approve_commissions( days_old: int = 0, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Auto-approve commissions that are X days old
    """
    # Check admin
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                user = db.query(User).filter(User.id == user_data.get("id")).first()
            elif hasattr(user_data, 'id'):
                user = user_data
            else:
                user = db.query(User).filter(User.id == user_data).first()
        else:
            user = db.query(User).filter(User.id == current_user.get("id")).first()
    else:
        user = current_user
    
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    
    try:
        count = CommissionService.auto_approve_commissions(db, days_old)
        
        return {
            "status": "success",
            "message": f"Auto-approved {count} commissions",
            "count": count
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-approve: {str(e)}"
        )

