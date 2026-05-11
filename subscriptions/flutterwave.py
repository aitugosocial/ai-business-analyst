# subscriptions/flutterwave.py
from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel
import requests
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
print('Flutterwave environment variables loaded.')


# Change prefix to match your frontend URL
router = APIRouter(prefix="/api/payments", tags=["payments"])

# Flutterwave configuration
FLUTTERWAVE_SECRET_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY")
FLUTTERWAVE_PUBLIC_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_PUBLIC_KEY")
FLUTTERWAVE_ENCRYPTION_KEY = os.getenv("FLUTTERWAVE_ENCRYPTION_KEY")
FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"

Subscription_plans = {
    "monthly": {
        "amount": Decimal("29.95"),
        "currency": "USD",
        "duration_of_days": 30
    },
    "yearly": {
        "amount": Decimal("290.00"),
        "currency": "USD",
        "duration_of_days": 365
    }
}

class PaymentVerifyRequest(BaseModel):
    transaction_id: str
    user_email: str

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
        
        print(f"=== FLUTTERWAVE VERIFICATION ===")
        print(f"Transaction ID: {transaction_id}")
        print(f"User Email: {user_email}")
        
        if not FLUTTERWAVE_SECRET_KEY:
            raise HTTPException(
                status_code=500,
                detail="Flutterwave secret key not configured"
            )
        
        # Verify user exists
        user = db.query(User).filter(User.email == user_email).first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with email {user_email} not found in database."
            )
        
        print(f"✅ User found: {user.email} (ID: {user.id})")
        
        # Verify with Flutterwave
        # Support both numeric transaction ID and transaction reference (tx_ref)
        if transaction_id.startswith("TX-") or not transaction_id.isdigit():
            print(f"ℹ️  Verifying by reference: {transaction_id}")
            url = f"{FLUTTERWAVE_BASE_URL}/transactions/verify_by_reference?tx_ref={transaction_id}"
        else:
            print(f"ℹ️  Verifying by ID: {transaction_id}")
            url = f"{FLUTTERWAVE_BASE_URL}/transactions/{transaction_id}/verify"
            
        headers = {
            "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Flutterwave API error: {response.text}"
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
                
                # Match to subscription plan
                current_plan = None
                plan_duration_days = None
                
                for name, plan_details in Subscription_plans.items():
                    if plan_details["currency"] == currency and plan_details["amount"] == verified_amount:
                        current_plan = name
                        plan_duration_days = plan_details["duration_of_days"]
                        break
        
                if not current_plan:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Verified amount {verified_amount} {currency} does not match any known subscription plan."
                    )
                
                # Check for duplicate transaction
                existing_sub = db.query(Subscriptions).filter(
                    (Subscriptions.tx_ref == tx_ref) | 
                    (Subscriptions.transaction_id == transaction_id)
                ).first()

                if existing_sub:
                    print(f"⚠️  Transaction {tx_ref} already processed.")
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
                    print(f"✅ Commission created: ${commission.amount} for referrer {commission.user_id}")
                else:
                    print(f"ℹ️  No commission created (user not referred)")
                
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
                print(f"✅ Subscription created for {user_email}")
                
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
        print(f"❌ Verification error: {str(e)}")
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
    
    print(f"=== BANK ACCOUNT VERIFICATION ===")
    print(f"Account Number: {account_data.account_number}")
    print(f"Bank Code: {account_data.bank_code}")
    print(f"User: {user_email} (ID: {user_id})")
    
    if not FLUTTERWAVE_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Flutterwave secret key not configured"
        )
    
    # Check if using test credentials
    is_test_mode = FLUTTERWAVE_SECRET_KEY.startswith("FLWSECK_TEST")
    
    print(f"Test Mode: {is_test_mode}")
    print(f"Secret Key Prefix: {FLUTTERWAVE_SECRET_KEY[:20]}...")
    
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
        
        print(f"Flutterwave Response Status: {response.status_code}")
        print(f"Flutterwave Response: {response.text}")
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") == "success":
                account_name = data.get("data", {}).get("account_name")
                
                if not account_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Account name not found in response"
                    )
                
                print(f"✅ Account verified: {account_name} for user {user_email}")
                
                return {
                    "status": "success",
                    "account_name": account_name,
                    "user_email": user_email,
                    "user_id": user_id
                }
            else:
                error_message = data.get("message", "Account verification failed")
                print(f"❌ Verification failed: {error_message}")
                raise HTTPException(
                    status_code=400,
                    detail=error_message
                )
        
        elif response.status_code == 401:
            print("❌ Authentication failed - Invalid API key")
            raise HTTPException(
                status_code=500,
                detail="Invalid Flutterwave API credentials. Please contact support."
            )
        
        elif response.status_code == 429:
            print("❌ Rate limit exceeded")
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again in a few minutes."
            )
        
        else:
            error_data = response.json() if response.text else {}
            error_message = error_data.get("message", "Account verification failed")
            
            print(f"❌ API Error: {error_message}")
            
            # Provide helpful error messages
            if "invalid" in error_message.lower() or "not found" in error_message.lower():
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid account details: {error_message}"
                )
            
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Flutterwave error: {error_message}"
            )
    
    except requests.Timeout:
        print("❌ Request timeout")
        raise HTTPException(
            status_code=504,
            detail="Request timeout. Please try again."
        )
    
    except requests.RequestException as e:
        print(f"❌ Request error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to connect to Flutterwave: {str(e)}"
        )
    
    except HTTPException:
        raise
    
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Verification failed: {str(e)}"
        )


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
                print(f"[Webhook] Invalid signature: {signature}")
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
        else:
            print("[Webhook] WARNING: FLUTTERWAVE_WEBHOOK_SECRET not set - skipping signature verification")
        
        # Extract event details
        event_type = payload.get("event", "")  # "transfer.completed" or "transfer.failed"
        transfer_data = payload.get("data", {})
        reference = transfer_data.get("reference", "")  # "PAYOUT-123-1234567890"
        transfer_status = transfer_data.get("status", "").lower()
        
        print(f"[Webhook] Event: {event_type}, Reference: {reference}, Status: {transfer_status}")
        
        # Extract payout_id from reference (format: PAYOUT-{id}-{timestamp})
        if reference and reference.startswith("PAYOUT-"):
            try:
                payout_id = int(reference.split("-")[1])
            except (IndexError, ValueError):
                print(f"[Webhook] Could not extract payout_id from reference: {reference}")
                return {"status": "error", "message": "Invalid reference format"}
            
            # Import and use PayoutService
            from subscriptions.payout_service import PayoutService
            from database.pg_models import Payout, Commission
            
            if event_type == "transfer.completed" or transfer_status == "successful":
                PayoutService.complete_flutterwave_payout(payout_id, background_tasks, "successful", db)
                print(f"[Webhook] ✅ Payout {payout_id} marked as completed")
                
            elif event_type == "transfer.failed" or transfer_status == "failed":
                PayoutService.complete_flutterwave_payout(payout_id, background_tasks, "failed", db)
                print(f"[Webhook] ❌ Payout {payout_id} marked as failed")
            else:
                print(f"[Webhook] Unknown event/status: {event_type}/{transfer_status}")
        else:
            print(f"[Webhook] Reference not related to payouts: {reference}")
        
        return {"status": "success"}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Webhook] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return 200 to prevent Flutterwave from retrying
        return {"status": "error", "message": str(e)}


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