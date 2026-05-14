# subscriptions/flutterwave.py
from __future__ import annotations
from typing import Annotated
from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel
import requests
import logging
from datetime import datetime, timedelta, timezone

from decimal import Decimal, InvalidOperation

# import the database
from database.pg_connections import get_db
from database.pg_models import User, Subscriptions
from api.routes.auth.login import get_current_user

#import the email system
from fastapi import BackgroundTasks
from emailing.email_service import email_service

import os
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
logger.info("Flutterwave module loaded.")


# Change prefix to match your frontend URL
router = APIRouter(prefix="/api/payments", tags=["payments"])

# Flutterwave configuration
FLUTTERWAVE_SECRET_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY")
FLUTTERWAVE_PUBLIC_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_PUBLIC_KEY")
FLUTTERWAVE_ENCRYPTION_KEY = os.getenv("FLUTTERWAVE_ENCRYPTION_KEY")
FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"

# Duration in days per plan — used when plan_type is passed explicitly
PLAN_DURATION: dict[str, int] = {
    "monthly":   30,
    "quarterly": 90,
    "yearly":    365,
}

# Amount-based fallback matching (for legacy or non-NGN transactions)
Subscription_plans = {
    "monthly_usd":   {"amount": Decimal("29.95"),  "currency": "USD", "plan": "monthly",   "duration_of_days": 30},
    "quarterly_usd": {"amount": Decimal("88.68"),  "currency": "USD", "plan": "quarterly", "duration_of_days": 90},
    "yearly_usd":    {"amount": Decimal("310.38"), "currency": "USD", "plan": "yearly",    "duration_of_days": 365},
    "monthly_gbp":   {"amount": Decimal("23.66"),  "currency": "GBP", "plan": "monthly",   "duration_of_days": 30},
    "quarterly_gbp": {"amount": Decimal("63.18"),  "currency": "GBP", "plan": "quarterly", "duration_of_days": 90},
    "yearly_gbp":    {"amount": Decimal("248.47"), "currency": "GBP", "plan": "yearly",    "duration_of_days": 365},
}


class PaymentVerifyRequest(BaseModel):
    transaction_id: str
    user_email: str
    plan_type: str | None = None  # monthly | quarterly | yearly — explicit plan from frontend

class BankVerifyRequest(BaseModel):
    account_number: str
    bank_code: str


