# subscriptions/flutterwave.py
from typing import Annotated
from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel
import requests
import logging
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from decimal import Decimal, InvalidOperation

# import the database
from sqlalchemy.orm import Session
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

class PaymentEventRequest(BaseModel):
    event: str          # e.g. checkout_opened | callback | error | closed
    tx_ref: str | None = None
    status: str | None = None   # successful | failed | cancelled | timeout
    error: str | None = None    # raw error message from Flutterwave
    user_email: str | None = None
    plan_type: str | None = None
    amount: float | None = None
    currency: str | None = None


@router.post("/flutterwave/event")
async def log_flutterwave_event(body: PaymentEventRequest):
    """
    Called by the frontend at every stage of the Flutterwave checkout flow.
    Produces Railway log lines for all payment events — including failures
    that occur inside Flutterwave's iframe before our verify endpoint is hit.
    No authentication required; event data is logged only, not persisted.
    """
    if body.event == "error" or body.status in ("failed", "cancelled", "timeout"):
        logger.error(
            "[FLW event] %s | tx_ref=%s status=%s error=%s user=%s plan=%s amount=%s %s",
            body.event, body.tx_ref, body.status, body.error,
            body.user_email, body.plan_type, body.amount, body.currency or ""
        )
    else:
        logger.info(
            "[FLW event] %s | tx_ref=%s status=%s user=%s plan=%s amount=%s %s",
            body.event, body.tx_ref, body.status,
            body.user_email, body.plan_type, body.amount, body.currency or ""
        )
    return {"status": "logged"}


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
            # Extracted before the successful/failed branch below (not just
            # inside the successful branch, where they used to live) — the
            # failed branch references the raw amount for its failure email,
            # and reading an unset local there raised NameError, crashing
            # the failed-payment path itself instead of notifying the user.
            amount = transaction_data.get("amount")
            currency = transaction_data.get("currency")

            if transaction_data.get("status") == "successful":
                tx_ref = transaction_data.get("tx_ref")

                try:
                    verified_amount = Decimal(str(amount))
                except InvalidOperation:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid amount received from Flutterwave API."
                    )

                # If checkout grossed up the charge with Flutterwave's own
                # fee/VAT (see flutterwave_split.get_flutterwave_processing_fee
                # and checkoutForm.tsx), verified_amount above is the INFLATED
                # total the customer's card/account was actually debited for
                # (base + fee) — not the subscription's real price. Everything
                # we store or show the user (Subscriptions.amount, the
                # commission/builder-bonus basis, the success notification/
                # email) must use the true base price instead, echoed back via
                # meta the same deterministic way SPLIT_META_KEY already is.
                # Legacy exact-amount plan matching below still needs the RAW
                # verified_amount, since that path is only ever hit for
                # non-NGN flows that never had fee gross-up applied.
                from subscriptions.flutterwave_split import BASE_AMOUNT_META_KEY
                charge_meta = transaction_data.get("meta") or {}
                base_amount_meta = charge_meta.get(BASE_AMOUNT_META_KEY)
                if base_amount_meta is not None:
                    try:
                        recorded_amount = Decimal(str(base_amount_meta))
                    except InvalidOperation:
                        recorded_amount = verified_amount
                else:
                    recorded_amount = verified_amount
                
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
                            "amount": str(recorded_amount),
                            "currency": currency,
                            "tx_ref": tx_ref,
                            "transaction_id": transaction_id,
                            "subscription_plan": current_plan,
                            "user_email": user_email
                        }
                    }
                
                # Capture Flutterwave card token from data.card.token.
                # This token can be used to charge the card off-session for
                # automatic renewals — no user action required at renewal time.
                card_info = transaction_data.get("card", {}) or {}
                flw_token = card_info.get("token")
                if flw_token and hasattr(user, "flutterwave_card_token"):
                    user.flutterwave_card_token = flw_token
                    # Also update card display metadata so the UI shows the card
                    if card_info.get("last_4digits"):
                        user.card_last4 = card_info["last_4digits"]
                    if card_info.get("type"):
                        user.card_brand = card_info["type"].title()
                    logger.info("[FLW verify] card token saved for user %s", user.id)

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
                    amount=recorded_amount,
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

                # Award 20 chops to the referrer if this subscriber was referred
                from database.pg_models import Referral as ReferralModel
                referral_record = db.query(ReferralModel).filter(
                    ReferralModel.referred_user_id == user.id
                ).first()
                if referral_record:
                    referrer_user = db.query(User).filter(User.id == referral_record.referrer_id).first()
                    if referrer_user:
                        referrer_user.total_chops = (referrer_user.total_chops or 0) + 20
                        referrer_user.referral_chops = (referrer_user.referral_chops or 0) + 20
                        referral_record.chops_awarded = (referral_record.chops_awarded or 0) + 20
                        logger.info(
                            "[FLW verify] awarded 20 chops to referrer %s for referred user %s subscribing",
                            referrer_user.id, user.id
                        )
                        from api.services.notification_service import NotificationService as NS
                        NS.create_notification(
                            db=db,
                            user_id=referrer_user.id,
                            type="referral_subscription",
                            title="🎉 Referral Subscribed!",
                            message=f"{user.name} subscribed to a plan. You earned 20 chops!",
                            link="/dashboard/earnings"
                        )

                # Calculate commission. already_settled reads back the
                # subaccount id this SPECIFIC charge's checkout config was
                # built with (echoed via Flutterwave's meta passthrough —
                # see flutterwave_split.SPLIT_META_KEY) rather than
                # re-checking "does a verified subaccount exist right now",
                # which would be wrong if the referrer added one in the gap
                # between checkout starting and this verification running —
                # that later timing must not retroactively mark THIS charge
                # (which was never split) as already paid.
                from subscriptions.commission_service import CommissionService
                from subscriptions.flutterwave_split import SPLIT_META_KEY

                already_settled = bool(charge_meta.get(SPLIT_META_KEY))
                logger.info(
                    "[FLW verify] [BUILDER BONUS] split check | tx_ref=%s meta=%s already_settled=%s "
                    "(true = this charge was placed with a Flutterwave subaccount split, "
                    "the referrer's share landed with them directly at payment time)",
                    tx_ref, charge_meta, already_settled,
                )

                commission = CommissionService.calculate_commission(
                    subscription=new_subscription,
                    db=db,
                    already_settled=already_settled,
                )

                commission_info = None
                if commission:
                    commission_info = {
                        "commission_id": commission.id,
                        "commission_amount": float(commission.amount),
                        "commission_status": commission.status,
                        "referrer_id": commission.user_id
                    }
                    logger.info(
                        "[FLW verify] [BUILDER BONUS] awarded | tx_ref=%s referred_user=%s referrer=%s "
                        "amount=%s %s rate=%s%% status=%s already_settled=%s commission_id=%s",
                        tx_ref, user.id, commission.user_id, commission.amount, currency,
                        commission.commission_rate, commission.status, already_settled, commission.id,
                    )
                else:
                    logger.info(
                        "[FLW verify] [BUILDER BONUS] none | tx_ref=%s referred_user=%s has no referrer "
                        "on record — no commission to award",
                        tx_ref, user.id,
                    )
                
                # In-app payment success notification (real-time via WebSocket)
                from api.services.notification_service import NotificationService
                NotificationService.create_notification(
                    db=db,
                    user_id=user.id,
                    type="payment_success",
                    title="✅ Payment Successful",
                    message=(
                        f"Your {current_plan.title()} plan is now active until "
                        f"{end_date.strftime('%B %d, %Y')}. "
                        f"Amount: {currency} {recorded_amount}."
                    ),
                    link="/dashboard/opportunity-alerts",
                )

                # Referrer builder-bonus notification (real-time).
                # NOTE: calculate_commission() above already sends its own
                # "Builder Bonus Earned!" notification internally, so this
                # block was always a duplicate — and, using `commission`
                # (the Commission ORM object) as a dict instead of
                # `commission_info` (the dict actually built above), it
                # raised TypeError on every referred user's payment,
                # uncaught inside this try block. That's a severe pre-
                # existing bug: it could roll back the whole transaction —
                # including the subscription just verified — for anyone
                # with a referrer, despite Flutterwave having already
                # charged them. Fixed to use commission_info; left in place
                # rather than removed since removing it isn't this
                # session's task and commission_service's own notification
                # doesn't currently reflect already_settled status changes
                # made after it fires.
                if commission_info:
                    NotificationService.create_notification(
                        db=db,
                        user_id=commission_info["referrer_id"],
                        type="commission_earned",
                        title="🎉 Builder Bonus Earned!",
                        message=(
                            f"You earned a builder bonus of {commission_info['commission_amount']:.2f} "
                            f"{currency} from a referral subscription."
                        ),
                        link="/dashboard/earnings",
                    )

                db.commit()
                db.refresh(user)
                db.refresh(new_subscription)

                # send success payment email
                background_tasks.add_task(
                    email_service.send_payment_success_email,
                    user.email,
                    user.name,
                    float(recorded_amount),
                    current_plan,
                    end_date.strftime("%B %d, %Y")
                )

                # One consolidated summary per payment, so the full story of
                # a single event (subscription + referral chops + builder
                # bonus) is readable as one block instead of pieced together
                # from separate lines scattered earlier in this function.
                if commission_info:
                    bonus_line = (
                        f"BUILDER BONUS: {commission_info['commission_amount']:.2f} {currency} "
                        f"to referrer user={commission_info['referrer_id']} "
                        f"(status={commission_info['commission_status']}, "
                        f"{'auto-settled via Flutterwave split' if already_settled else 'pending manual payout'})"
                    )
                elif referral_record:
                    bonus_line = "BUILDER BONUS: none (referrer has no commission record for this subscription)"
                else:
                    bonus_line = "BUILDER BONUS: none (user was not referred)"
                logger.info(
                    "[FLW verify] PAYMENT EVENT COMPLETE | tx_id=%s tx_ref=%s user=%s(id=%s) "
                    "plan=%s charged=%s %s recorded=%s %s %s | %s",
                    transaction_id, tx_ref, user_email, user.id, current_plan,
                    verified_amount, currency, recorded_amount, currency,
                    "(fee/VAT-inclusive charge)" if recorded_amount != verified_amount else "",
                    bonus_line,
                )

                return {
                    "status": "success",
                    "message": "Payment verified successfully",
                    "data": {
                        "amount": str(recorded_amount),
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
                from api.services.notification_service import NotificationService
                NotificationService.create_notification(
                    db=db, user_id=user.id, type="payment_failed",
                    title="❌ Payment Failed",
                    message="We could not process your payment. Please try again or use a different payment method.",
                    link="/dashboard/upgrade",
                )
                background_tasks.add_task(
                    email_service.send_payment_failed_email,
                    user.email, user.name, float(amount) if amount is not None else 0.0,
                    f"Transaction status: {transaction_data.get('status')}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Payment not successful. Status: {transaction_data.get('status')}"
                )
        else:
            from api.services.notification_service import NotificationService
            NotificationService.create_notification(
                db=db, user_id=user.id, type="payment_failed",
                title="❌ Payment Failed",
                message="We could not verify your payment. Please contact support if you were charged.",
                link="/dashboard/upgrade",
            )
            background_tasks.add_task(
                email_service.send_payment_failed_email,
                user.email, user.name, 0.0,
                "Flutterwave verification failed to confirm success"
            )
            raise HTTPException(status_code=400, detail="Verification failed")
            
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


class BankResolveRequest(BaseModel):
    account_number: str


# NUBAN (Nigerian Uniform Bank Account Number) doesn't encode the issuing
# bank the way an IBAN does, so Flutterwave has no single "resolve just from
# account number" endpoint — /accounts/resolve always requires a bank code.
# The bank list itself barely changes, so it's cached for a day rather than
# fetched on every lookup.
_bank_list_cache: dict = {"data": None, "ts": 0.0}
_BANK_LIST_TTL_SECONDS = 24 * 60 * 60


def _fetch_nigerian_banks() -> list[dict]:
    now = time.time()
    if _bank_list_cache["data"] is not None and (now - _bank_list_cache["ts"]) < _BANK_LIST_TTL_SECONDS:
        return _bank_list_cache["data"]
    response = requests.get(
        f"{FLUTTERWAVE_BASE_URL}/banks/NG",
        headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"},
        timeout=10,
    )
    response.raise_for_status()
    banks = response.json().get("data", []) or []
    _bank_list_cache["data"] = banks
    _bank_list_cache["ts"] = now
    return banks


def _try_resolve_against_bank(account_number: str, bank: dict) -> dict | None:
    """One /accounts/resolve attempt against a single bank. Returns None on
    any failure (wrong bank, timeout, error) — this is expected to fail for
    every bank except the right one, so failure here is normal, not
    exceptional."""
    try:
        response = requests.post(
            f"{FLUTTERWAVE_BASE_URL}/accounts/resolve",
            json={"account_number": account_number, "account_bank": bank.get("code")},
            headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}", "Content-Type": "application/json"},
            timeout=8,
        )
        if response.status_code == 200:
            data = response.json()
            account_name = data.get("data", {}).get("account_name") if data.get("status") == "success" else None
            if account_name:
                return {
                    "bank_code": bank.get("code"),
                    "bank_name": bank.get("name"),
                    "account_name": account_name,
                }
    except requests.RequestException:
        pass
    return None


async def _resolve_against_many(account_number: str, banks: list[dict], max_workers: int) -> list[dict]:
    """Try every bank in `banks` concurrently, capped at max_workers threads
    at once (asyncio.to_thread's default executor caps out around ~32
    workers, which serializes a large bank list into multiple slow waves —
    a dedicated pool sized to the list avoids that).

    Returns EVERY bank that resolved successfully, not just the first —
    the same NUBAN-style account number can validly resolve against more
    than one institution (reported directly: an Opay account resolved
    against a wrong-but-real bank first, silently auto-filling it, and the
    user only found the real Opay code by looking it up manually). Racing
    to the first hit made that failure mode invisible; a full sweep surfaces
    every candidate so the caller can decide (auto-fill only when there is
    exactly one, otherwise ask the user to pick).
    """
    if not banks:
        return []
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(banks)))
    try:
        futures = [
            loop.run_in_executor(pool, _try_resolve_against_bank, account_number, bank)
            for bank in banks
        ]
        results = await asyncio.gather(*futures)
    finally:
        pool.shutdown(wait=False)
    return [r for r in results if r]


@router.post("/flutterwave/resolve-bank")
async def resolve_bank_from_account_number(
    body: BankResolveRequest,
    current_user=Depends(get_current_user),
):
    """Given only an account number, find every Nigerian bank it validly
    resolves against — lets the payout-account form auto-fill the bank
    code when there's exactly one match, or ask the user to pick when
    there's more than one, instead of asking them to know/look up the
    code themselves from scratch.

    NUBAN doesn't encode the issuing bank the way an IBAN does, so there is
    no cheaper single-call lookup — this has to try candidate banks against
    /accounts/resolve. It has to try ALL of them and collect every match,
    not stop at the first: the same account number can validly resolve
    against more than one institution (confirmed directly — an Opay account
    number also resolved successfully against an unrelated bank, and a
    first-match-wins design would have auto-filled that wrong bank with no
    way for the user to notice). Flutterwave's NG bank list has ~700
    entries (not the ~25 well-known commercial banks one might expect — it
    includes hundreds of microfinance banks, mobile-money wallets, and
    fintechs). Tiered to keep the common case fast: Tier 1 sweeps only the
    ~25 traditional deposit-money banks (Access, GTBank, Zenith, etc. —
    identifiable by their short, legacy 3-digit CBN codes, unlike newer
    institutions' 6-digit codes), covering the large majority of real
    accounts in a few seconds. Tier 2 — only reached if tier 1 finds
    nothing — sweeps the remaining ~675 fintech/microfinance banks (Kuda,
    Opay, Moniepoint, etc.) with a wider thread pool.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Flutterwave secret key not configured")

    account_number = (body.account_number or "").strip()
    if len(account_number) != 10 or not account_number.isdigit():
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit account number")

    try:
        banks = await asyncio.to_thread(_fetch_nigerian_banks)
    except Exception as e:
        logger.error(f"[FLW bank resolve] Failed to fetch bank list: {e}")
        raise HTTPException(status_code=502, detail="Could not fetch the bank list from Flutterwave")

    if not banks:
        raise HTTPException(status_code=502, detail="No banks returned by Flutterwave")

    major_banks = [b for b in banks if len(str(b.get("code") or "")) <= 3]
    other_banks = [b for b in banks if len(str(b.get("code") or "")) > 3]

    matches = await _resolve_against_many(account_number, major_banks, max_workers=30)
    if not matches:
        logger.info(f"[FLW bank resolve] No match among {len(major_banks)} major banks, sweeping {len(other_banks)} others")
        matches = await _resolve_against_many(account_number, other_banks, max_workers=80)

    if not matches:
        raise HTTPException(status_code=404, detail="Could not find a matching bank for this account number")

    match_summary = [f"{m['bank_name']} ({m['bank_code']})" for m in matches]
    logger.info(f"[FLW bank resolve] {account_number} -> {len(matches)} candidate(s): {match_summary}")
    return {"status": "success", "matches": matches}


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


@router.get("/flutterwave/split-info")
async def get_flutterwave_split_info(
    db: Annotated[Session, Depends(get_db)],
    current_user: User = Depends(get_current_user),
):
    """
    Called by the frontend before building its Flutterwave checkout config
    (see app/l/upgrade/checkoutForm.tsx). Returns the referrer's Flutterwave
    subaccount id + split percentage to include in that config's
    `subaccounts` field IF the current user was referred by someone with a
    verified payout account — {"subaccount_id": null} otherwise, meaning
    charge 100% to Lavoo as before (the referred user is never blocked on
    this; only whether the payment splits is affected).
    """
    from subscriptions.commissions import extract_user_id
    from subscriptions.flutterwave_split import get_split_config_for_referred_user

    user_id = extract_user_id(current_user)
    split_config = get_split_config_for_referred_user(user_id, db)
    if not split_config:
        return {"status": "success", "data": {"subaccount_id": None, "main_account_charge_percentage": None}}
    return {"status": "success", "data": split_config}


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