@router.post("/flutterwave/verify")
async def verify_flutterwave_payment(verify_data: PaymentVerifyRequest, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)]):
    """
    Verify Flutterwave payment transaction and calculate commission
    """
    try:
        transaction_id = verify_data.transaction_id
        user_email = verify_data.user_email

        logger.info(
            "[FLW verify] request | tx_id=%s email=%s plan=%s",
            transaction_id, user_email, verify_data.plan_type
        )

        if not FLUTTERWAVE_SECRET_KEY:
            logger.error("[FLW verify] NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY is not set")
            raise HTTPException(status_code=500, detail="Flutterwave secret key not configured")

        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            logger.warning("[FLW verify] user not found: %s", user_email)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with email {user_email} not found in database."
            )

        logger.info("[FLW verify] user found: id=%s", user.id)

        if transaction_id.startswith("TX-") or not transaction_id.isdigit():
            url = f"{FLUTTERWAVE_BASE_URL}/transactions/verify_by_reference?tx_ref={transaction_id}"
            logger.info("[FLW verify] verifying by tx_ref → %s", url)
        else:
            url = f"{FLUTTERWAVE_BASE_URL}/transactions/{transaction_id}/verify"
            logger.info("[FLW verify] verifying by id → %s", url)

        headers = {
            "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
            "Content-Type": "application/json",
        }

        response = requests.get(url, headers=headers, timeout=15)

        # Always log the raw Flutterwave response so Railway shows the detail
        logger.info(
            "[FLW verify] Flutterwave API response | status=%s body=%s",
            response.status_code, response.text[:500]
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Flutterwave API returned {response.status_code}: {response.text}"
            )
        
        data = response.json()
        
        if data.get("status") == "success":
            transaction_data = data.get("data", {})
            
            if transaction_data.get("status") == "successful":
                amount = transaction_data.get("amount")
                currency = transaction_data.get("currency")
                tx_ref = transaction_data.get("tx_ref")
                
                try:
                    verified_amount = Decimal(str(amount))
                except InvalidOperation:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid amount received from Flutterwave API."
                    )
                
                # Determine subscription plan — prefer explicit plan_type from the
                # frontend request (required for NGN/Flutterwave where we don't
                # maintain price-ID tables). Fall back to amount-based matching
                # for legacy USD/GBP payments.
                current_plan = None
                plan_duration_days = None

                if verify_data.plan_type and verify_data.plan_type in PLAN_DURATION:
                    current_plan = verify_data.plan_type
                    plan_duration_days = PLAN_DURATION[current_plan]
                else:
                    for _, plan_details in Subscription_plans.items():
                        if (plan_details["currency"] == currency
                                and plan_details["amount"] == verified_amount):
                            current_plan = plan_details["plan"]
                            plan_duration_days = plan_details["duration_of_days"]
                            break

                if not current_plan:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Could not determine subscription plan for amount "
                            f"{verified_amount} {currency}. Please include plan_type."
                        )
                    )
                
                # Check for duplicate transaction
                existing_sub = db.query(Subscriptions).filter(
                    (Subscriptions.tx_ref == tx_ref) | 
                    (Subscriptions.transaction_id == transaction_id)
                ).first()

                if existing_sub:
                    logger.warning("[FLW verify] duplicate tx_ref=%s — already processed", tx_ref)
                    return {
                        "status": "success",
                        "message": "Payment verified successfully (already processed).",
                        "data": {
                            "amount": str(verified_amount), 
                            "currency": currency,
                            "tx_ref": tx_ref,
                            "transaction_id": transaction_id,
                            "subscription_plan": current_plan,
                            "user_email": user_email
                        }
                    }
                
                # Update user subscription
                user.subscription_status = "active"
                user.subscription_plan = current_plan

                # Create subscription record
                start_date = datetime.now(timezone.utc)
                end_date = start_date + timedelta(days=plan_duration_days)

                new_subscription = Subscriptions(
                    user_id=user.id,
                    tx_ref=tx_ref,
                    transaction_id=transaction_id,
                    amount=verified_amount,
                    payment_provider="Flutterwave",
                    currency=currency,
                    subscription_plan=current_plan,
                    status="successful",
                    subscription_status="active",
                    start_date=start_date,
                    end_date=end_date
                )
                
                db.add(new_subscription)
                db.flush()
                
                # Calculate commission
                from subscriptions.commission_service import CommissionService
                
                commission = CommissionService.calculate_commission(
                    subscription=new_subscription,
                    db=db
                )
                
                commission_info = None
                if commission:
                    commission_info = {
                        "commission_id": commission.id,
                        "commission_amount": float(commission.amount),
                        "commission_status": commission.status,
                        "referrer_id": commission.user_id
                    }
                    logger.info("[FLW verify] commission amount=%s referrer=%s", commission.amount, commission.user_id)
                else:
                    logger.info("[FLW verify] no commission — user has no referrer")
                
                db.commit()
                db.refresh(user)
                db.refresh(new_subscription)

                # send success payment email
                background_tasks.add_task(
                    email_service.send_payment_success_email,
                    user.email,
                    user.name,
                    float(verified_amount),
                    current_plan,
                    end_date.strftime("%B %d, %Y")
                )
                logger.info("[FLW verify] subscription created user=%s plan=%s", user_email, current_plan)
                
                return {
                    "status": "success",
                    "message": "Payment verified successfully",
                    "data": {
                        "amount": str(verified_amount), 
                        "currency": currency,
                        "tx_ref": tx_ref,
                        "user_email": user_email,
                        "transaction_id": transaction_id,
                        "subscription_plan": current_plan,
                        "start_date": start_date.isoformat(),
                        "expires_on": end_date.isoformat(),
                        "commission": commission_info
                    }
                }
            else:
                # Send failed email
                background_tasks.add_task(
                    email_service.send_payment_failed_email,
                    user.email,
                    user.name,
                    float(verified_amount),
                    f"Transaction status: {transaction_data.get('status')}"
                )
                
                raise HTTPException(
                    status_code=400,
                    detail=f"Payment not successful. Status: {transaction_data.get('status')}"
                )
        else:
            # Send failed email for generic failure
            background_tasks.add_task(
                email_service.send_payment_failed_email,
                user.email,
                user.name,
                0.0, # Unknown amount if verification failed completely
                "Flutterwave verification failed to confirm success"
            )
            raise HTTPException(
                status_code=400,
                detail="Verification failed"
            )
            
    except requests.RequestException as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to communicate with Flutterwave: {str(e)}"
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("[FLW verify] unexpected error: %s", str(e), exc_info=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Payment verification failed: {str(e)}"
        )


@router.post("/flutterwave/verify-account")
async def verify_bank_account(
    account_data: BankVerifyRequest,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Verify user's bank account with Flutterwave"""
    
    # Access current_user as an object (SQLAlchemy model)
    user_obj = current_user
    user_email = current_user.email
    user_id = current_user.id
    
    logger.info("[FLW bank] account=%s bank_code=%s user=%s id=%s",
                account_data.account_number, account_data.bank_code, user_email, user_id)

    if not FLUTTERWAVE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Flutterwave secret key not configured")

    is_test_mode = FLUTTERWAVE_SECRET_KEY.startswith("FLWSECK_TEST")
    logger.info("[FLW bank] mode=%s", "TEST" if is_test_mode else "LIVE")
    
    url = "https://api.flutterwave.com/v3/accounts/resolve"
    headers = {
        "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "account_number": account_data.account_number,
        "account_bank": account_data.bank_code
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        logger.info("[FLW bank] response status=%s", response.status_code)
        logger.info("[FLW bank] response body=%s", response.text[:300])
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") == "success":
                account_name = data.get("data", {}).get("account_name")
                
                if not account_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Account name not found in response"
                    )
                
                logger.info("[FLW bank] verified name=%s user=%s", account_name, user_email)
                
                return {
                    "status": "success",
                    "account_name": account_name,
                    "user_email": user_email,
                    "user_id": user_id
                }
            else:
                error_message = data.get("message", "Account verification failed")
                logger.warning("[FLW bank] verification failed: %s", error_message)
                raise HTTPException(status_code=400, detail=error_message)

        elif response.status_code == 401:
            logger.error("[FLW bank] invalid API key")
            raise HTTPException(status_code=500, detail="Invalid Flutterwave API credentials. Please contact support.")

        elif response.status_code == 429:
            logger.warning("[FLW bank] rate limit exceeded")
            raise HTTPException(status_code=429, detail="Too many requests. Please try again in a few minutes.")

        else:
            error_data = response.json() if response.text else {}
            error_message = error_data.get("message", "Account verification failed")
            logger.error("[FLW bank] API error status=%s msg=%s", response.status_code, error_message)
            if "invalid" in error_message.lower() or "not found" in error_message.lower():
                raise HTTPException(status_code=400, detail=f"Invalid account details: {error_message}")
            raise HTTPException(status_code=response.status_code, detail=f"Flutterwave error: {error_message}")

    except requests.Timeout:
        logger.error("[FLW bank] request timeout")
        raise HTTPException(status_code=504, detail="Request timeout. Please try again.")

    except requests.RequestException as e:
        logger.error("[FLW bank] request error: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to connect to Flutterwave: {str(e)}")

    except HTTPException:
        raise

    except Exception as e:
        logger.error("[FLW bank] unexpected error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


# Note: The /flutterwave/callback endpoint below handles transfer webhooks


@router.post("/flutterwave/callback")
async def flutterwave_payout_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Webhook to receive Flutterwave transfer status updates.
    
    Events:
    - transfer.completed: Money successfully sent to user
    - transfer.failed: Transfer failed
    """
    try:
        # Get raw body for signature verification
        body = await request.body()
        payload = await request.json()
        
        # Verify webhook signature (security)
        webhook_secret = os.getenv("FLUTTERWAVE_WEBHOOK_SECRET")
        if webhook_secret:
            signature = request.headers.get("verif-hash")
            if signature != webhook_secret:
                logger.warning("[FLW webhook] invalid signature: %s", signature)
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
        else:
            logger.warning("[FLW webhook] FLUTTERWAVE_WEBHOOK_SECRET not set — signature check skipped")

        event_type = payload.get("event", "")
        transfer_data = payload.get("data", {})
        reference = transfer_data.get("reference", "")
        transfer_status = transfer_data.get("status", "").lower()

        logger.info("[FLW webhook] event=%s ref=%s status=%s", event_type, reference, transfer_status)

        if reference and reference.startswith("PAYOUT-"):
            try:
                payout_id = int(reference.split("-")[1])
            except (IndexError, ValueError):
                logger.error("[FLW webhook] cannot parse payout_id from ref: %s", reference)
                return {"status": "error", "message": "Invalid reference format"}

            from subscriptions.payout_service import PayoutService
            from database.pg_models import Payout, Commission

            if event_type == "transfer.completed" or transfer_status == "successful":
                PayoutService.complete_flutterwave_payout(payout_id, background_tasks, "successful", db)
                logger.info("[FLW webhook] payout %s completed", payout_id)
            elif event_type == "transfer.failed" or transfer_status == "failed":
                PayoutService.complete_flutterwave_payout(payout_id, background_tasks, "failed", db)
                logger.warning("[FLW webhook] payout %s failed", payout_id)
            else:
                logger.warning("[FLW webhook] unknown event=%s status=%s", event_type, transfer_status)
        else:
            logger.info("[FLW webhook] non-payout ref: %s", reference)

        return {"status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[FLW webhook] error: %s", str(e), exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/flutterwave/config")
async def get_flutterwave_config():
    """
    Return the Flutterwave public key so the frontend never hardcodes it.
    Safe to expose — it is the publishable/public key only.
    """
    if not FLUTTERWAVE_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="Flutterwave public key not configured")
    return {"publicKey": FLUTTERWAVE_PUBLIC_KEY}


@router.get("/health")
async def payment_health_check():
    """
    Check if payment system is configured correctly
    """
    return {
        "status": "healthy",
        "flutterwave_configured": bool(FLUTTERWAVE_SECRET_KEY),
        "public_key_configured": bool(FLUTTERWAVE_PUBLIC_KEY),
        "secret_key_prefix": FLUTTERWAVE_SECRET_KEY[:15] + "..." if FLUTTERWAVE_SECRET_KEY else None,
        "is_test_mode": FLUTTERWAVE_SECRET_KEY.startswith("FLWSECK_TEST") if FLUTTERWAVE_SECRET_KEY else False,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/test")
async def test_endpoint():
    """
    Simple test endpoint to verify routes are working
    """
    return {
        "status": "ok",
        "message": "Flutterwave payment routes are working",
        "endpoints": [
            "POST /api/payments/flutterwave/verify",
            "POST /api/payments/flutterwave/verify-account",
            "GET /api/payments/health",
            "GET /api/payments/test"
        ]
    }