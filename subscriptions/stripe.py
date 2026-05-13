
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
from decimal import Decimal
import os
import secrets
import stripe

from database.pg_connections import get_db, SessionLocal
from database.pg_models import (
    PaymentIntentCreate, PaymentIntentResponse, PaymentVerify,
    SubscriptionResponse, CreateSubscriptionRequest,
    UpdatePaymentMethodRequest, ConfirmSubscriptionRequest, SaveCardRequest
)

from .stripe_service import StripeService
from database.pg_models import User, Subscriptions
from api.routes.auth.login import get_current_user
import json
import logging
import traceback

logger = logging.getLogger(__name__)

from fastapi import BackgroundTasks
from emailing.email_service import email_service
from api.services.notification_service import NotificationService
from database.pg_models import NotificationType
from .beta_service import BetaService

router = APIRouter(prefix="/api/stripe", tags=["stripe"])

# Log the Stripe mode at startup so Railway logs immediately show whether the
# backend is using a live or test key — helps catch test/live mismatches fast.
_startup_key = os.getenv("STRIPE_SECRET_KEY", "")
_stripe_mode = "LIVE" if _startup_key.startswith("sk_live_") else ("TEST" if _startup_key.startswith("sk_test_") else "UNKNOWN/MISSING")
logger.info(f"[Stripe] Initialised in {_stripe_mode} mode (key prefix: {_startup_key[:14]}...)")


# =============================================================================
# BILLING FLOW
# =============================================================================
#
# APP_MODE = "beta"
#   save-card-beta → save card only, NO charge, mark is_beta_user=True.
#
# APP_MODE = "launch"
#   save-card-beta → resolve the user's Stripe state into one of three cases:
#
#   CASE A — sub active in Stripe + valid DB record
#     → already subscribed; card update only.
#
#   CASE B — sub active in Stripe, NO valid DB record
#     → previous session created the Stripe sub but our DB write never finished
#       (crash, abandoned 3DS, etc). MUST NOT call Subscription.create() —
#       Stripe rejects it with "cannot combine currencies". Instead: adopt the
#       existing sub by writing the missing DB record.
#
#   CASE C — no active Stripe sub
#     → cancel any stale incomplete sub, then create a fresh subscription.
#     → incomplete (3DS) → return requires_action, no DB record yet
#     → active → write DB record, mark user active
#
# =============================================================================


SUPPORTED_CURRENCIES = {"USD", "GBP", "NGN"}
DEFAULT_CURRENCY = "USD"


def get_stripe_price_id(plan_type: str, currency: str = "USD") -> str:
    """
    Resolve the Stripe Price ID for a given plan and currency.

    .env naming convention: STRIPE_{PLAN}_PRICE_ID_{CURRENCY}
      e.g. STRIPE_MONTHLY_PRICE_ID_USD, STRIPE_MONTHLY_PRICE_ID_GBP

    Lookup order:
      1. STRIPE_{PLAN}_PRICE_ID_{CURRENCY}  (currency-specific — matches your .env)
      2. STRIPE_{PLAN}_PRICE_ID             (bare key — legacy fallback, no suffix)
    """
    base_keys = {
        "monthly":   "STRIPE_MONTHLY_PRICE_ID",
        "quarterly": "STRIPE_QUARTERLY_PRICE_ID",
        "yearly":    "STRIPE_YEARLY_PRICE_ID",
    }
    base = base_keys.get(plan_type)
    if not base:
        logger.warning(f"⚠️ Unknown plan_type '{plan_type}'")
        return ""

    currency = (currency or DEFAULT_CURRENCY).upper().strip()
    if currency not in SUPPORTED_CURRENCIES:
        logger.warning(f"⚠️ Unsupported currency '{currency}' — falling back to USD")
        currency = DEFAULT_CURRENCY

    # Try currency-specific key first (matches your .env naming)
    suffixed_key = f"{base}_{currency}"
    price_id = os.getenv(suffixed_key, "").strip()
    if price_id:
        logger.info(f"💱 Price ID: {suffixed_key} → {price_id}")
        return price_id

    # Fall back to bare key (STRIPE_MONTHLY_PRICE_ID without suffix)
    price_id = os.getenv(base, "").strip()
    if price_id:
        logger.info(f"💱 Price ID (bare fallback): {base} → {price_id}")
        return price_id

    logger.error(
        f"❌ No price ID for plan='{plan_type}', currency='{currency}'. "
        f"Tried: {suffixed_key}, {base}"
    )
    return ""


def get_currency_from_request(request) -> str:
    """Extract and validate currency from a request object. Raises 400 if missing or unsupported."""
    raw = (getattr(request, 'currency', None) or "").strip()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="currency is required (USD, GBP, or NGN)."
        )
    currency = raw.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported currency '{raw}'. Accepted: {', '.join(sorted(SUPPORTED_CURRENCIES))}."
        )
    return currency


def generate_tx_ref(prefix: str = "STRIPE") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random_str = secrets.token_hex(4).upper()
    return f"{prefix}-{timestamp}-{random_str}"


def get_amount_from_stripe_price(price_id: str) -> float:
    """
    Fetch the unit amount for a Price ID directly from Stripe.
    This is the single source of truth — avoids database price duplication
    and ensures the amount recorded in our DB always matches what Stripe charges.
    Returns the amount in the Price's major currency unit (e.g. dollars, not cents).
    """
    if not price_id:
        raise ValueError("Cannot fetch amount: price_id is empty or not configured.")
    price = stripe.Price.retrieve(price_id)
    unit_amount = getattr(price, "unit_amount", None)
    if unit_amount is None:
        raise ValueError(f"Stripe Price {price_id} has no unit_amount (is it a metered or tiered price?)")
    return round(unit_amount / 100, 2)


def extract_user_id(current_user) -> int:
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                uid = user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                uid = user_data.id
            else:
                uid = user_data
        else:
            uid = current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
        if uid is None:
            raise HTTPException(status_code=500, detail="Could not extract user_id from token")
        return int(uid)
    return int(current_user.id)


def resolve_stripe_subscription_state(user: User, db: Session) -> dict:
    """
    Determines the user's true subscription state by cross-referencing
    Stripe and our local DB. Returns a dict:

      {
        "case": "fully_subscribed" | "stripe_active_no_db" | "needs_new_sub",
        "stripe_sub": <Stripe sub dict or None>,
        "stripe_sub_id": <str or None>,
      }

    CASE MEANINGS
    ─────────────
    "fully_subscribed"
        Stripe active/trialing AND valid non-expired DB record → card update only.

    "stripe_active_no_db"
        Stripe active/trialing BUT no matching DB record.
        A previous session created the Stripe sub but our DB write never
        completed. DO NOT call Subscription.create() — Stripe will reject
        with a currency conflict. Adopt the existing sub instead.

    "needs_new_sub"
        No live Stripe subscription → safe to call Subscription.create().
        Caller must cancel any stale incomplete sub first.
    """
    # IMPORTANT: User model does not have stripe_subscription_id column.
    # The subscription ID lives in the Subscriptions table. We look up the
    # most recent active DB record first, then fall back to user attribute
    # (for legacy rows that may have it). Using getattr blindly returns ''
    # which causes needs_new_sub every time → double-billing for active users.
    sub_id = None

    # Primary: look up from Subscriptions table (authoritative source)
    active_db_sub = db.query(Subscriptions).filter(
        Subscriptions.user_id == user.id,
        Subscriptions.subscription_status == "active",
        Subscriptions.end_date > datetime.now(timezone.utc),
    ).order_by(Subscriptions.created_at.desc()).first()

    if active_db_sub:
        sub_id = getattr(active_db_sub, 'stripe_subscription_id', None) or \
                 getattr(active_db_sub, 'transaction_id', None)
        sub_id = str(sub_id or '').strip() or None

    # Fallback: user model attribute (may exist on some schema versions)
    if not sub_id:
        sub_id = str(getattr(user, 'stripe_subscription_id', '') or '').strip() or None

    if not sub_id:
        return {"case": "needs_new_sub", "stripe_sub": None, "stripe_sub_id": None}

    try:
        stripe_sub = StripeService.retrieve_subscription(sub_id)
        stripe_status = stripe_sub.get("status", "")
    except Exception as e:
        logger.warning(f"⚠️ Could not retrieve sub {sub_id} from Stripe: {e} — needs_new_sub")
        return {"case": "needs_new_sub", "stripe_sub": None, "stripe_sub_id": sub_id}

    if stripe_status not in ("active", "trialing"):
        logger.info(f"ℹ️ Sub {sub_id} status='{stripe_status}' in Stripe — needs_new_sub")
        return {"case": "needs_new_sub", "stripe_sub": stripe_sub, "stripe_sub_id": sub_id}

    # Stripe says active — check our DB.
    # A "fully_subscribed" result means we have a valid, non-expired active row
    # for this exact sub_id. Any other situation (expired row, wrong status, or
    # no row at all) routes to stripe_active_no_db, where _create_active_subscription_record
    # will safely UPSERT (UPDATE existing row or INSERT new row) without hitting
    # the unique constraint on transaction_id.
    valid_record = db.query(Subscriptions).filter(
        Subscriptions.user_id == user.id,
        Subscriptions.transaction_id == sub_id,
        Subscriptions.subscription_status == "active",
        Subscriptions.end_date > datetime.now(timezone.utc)
    ).first()

    if valid_record:
        logger.info(f"✅ Sub {sub_id} active in Stripe + valid DB record — fully_subscribed")
        return {"case": "fully_subscribed", "stripe_sub": stripe_sub, "stripe_sub_id": sub_id}

    # Row exists but expired/wrong-status, OR no row — route to adopt.
    # _create_active_subscription_record will UPDATE or INSERT safely.
    logger.info(
        f"⚠️ Sub {sub_id} active in Stripe, DB record missing or stale — "
        f"stripe_active_no_db (will upsert)"
    )
    return {"case": "stripe_active_no_db", "stripe_sub": stripe_sub, "stripe_sub_id": sub_id}


def get_subscription_dates_from_stripe(subscription_result: dict, plan_type: str):
    """Always prefer Stripe's authoritative period timestamps."""
    period_start = subscription_result.get("current_period_start")
    period_end = subscription_result.get("current_period_end")

    if not (period_start and period_end):
        latest_invoice = subscription_result.get("latest_invoice")
        if isinstance(latest_invoice, dict):
            for line in latest_invoice.get("lines", {}).get("data", []):
                if line.get("period"):
                    period_start = period_start or line["period"].get("start")
                    period_end = period_end or line["period"].get("end")
                    if period_start and period_end:
                        break

    if period_start and period_end:
        try:
            start_date = datetime.fromtimestamp(int(period_start))
            end_date = datetime.fromtimestamp(int(period_end))
            logger.info(f"📅 Stripe period: {start_date} → {end_date}")
            return start_date, end_date
        except (ValueError, TypeError, OverflowError) as e:
            logger.warning(f"⚠️ Could not parse Stripe timestamps: {e}")

    start = datetime.now(timezone.utc)
    delta_map = {"monthly": 30, "quarterly": 90, "yearly": 365}
    return start, start + timedelta(days=delta_map.get(plan_type, 30))


def _get_invoice_amount_and_currency(sub_result):
    """
    Read the actual charged amount and currency from the subscription's latest invoice.
    Falls back to (None, None) so callers can use their own default.

    Handles three cases:
    1. sub_result is a Stripe object with latest_invoice expanded
    2. sub_result is a dict with 'latest_invoice' key
    3. sub_result is a dict with only 'subscription_id'/'id' — fetches invoice from Stripe API
    """
    import os as _os
    api_key = _os.getenv("STRIPE_SECRET_KEY")
    try:
        li = None

        # Case 1 & 2: invoice already attached to the sub_result
        if hasattr(sub_result, 'latest_invoice'):
            li = sub_result.latest_invoice
        elif isinstance(sub_result, dict):
            li = sub_result.get('latest_invoice')

        # Case 3: sub_result is a dict with only the subscription ID — fetch from Stripe
        if not li:
            sub_id = None
            if isinstance(sub_result, dict):
                sub_id = sub_result.get('subscription_id') or sub_result.get('id')
            elif hasattr(sub_result, 'id'):
                sub_id = sub_result.id

            if sub_id:
                full_sub = stripe.Subscription.retrieve(
                    sub_id,
                    expand=['latest_invoice'],
                    api_key=api_key,
                )
                li = getattr(full_sub, 'latest_invoice', None)

        if not li:
            return None, None

        # li may be an invoice ID string — fetch the full object
        if isinstance(li, str):
            invoice = stripe.Invoice.retrieve(li, api_key=api_key)
        else:
            invoice = li

        # Try amount_paid first (basil API 2025-03-31 still supports it),
        # fall back to amount_due and total for edge cases where amount_paid = 0
        # on the initial creation webhook but the actual charge is non-zero.
        def _get(obj, *keys):
            for k in keys:
                v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
                if v is not None and v != 0:
                    return v
            # If all fields are 0, return 0 explicitly (free/trial invoice)
            for k in keys:
                v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
                if v is not None:
                    return v
            return None

        paid = _get(invoice, 'amount_paid', 'total', 'amount_due')
        currency = getattr(invoice, 'currency', None) or (invoice.get('currency') if isinstance(invoice, dict) else None)

        if paid is not None and currency:
            return round(paid / 100, 2), currency.upper()
    except Exception as e:
        logger.warning(f"[_get_invoice_amount_and_currency] could not read invoice: {e}")
    return None, None


def _create_active_subscription_record(db, user, sub_result, plan_type, amount, tx_ref_prefix="SUB"):
    """
    Upsert a Subscriptions DB record and update user fields for an active sub.
    Only call when status is 'active' or 'trialing'. Never for 'incomplete'.

    sub_result may be either:
      - Our StripeService dict  → has key "subscription_id"
      - A raw Stripe sub dict   → has key "id"

    UPSERT logic: if a row already exists for this transaction_id (e.g. a
    previously expired or incomplete record written by a webhook or prior
    session), UPDATE it in-place rather than INSERT. This prevents the
    unique constraint violation on ix_subscriptions_transaction_id.
    """
    sub_id = sub_result.get("subscription_id") or sub_result.get("id")
    start_date, end_date = get_subscription_dates_from_stripe(sub_result, plan_type)

    # Use the actual invoiced amount/currency so history reflects what was charged.
    actual_amount, actual_currency = _get_invoice_amount_and_currency(sub_result)
    record_amount = Decimal(str(actual_amount if actual_amount is not None else amount))
    record_currency = actual_currency or "USD"

    # Check for any existing row with this transaction_id (any status)
    existing = db.query(Subscriptions).filter(
        Subscriptions.transaction_id == sub_id
    ).first()

    if existing:
        # Update the existing row to active/completed with fresh dates
        existing.subscription_plan = plan_type
        existing.status = "completed"
        existing.subscription_status = "active"
        existing.amount = record_amount
        existing.currency = record_currency
        existing.start_date = start_date
        existing.end_date = end_date
        subscription = existing
        logger.info(f"♻️ Updated existing subscription record id={existing.id} for sub {sub_id}")
    else:
        subscription = Subscriptions(
            user_id=user.id,
            subscription_plan=plan_type,
            transaction_id=sub_id,
            tx_ref=generate_tx_ref(tx_ref_prefix),
            amount=record_amount,
            currency=record_currency,
            status="completed",
            subscription_status="active",
            payment_provider="stripe",
            start_date=start_date,
            end_date=end_date
        )
        db.add(subscription)

    db.flush()

    if hasattr(user, 'stripe_subscription_id'):
        user.stripe_subscription_id = sub_id
    if hasattr(user, 'subscription_status'):
        user.subscription_status = "active"
    if hasattr(user, 'subscription_plan'):
        user.subscription_plan = plan_type
    if hasattr(user, 'subscription_expires_at'):
        user.subscription_expires_at = end_date

    from subscriptions.commission_service import CommissionService
    CommissionService.calculate_commission(subscription=subscription, db=db)

    return subscription, end_date


# =============================================================================
# STRIPE CONFIG
# =============================================================================

@router.get("/config")
async def get_stripe_config():
    # Accept both env var names so Railway config is flexible
    publishable_key = (
        os.getenv("STRIPE_PUBLISHABLE_KEY") or
        os.getenv("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY")
    )
    if not publishable_key or not publishable_key.strip().startswith("pk_"):
        raise HTTPException(status_code=500, detail="Stripe publishable key not configured on backend")
    return {"publishableKey": publishable_key.strip()}


# Module-level price cache — prices change rarely; 10-minute TTL avoids
# 9 Stripe API round-trips on every page load.
_price_cache: dict = {}
_price_cache_at: float = 0.0
_PRICE_CACHE_TTL = 600  # 10 minutes


@router.get("/prices")
async def get_subscription_prices(current_user: User = Depends(get_current_user)):
    """
    Fetch live subscription prices from Stripe using the Price IDs stored in
    environment variables. Results are cached for 10 minutes so the 9 Stripe
    API calls only happen once per cache window instead of on every page load.

    Env var naming: STRIPE_{PLAN}_PRICE_ID_{CURRENCY}
    e.g. STRIPE_MONTHLY_PRICE_ID_USD, STRIPE_YEARLY_PRICE_ID_NGN
    """
    import time
    global _price_cache, _price_cache_at

    # Serve from cache if warm
    if _price_cache and time.time() - _price_cache_at < _PRICE_CACHE_TTL:
        logger.info("[prices] cache hit")
        return _price_cache

    api_key = os.getenv("STRIPE_SECRET_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured")

    plans = ["monthly", "quarterly", "yearly"]
    currencies = ["USD", "GBP", "NGN"]
    errors = {}
    result: dict = {}

    for currency in currencies:
        result[currency] = {}
        for plan in plans:
            env_key = f"STRIPE_{plan.upper()}_PRICE_ID_{currency}"
            price_id = os.getenv(env_key, "").strip()
            if not price_id:
                logger.warning(f"[prices] {env_key} not set")
                result[currency][plan] = None
                errors[env_key] = "not set"
                continue
            try:
                price_obj = stripe.Price.retrieve(price_id, api_key=api_key)
                unit_amount = getattr(price_obj, "unit_amount", None)
                if unit_amount is None:
                    unit_amount = price_obj["unit_amount"] if "unit_amount" in price_obj else 0
                result[currency][plan] = (unit_amount or 0) / 100
                logger.info(f"[prices] {env_key} ({price_id}) = {result[currency][plan]}")
            except Exception as e:
                logger.error(f"[prices] {env_key} ({price_id}): {e}")
                result[currency][plan] = None
                errors[env_key] = str(e)

    if errors:
        logger.warning(f"[prices] Some prices could not be fetched: {errors}")

    # Store in module cache only if we got at least some prices
    if any(v for curr in result.values() for v in curr.values()):
        _price_cache = result
        _price_cache_at = time.time()

    return result


# =============================================================================
# SUBSCRIPTION HISTORY
# =============================================================================

@router.get("/history", response_model=list[SubscriptionResponse])
async def get_subscription_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        subscriptions = db.query(Subscriptions).filter(
            Subscriptions.user_id == user_id
        ).order_by(Subscriptions.created_at.desc()).all()
        return subscriptions
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch subscription history")


# =============================================================================
# LEGACY PAYMENT INTENT (one-time charge — NOT subscriptions)
# =============================================================================

@router.post("/create-payment-intent", response_model=PaymentIntentResponse)
async def create_payment_intent(
    payment_data: PaymentIntentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """LEGACY: one-time charge only. Do NOT use for subscriptions."""
    try:
        user_id = extract_user_id(current_user)
        if int(payment_data.user_id) != user_id:
            raise HTTPException(status_code=403, detail="Unauthorized")
        tx_ref = generate_tx_ref("STRIPE")
        intent = StripeService.create_payment_intent(
            amount=payment_data.amount, currency="usd",
            customer_email=payment_data.email,
            metadata={
                "user_id": str(payment_data.user_id),
                "plan_type": payment_data.plan_type,
                "customer_name": payment_data.name,
                "tx_ref": tx_ref,
                "legacy_payment_intent": "true"
            }
        )
        return intent
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verify-payment", response_model=SubscriptionResponse)
async def verify_payment(
    payment_verify: PaymentVerify,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """LEGACY: verify a one-time PaymentIntent."""
    try:
        user_id = extract_user_id(current_user)
        if int(payment_verify.user_id) != user_id:
            raise HTTPException(status_code=403, detail="Unauthorized")
        verification = StripeService.verify_payment(payment_verify.payment_intent_id)
        if verification["status"] != "succeeded":
            raise HTTPException(status_code=400, detail=f"Payment not successful: {verification['status']}")
        existing_sub = db.query(Subscriptions).filter(
            Subscriptions.transaction_id == payment_verify.payment_intent_id
        ).first()
        if existing_sub:
            return existing_sub
        metadata = verification.get("metadata", {})
        plan_type = metadata.get("plan_type", "monthly")
        tx_ref = metadata.get("tx_ref", generate_tx_ref("STRIPE"))
        start_date = datetime.now(timezone.utc)
        delta_map = {"monthly": 30, "quarterly": 90, "yearly": 365}
        end_date = start_date + timedelta(days=delta_map.get(plan_type, 30))
        subscription = Subscriptions(
            user_id=payment_verify.user_id, subscription_plan=plan_type,
            transaction_id=payment_verify.payment_intent_id, tx_ref=tx_ref,
            amount=Decimal(str(verification.get("amount", 0))),
            currency=verification.get("currency", "USD").upper(),
            status="completed", subscription_status="active",
            payment_provider="stripe", start_date=start_date, end_date=end_date
        )
        db.add(subscription)
        db.flush()
        user = db.query(User).filter(User.id == payment_verify.user_id).first()
        if user:
            if hasattr(user, 'subscription_status'):
                user.subscription_status = "active"
            if hasattr(user, 'subscription_plan'):
                user.subscription_plan = plan_type
            if hasattr(user, 'subscription_expires_at'):
                user.subscription_expires_at = end_date
        from subscriptions.commission_service import CommissionService
        CommissionService.calculate_commission(subscription=subscription, db=db)
        db.commit()
        db.refresh(subscription)
        if user:
            background_tasks.add_task(
                email_service.send_payment_success_email,
                user.email, user.name, float(verification.get("amount", 0)),
                plan_type, end_date.strftime("%B %d, %Y")
            )
        return subscription
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# SAVE CARD / CHECKOUT
# =============================================================================

class SetupIntentRequest(BaseModel):
    currency: Optional[str] = "USD"


@router.post("/setup-intent")
async def create_setup_intent(
    request: SetupIntentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 1 of the SetupIntent card-save flow.

    Creates a Stripe SetupIntent for the authenticated user's customer record
    and returns the client_secret the frontend needs to call
    stripe.confirmCardSetup().  That call opens the bank's OTP popup, satisfies
    3DS, and attaches the payment method to the customer — all without a charge.

    Why this exists:
    - PaymentMethod.attach() does a silent card verification that Nigerian banks
      block because no OTP was presented to the cardholder.
    - confirmCardSetup() triggers the bank's OTP/3DS popup so the cardholder
      authenticates before any money moves, which most Nigerian banks require.
    """
    user_id = None
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Get or create Stripe customer (same logic as save-card-beta)
        customer_id = StripeService.get_or_create_customer(
            user_id=user_id, email=user.email, name=user.name,
            stripe_customer_id=getattr(user, 'stripe_customer_id', None)
        )

        # Persist customer_id in its own session so it survives any rollback
        if getattr(user, 'stripe_customer_id', None) != customer_id:
            try:
                with SessionLocal() as _s:
                    _s.query(User).filter(User.id == user_id).update(
                        {"stripe_customer_id": customer_id}
                    )
                    _s.commit()
                user.stripe_customer_id = customer_id
                logger.info(f"💾 Persisted stripe_customer_id for user {user_id}: {customer_id}")
            except Exception as _e:
                logger.warning(f"⚠️ Could not persist stripe_customer_id: {_e}")

        # Create the SetupIntent
        # usage='off_session' tells Stripe this card will be charged automatically
        # in the future (subscriptions), so the bank must authenticate it now.
        setup_intent = stripe.SetupIntent.create(
            customer=customer_id,
            usage="off_session",
            payment_method_types=["card"],
            api_key=os.getenv("STRIPE_SECRET_KEY"),
        )

        logger.info(
            f"🔐 SetupIntent {setup_intent.id} created for user {user_id} "
            f"(customer {customer_id})"
        )

        return {
            "client_secret": setup_intent.client_secret,
            "setup_intent_id": setup_intent.id,
            "customer_id": customer_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ SetupIntent creation failed for user {user_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/save-card-beta")
async def save_card_for_beta(
    request: SaveCardRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    BETA MODE  → save card only, no charge, mark user as beta user.
    LAUNCH MODE → resolve Stripe state (3 cases), bill appropriately.

    See resolve_stripe_subscription_state() for the three-case logic.
    The critical rule: NEVER call Subscription.create() when the customer
    already has an active sub in Stripe — even if our DB record is missing.
    """
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        app_mode = BetaService.get_app_mode()
        currency = get_currency_from_request(request)

        logger.info(
            f"💳 save-card-beta: user={user.email} (id={user_id}), "
            f"app_mode='{app_mode}', currency='{currency}', "
            f"is_beta_user={getattr(user, 'is_beta_user', False)}, "
            f"stripe_sub_id='{str(getattr(user, 'stripe_subscription_id', '') or '').strip() or 'none'}'"
        )

        # ── Attach card ───────────────────────────────────────────────────────
        customer_id = StripeService.get_or_create_customer(
            user_id=user_id, email=user.email, name=user.name,
            stripe_customer_id=getattr(user, 'stripe_customer_id', None)
        )
        # Always persist the customer_id in its own independent session so it
        # survives any rollback on the main request session (card decline,
        # pool recycle, etc.).  Using a separate SessionLocal guarantees a
        # separate DB connection and transaction that nothing in this request
        # can accidentally roll back.
        if getattr(user, 'stripe_customer_id', None) != customer_id:
            try:
                with SessionLocal() as _s:
                    _s.query(User).filter(User.id == user_id).update(
                        {"stripe_customer_id": customer_id}
                    )
                    _s.commit()
                # Also update the in-memory object so the rest of this request
                # sees the correct value without a DB round-trip.
                user.stripe_customer_id = customer_id
                logger.info(f"💾 Persisted stripe_customer_id for user {user_id}: {customer_id}")
            except Exception as _e:
                logger.warning(f"⚠️ Could not persist stripe_customer_id: {_e}")

        # NOTE: We deliberately do NOT call PaymentMethod.attach() here.
        # attach() sends a silent $0 card verification to the bank with no OTP —
        # CBN regulations require every Nigerian card transaction to have explicit
        # cardholder authentication, so banks decline silent verifications.
        # The card will be authenticated and attached by Stripe automatically when
        # the frontend calls stripe.confirmCardPayment() with the card element
        # inline (the path that banks accept). For off_session cron billing the
        # PM is already attached from a previous successful confirmation.

        # ── Save card metadata ────────────────────────────────────────────────
        _stripe_key = os.getenv("STRIPE_SECRET_KEY")
        payment_method = stripe.PaymentMethod.retrieve(
            request.payment_method_id, api_key=_stripe_key
        )
        user.stripe_payment_method_id = request.payment_method_id
        user.card_last4 = payment_method.card.last4
        user.card_brand = payment_method.card.brand
        user.card_exp_month = payment_method.card.exp_month
        user.card_exp_year = payment_method.card.exp_year
        user.card_saved_at = datetime.now(timezone.utc)

        # ── Strict plan / currency / amount validation ────────────────────────
        _valid_plans = {"monthly", "quarterly", "yearly"}
        requested_plan = (getattr(request, 'plan_type', None) or "").lower().strip()
        if requested_plan not in _valid_plans:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"plan_type '{requested_plan or '(empty)'}' is not valid. "
                    f"Must be one of: monthly, quarterly, yearly."
                )
            )

        # Resolve the Stripe price ID for this exact plan + currency combination.
        price_id = get_stripe_price_id(requested_plan, currency)
        if not price_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No Stripe price configured for plan='{requested_plan}' / "
                    f"currency='{currency}'. Contact support."
                )
            )

        # Fetch the real amount from Stripe — payment must not proceed if
        # the price is missing, zero, or misconfigured.
        try:
            charge_amount = get_amount_from_stripe_price(price_id)
        except Exception as e:
            logger.error(f"❌ Cannot fetch price amount for {price_id}: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Could not verify charge amount for price '{price_id}'. Payment blocked."
            )
        if charge_amount <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Stripe price '{price_id}' has a zero or negative amount. Payment blocked."
            )

        logger.info(
            f"[save-card] ✅ plan={requested_plan} currency={currency} "
            f"price_id={price_id} amount={charge_amount} user={user.id}"
        )
        if hasattr(user, 'subscription_plan'):
            user.subscription_plan = requested_plan

        # =====================================================================
        # BETA MODE — save card, no charge
        # =====================================================================
        if app_mode == "beta":
            if hasattr(user, 'is_beta_user'):
                user.is_beta_user = True
            BetaService.mark_as_beta_user(user, db)
            db.commit()
            logger.info(f"✅ Beta card saved for user {user.id} — no charge")
            background_tasks.add_task(
                email_service.send_beta_card_saved_email,
                user.email, user.name, user.card_last4, user.card_brand,
                BetaService.get_grace_period_days()
            )
            NotificationService.create_notification(
                db=db, user_id=user.id, type="card_saved",
                title="✅ Card Saved Successfully",
                message="Your card is securely saved. You'll be billed when we launch — no charge today!",
                link="/dashboard"
            )
            return {
                "status": "success",
                "message": "Card saved. You will be charged at launch.",
                "card_info": {
                    "last4": user.card_last4, "brand": user.card_brand,
                    "exp_month": user.card_exp_month, "exp_year": user.card_exp_year
                },
                "grace_period_days": BetaService.get_grace_period_days(),
                "grace_period_ends": user.grace_period_ends_at.isoformat() if user.grace_period_ends_at else None
            }

        # =====================================================================
        # LAUNCH MODE — three-case resolution
        # =====================================================================
        # price_id and charge_amount already validated and fetched above.
        plan_type = requested_plan
        amount = charge_amount


        state = resolve_stripe_subscription_state(user, db)
        logger.info(f"🔍 Stripe state for user {user.id}: case='{state['case']}'")

        # ── CASE A: Genuinely subscribed — card update only ───────────────────
        if state["case"] == "fully_subscribed":
            db.commit()
            logger.info(f"ℹ️ User {user.id} fully subscribed — card updated only")
            NotificationService.create_notification(
                db=db, user_id=user.id, type="card_updated",
                title="✅ Card Updated",
                message="Your payment card has been updated.",
                link="/dashboard"
            )
            return {
                "status": "success",
                "message": "Card updated. Your subscription remains active.",
                "card_info": {
                    "last4": user.card_last4, "brand": user.card_brand,
                    "exp_month": user.card_exp_month, "exp_year": user.card_exp_year
                }
            }

        # ── CASE B: Active in Stripe, missing DB record — adopt it ───────────
        # The Stripe subscription already exists and is paid. Our DB write never
        # completed in a previous session. Write the missing record now.
        # NEVER call Subscription.create() here — Stripe rejects with currency conflict.
        if state["case"] == "stripe_active_no_db":
            stripe_sub = state["stripe_sub"]
            subscription, end_date = _create_active_subscription_record(
                db=db, user=user, sub_result=stripe_sub,
                plan_type=plan_type, amount=amount, tx_ref_prefix="ADOPT"
            )
            db.commit()
            db.refresh(subscription)
            logger.info(
                f"✅ Adopted existing Stripe sub {state['stripe_sub_id']} "
                f"for user {user.id}, expires {end_date}"
            )
            background_tasks.add_task(
                email_service.send_payment_success_email,
                user.email, user.name, float(amount),
                plan_type, end_date.strftime("%B %d, %Y")
            )
            NotificationService.create_notification(
                db=db, user_id=user.id, type="subscription_active",
                title="🎉 Subscription Activated!",
                message=f"Your {plan_type} subscription is active until {end_date.strftime('%B %d, %Y')}.",
                link="/dashboard"
            )
            return {
                "status": "success",
                "message": "Subscription activated successfully.",
                "card_info": {
                    "last4": user.card_last4, "brand": user.card_brand,
                    "exp_month": user.card_exp_month, "exp_year": user.card_exp_year
                }
            }

        # ── CASE C: No active Stripe sub — create a fresh one ────────────────
        # Cancel any stale incomplete sub first so Stripe doesn't complain.
        stale_sub_id = state["stripe_sub_id"]
        if stale_sub_id:
            try:
                stale_status = (state["stripe_sub"] or {}).get("status", "")
                if stale_status == "incomplete":
                    stripe.Subscription.delete(stale_sub_id)
                    logger.info(f"🗑️ Cancelled stale incomplete sub {stale_sub_id}")
            except Exception as cancel_err:
                logger.warning(f"⚠️ Could not cancel stale sub {stale_sub_id}: {cancel_err}")
            if hasattr(user, 'stripe_subscription_id'):
                user.stripe_subscription_id = None

        logger.info(
            f"🚀 [LAUNCH] Creating new subscription for user {user.id} ({user.email}), "
            f"plan='{plan_type}', price='{price_id}'"
        )

        sub_result = StripeService.create_subscription_with_saved_card(
            customer_id=customer_id,
            price_id=price_id,
            payment_method_id=None,  # No pre-attach — frontend confirms inline
            metadata={
                "user_id": str(user.id),
                "plan_type": plan_type,
                "currency": currency,
                "source": "save_card_launch",
                "is_beta_user": str(getattr(user, 'is_beta_user', False))
            },
            off_session=False  # User is present — frontend confirms via OTP
        )

        sub_status = sub_result.get("status")
        logger.info(
            f"   Stripe result: status='{sub_status}', "
            f"sub_id='{sub_result.get('subscription_id')}', "
            f"has_client_secret={bool(sub_result.get('client_secret'))}"
        )

        # 3DS required — commit card save, return requires_action.
        # Do NOT create a DB record — the sub has no valid billing dates yet.
        # Frontend: stripe.confirmCardPayment(client_secret) → POST /confirm-subscription
        if sub_status == "incomplete":
            if not sub_result.get("client_secret"):
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "3DS required but Stripe did not return a client_secret. "
                        f"Sub ID: {sub_result.get('subscription_id', '?')}"
                    )
                )
            if hasattr(user, 'stripe_subscription_id'):
                user.stripe_subscription_id = sub_result["subscription_id"]
            db.commit()
            logger.info(f"🔐 3DS required for user {user.id}, sub={sub_result.get('subscription_id')}")
            return {
                "status": "requires_action",
                "subscription_id": sub_result["subscription_id"],
                "payment_intent_id": sub_result.get("payment_intent_id"),
                "client_secret": sub_result.get("client_secret"),
                "message": "Additional authentication required to complete payment."
            }

        # Subscription active immediately — write DB record
        if sub_status in ("active", "trialing"):
            subscription, end_date = _create_active_subscription_record(
                db=db, user=user, sub_result=sub_result,
                plan_type=plan_type, amount=amount, tx_ref_prefix="LAUNCH"
            )
            db.commit()
            db.refresh(subscription)
            logger.info(f"✅ New subscription active for user {user.id}, expires {end_date}")
            background_tasks.add_task(
                email_service.send_payment_success_email,
                user.email, user.name, float(amount),
                plan_type, end_date.strftime("%B %d, %Y")
            )
            NotificationService.create_notification(
                db=db, user_id=user.id, type="subscription_active",
                title="🎉 Subscription Activated!",
                message=f"Your {plan_type} subscription is active until {end_date.strftime('%B %d, %Y')}.",
                link="/dashboard"
            )
            return {
                "status": "success",
                "message": "Subscription activated successfully.",
                "card_info": {
                    "last4": user.card_last4, "brand": user.card_brand,
                    "exp_month": user.card_exp_month, "exp_year": user.card_exp_year
                }
            }

        raise HTTPException(
            status_code=400,
            detail=f"Unexpected Stripe subscription status: '{sub_status}'"
        )

    except HTTPException:
        db.rollback()
        raise
    except stripe.error.CardError as e:
        db.rollback()
        decline_code = getattr(e, 'code', None) or 'unknown'
        user_message = str(e.user_message) if hasattr(e, 'user_message') and e.user_message else str(e)
        logger.error(
            f"💳 Card declined for user {user_id} — "
            f"code={decline_code!r} message={user_message!r}"
        )
        # Create the failure notification in a clean state after rollback
        try:
            NotificationService.create_notification(
                db=db,
                user_id=user_id,
                type="payment_failed",
                title="❌ Payment Failed",
                message=f"Your payment was declined: {user_message}. Please check your card details and try again.",
                link="/dashboard/upgrade",
            )
            db.commit()
        except Exception as notif_err:
            logger.warning(f"⚠️ Could not save payment-failed notification: {notif_err}")
            db.rollback()
        raise HTTPException(status_code=400, detail=user_message)
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# BETA STATUS
# =============================================================================

@router.get("/beta/status")
async def get_beta_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        status = BetaService.get_user_status(user)

        if status.get("show_card_info") and user.card_last4:
            status["card_info"] = {
                "last4": user.card_last4, "brand": user.card_brand,
                "exp_month": user.card_exp_month, "exp_year": user.card_exp_year
            }

        status["is_beta_mode"] = BetaService.is_beta_mode()
        status["is_in_grace_period"] = BetaService.is_in_grace_period(user)
        return status
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# WEBHOOK
# =============================================================================

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="stripe-signature"),
    db: Session = Depends(get_db)
):
    try:
        payload = await request.body()

        is_production = os.getenv("ENVIRONMENT", "development") == "production"
        if not stripe_signature and not is_production:
            logger.warning("⚠️ Webhook: No signature — manual test mode")
            try:
                event_data = json.loads(payload)
                event = stripe.Event.construct_from(event_data, stripe.api_key)
            except Exception as e:
                raise HTTPException(status_code=400, detail="Invalid JSON payload")
        else:
            event = StripeService.verify_webhook_signature(payload, stripe_signature)

        logger.info(f"📨 Webhook: {event.type}")

        if event.type == "invoice.payment_succeeded":
            invoice = event.data.object

            # ----------------------------------------------------------------
            # Stripe API 2025-03-31 (basil) moved the subscription reference
            # out of invoice.subscription into invoice.parent.subscription_details.subscription
            # We try all known locations in order.
            # ----------------------------------------------------------------

            subscription_id = None
            payment_intent_id = getattr(invoice, 'payment_intent', None)

            # Location 1 (old API / direct field — may still be present)
            subscription_id = getattr(invoice, 'subscription', None) or None

            # Location 2 (basil API): invoice.parent.subscription_details.subscription
            if not subscription_id:
                parent = getattr(invoice, 'parent', None)
                if parent:
                    sub_details = getattr(parent, 'subscription_details', None)
                    if sub_details:
                        subscription_id = getattr(sub_details, 'subscription', None)
                        if subscription_id:
                            logger.info(f"ℹ️ subscription_id from invoice.parent.subscription_details: {subscription_id}")

            # Location 3: invoice.lines.data[].parent.subscription_item_details.subscription
            if not subscription_id:
                lines = getattr(getattr(invoice, 'lines', None), 'data', []) or []
                for line in lines:
                    line_parent = getattr(line, 'parent', None)
                    if line_parent:
                        sid_details = getattr(line_parent, 'subscription_item_details', None)
                        if sid_details:
                            subscription_id = getattr(sid_details, 'subscription', None)
                            if subscription_id:
                                logger.info(f"ℹ️ subscription_id from line item parent: {subscription_id}")
                                break

            # Location 4: metadata on line items (last resort)
            if not subscription_id:
                lines = getattr(getattr(invoice, 'lines', None), 'data', []) or []
                for line in lines:
                    meta = getattr(line, 'metadata', None)
                    if meta:
                        # StripeObject in v14+ does not have .get()
                        m_dict = meta.to_dict() if hasattr(meta, 'to_dict') else dict(meta)
                        sid = m_dict.get('subscription') or m_dict.get('subscription_id')
                        if sid:
                            subscription_id = sid
                            logger.info(f"ℹ️ subscription_id from line metadata: {subscription_id}")
                            break

            # Location 5: customer lookup fallback
            if not subscription_id:
                cid = getattr(invoice, 'customer', None)
                if cid:
                    try:
                        subs = stripe.Subscription.list(customer=cid, status="active", limit=1)
                        if subs and subs.data:
                            subscription_id = subs.data[0].id
                            logger.info(f"ℹ️ subscription_id resolved via customer {cid}: {subscription_id}")
                    except Exception as lookup_err:
                        logger.warning(f"⚠️ Could not resolve subscription via customer: {lookup_err}")

            if not subscription_id:
                logger.warning(
                    f"⚠️ invoice.payment_succeeded: no subscription_id found anywhere "                    f"(customer={getattr(invoice, 'customer', 'unknown')}, "                    f"payment_intent={payment_intent_id or 'none'}) — skipping"
                )
                return {"status": "success"}

            # basil API: payment_intent moved to invoice.payment_intent still exists
            # but may be null on test clocks. Fall back to the charge ID or invoice ID
            # for idempotency purposes so we don't skip real renewals.
            if not payment_intent_id:
                # Try getting it from the charges on the invoice
                charge_id = getattr(invoice, 'charge', None)
                if charge_id:
                    payment_intent_id = charge_id
                    logger.info(f"ℹ️ Using charge_id as transaction_id: {payment_intent_id}")

            if not payment_intent_id:
                # Use invoice ID itself — guaranteed unique, safe for idempotency
                invoice_id = getattr(invoice, 'id', None)
                if invoice_id:
                    payment_intent_id = invoice_id
                    logger.info(f"ℹ️ Using invoice_id as transaction_id: {payment_intent_id}")

            if not payment_intent_id:
                logger.warning(f"⚠️ No transaction identifier found for sub {subscription_id} — skipping")
                return {"status": "success"}

            # Retrieve subscription for period dates and metadata
            stripe_sub = stripe.Subscription.retrieve(subscription_id)

            # ----------------------------------------------------------------
            # Period dates — try 3 sources in order of reliability:
            # 1. invoice.lines.data[0].period  (most reliable in basil API)
            # 2. stripe_sub.current_period_start/end
            # 3. Calculated fallback from plan_type
            # The event data shows dates live in lines[0].period for basil API.
            # ----------------------------------------------------------------
            period_start = None
            period_end   = None

            # Source 1: line item period (basil API puts dates here)
            lines = getattr(getattr(invoice, 'lines', None), 'data', []) or []
            for line in lines:
                lp = getattr(line, 'period', None)
                if lp:
                    period_start = getattr(lp, 'start', None)
                    period_end   = getattr(lp, 'end',   None)
                if period_start and period_end:
                    logger.info(f"📅 Period from line item: {period_start} → {period_end}")
                    break

            # Source 2: subscription object
            if not (period_start and period_end):
                period_start = getattr(stripe_sub, 'current_period_start', None)
                period_end   = getattr(stripe_sub, 'current_period_end',   None)
                if period_start and period_end:
                    logger.info(f"📅 Period from subscription object: {period_start} → {period_end}")

            if period_start and period_end:
                start_date = datetime.fromtimestamp(int(period_start))
                end_date   = datetime.fromtimestamp(int(period_end))
            else:
                logger.warning(f"⚠️ Could not determine period for sub {subscription_id} — using fallback dates")
                start_date = datetime.now(timezone.utc)
                sub_meta_obj = getattr(stripe_sub, 'metadata', None)
                sub_meta_dict = (sub_meta_obj.to_dict() if hasattr(sub_meta_obj, 'to_dict') else dict(sub_meta_obj)) if sub_meta_obj else {}
                plan_fallback = sub_meta_dict.get("plan_type", "monthly")
                delta_map = {"monthly": 30, "quarterly": 90, "yearly": 365}
                end_date = start_date + timedelta(days=delta_map.get(plan_fallback, 30))

            logger.info(f"📅 Renewal period: {start_date.date()} → {end_date.date()}")

            # ----------------------------------------------------------------
            # Find user — 5 strategies, log which one succeeds.
            # The basil API puts user_id in line item metadata, so check there too.
            # ----------------------------------------------------------------
            # Strategy 1: look up via Subscriptions table (stripe_subscription_id
            # lives there, not on User — querying User.stripe_subscription_id
            # raises AttributeError if that column doesn't exist on the model)
            user = None
            # Subscriptions.transaction_id stores the Stripe subscription ID (sub_xxx).
            # There is no stripe_subscription_id column on this model.
            sub_record = db.query(Subscriptions).filter(
                Subscriptions.transaction_id == subscription_id
            ).first()
            if sub_record:
                user = db.query(User).filter(User.id == sub_record.user_id).first()
                if user:
                    logger.info(f"👤 User found via Subscriptions table: {user.email}")

            if not user:
                inv_meta = getattr(invoice, 'metadata', None)
                inv_meta_dict = (inv_meta.to_dict() if hasattr(inv_meta, 'to_dict') else dict(inv_meta)) if inv_meta else {}
                uid = inv_meta_dict.get("user_id")
                if uid:
                    user = db.query(User).filter(User.id == int(uid)).first()
                    if user:
                        logger.info(f"👤 User found via invoice metadata user_id={uid}: {user.email}")

            # basil API: user_id is in invoice.parent.subscription_details.metadata
            if not user:
                parent = getattr(invoice, 'parent', None)
                sub_details = getattr(parent, 'subscription_details', None) if parent else None
                parent_meta = getattr(sub_details, 'metadata', None) if sub_details else None
                uid = (parent_meta or {}).get("user_id") if parent_meta else None
                if uid:
                    user = db.query(User).filter(User.id == int(uid)).first()
                    if user:
                        logger.info(f"👤 User found via parent.subscription_details metadata user_id={uid}: {user.email}")

            # basil API: user_id is also in line item metadata
            if not user:
                for line in lines:
                    meta_obj = getattr(line, 'metadata', None)
                    meta_dict = (meta_obj.to_dict() if hasattr(meta_obj, 'to_dict') else dict(meta_obj)) if meta_obj else {}
                    uid = meta_dict.get("user_id")
                    if uid:
                        user = db.query(User).filter(User.id == int(uid)).first()
                        if user:
                            logger.info(f"👤 User found via line item metadata user_id={uid}: {user.email}")
                            break

            if not user:
                sub_meta_obj = getattr(stripe_sub, 'metadata', None)
                sub_meta_dict = (sub_meta_obj.to_dict() if hasattr(sub_meta_obj, 'to_dict') else dict(sub_meta_obj)) if sub_meta_obj else {}
                uid = sub_meta_dict.get("user_id")
                if uid:
                    user = db.query(User).filter(User.id == int(uid)).first()
                    if user:
                        logger.info(f"👤 User found via sub metadata user_id={uid}: {user.email}")
                        if hasattr(user, 'stripe_subscription_id'):
                            user.stripe_subscription_id = subscription_id

            if not user:
                cid = getattr(invoice, 'customer', None)
                if cid:
                    user = db.query(User).filter(User.stripe_customer_id == cid).first()
                    if user:
                        logger.info(f"👤 User found via customer_id {cid}: {user.email}")

            if not user:
                logger.warning(
                    f"⚠️ No user found for subscription {subscription_id} "                    f"(customer={getattr(invoice, 'customer', 'unknown')}) — skipping"
                )
                return {"status": "success"}

            if hasattr(user, 'stripe_subscription_id') and user.stripe_subscription_id != subscription_id:
                user.stripe_subscription_id = subscription_id

            # Idempotency — skip if this payment event was already recorded.
            # The direct API path stores transaction_id = subscription_id.
            # The webhook path falls back to the invoice_id when no
            # payment_intent exists. Check all three identifiers so that
            # a subscription created by the API and then confirmed by the
            # webhook does not produce a second row.
            ident_checks = [payment_intent_id]
            if subscription_id:
                ident_checks.append(subscription_id)
            existing = db.query(Subscriptions).filter(
                Subscriptions.transaction_id.in_([i for i in ident_checks if i])
            ).first()
            if existing:
                logger.info(
                    f"ℹ️ Subscription already recorded (matched on "
                    f"{existing.transaction_id}) — skipping"
                )
                return {"status": "success"}

            sub_meta_obj = getattr(stripe_sub, 'metadata', None)
            sub_meta = (sub_meta_obj.to_dict() if hasattr(sub_meta_obj, 'to_dict') else dict(sub_meta_obj)) if sub_meta_obj else {}
            plan_type = sub_meta.get("plan_type") or getattr(user, 'subscription_plan', None) or "monthly"

            user.subscription_status = "active"
            user.subscription_expires_at = end_date
            if hasattr(user, 'subscription_plan'):
                user.subscription_plan = plan_type

            # amount_paid can be 0 on the initial webhook before Stripe confirms
            # the charge.  Fall back to total/amount_due so the history record
            # always shows the real charged amount.
            def _invoice_val(obj, *keys):
                for k in keys:
                    v = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
                    if v:
                        return v
                return 0
            amount_paid = _invoice_val(invoice, 'amount_paid', 'total', 'amount_due')
            currency = getattr(invoice, 'currency', None) or 'usd'

            new_sub = Subscriptions(
                user_id=user.id, subscription_plan=plan_type,
                transaction_id=payment_intent_id,
                tx_ref=f"RENEW-{user.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                amount=Decimal(str(amount_paid / 100)),
                currency=currency.upper(),
                status="completed", subscription_status="active",
                payment_provider="stripe", start_date=start_date, end_date=end_date
            )
            db.add(new_sub)
            db.flush()

            from subscriptions.commission_service import CommissionService
            CommissionService.calculate_commission(subscription=new_sub, db=db)
            db.commit()
            logger.info(f"✅ Renewal recorded: user={user.email} (id={user.id}), plan={plan_type}, {start_date.date()} → {end_date.date()}")

            NotificationService.create_notification(
                db=db, user_id=user.id, type="subscription_renewed",
                title="✅ Subscription Renewed",
                message=f"Your {plan_type} subscription has been renewed until {end_date.strftime('%B %d, %Y')}.",
                link="/dashboard"
            )
            db.commit()

        elif event.type == "invoice.payment_failed":
            invoice = event.data.object

            # Basil API (2025-03-31): subscription moved to invoice.parent.subscription_details
            sub_id = getattr(invoice, 'subscription', None)

            if not sub_id:
                parent = getattr(invoice, 'parent', None)
                if parent:
                    sub_details = getattr(parent, 'subscription_details', None)
                    if sub_details:
                        sub_id = getattr(sub_details, 'subscription', None)

            if not sub_id:
                # Line item fallback
                lines = getattr(getattr(invoice, 'lines', None), 'data', []) or []
                for line in lines:
                    line_parent = getattr(line, 'parent', None)
                    if line_parent:
                        sid_details = getattr(line_parent, 'subscription_item_details', None)
                        if sid_details:
                            sub_id = getattr(sid_details, 'subscription', None)
                            if sub_id:
                                break

            # Find user — try Subscriptions table first, then customer ID
            user = None
            if sub_id:
                sub_record = db.query(Subscriptions).filter(
                    Subscriptions.stripe_subscription_id == sub_id
                ).first()
                if sub_record:
                    user = db.query(User).filter(User.id == sub_record.user_id).first()

            if not user:
                cid = getattr(invoice, 'customer', None)
                if cid:
                    user = db.query(User).filter(User.stripe_customer_id == cid).first()

            if user:
                logger.warning(f"⚠️ Payment failed for user {user.id}, sub {sub_id or 'unknown'}")
                NotificationService.create_notification(
                    db=db, user_id=user.id,
                    type="payment_failed",
                    title="⚠️ Payment Failed",
                    message="Your subscription payment failed. Please update your payment method to keep your access.",
                    link="/dashboard/upgrade"
                )
                db.commit()
            else:
                logger.warning(
                    f"⚠️ invoice.payment_failed: no user found "
                    f"(sub={sub_id}, customer={getattr(invoice, 'customer', 'unknown')})"
                )

        elif event.type == "customer.subscription.deleted":
            stripe_sub = event.data.object
            # User model has no stripe_subscription_id column — look up via Subscriptions table
            sub_record = db.query(Subscriptions).filter(
                Subscriptions.stripe_subscription_id == stripe_sub.id
            ).first()
            user = db.query(User).filter(User.id == sub_record.user_id).first() if sub_record else None
            if not user:
                cid = getattr(stripe_sub, 'customer', None)
                if cid:
                    user = db.query(User).filter(User.stripe_customer_id == cid).first()
            
            if user:
                user.subscription_status = "cancelled"
                if hasattr(user, 'stripe_subscription_id'):
                    user.stripe_subscription_id = None

                sub_record = db.query(Subscriptions).filter(
                    Subscriptions.user_id == user.id,
                    Subscriptions.subscription_status == "active"
                ).first()

                if sub_record:
                    sub_record.subscription_status = "cancelled"
                    sub_record.status = "cancelled"
                NotificationService.create_notification(
                    db=db, user_id=user.id, type="subscription_cancelled",
                    title="Subscription Cancelled",
                    message="Your subscription has been cancelled.",
                    link="/dashboard/upgrade"
                )
                db.commit()

        elif event.type == "customer.subscription.updated":
            stripe_sub = event.data.object
            user = None

            # Strategy 1: metadata user_id (most reliable — set by our API)
            sub_meta_obj = getattr(stripe_sub, 'metadata', None)
            sub_meta_dict = (sub_meta_obj.to_dict() if hasattr(sub_meta_obj, 'to_dict') else dict(sub_meta_obj)) if sub_meta_obj else {}
            uid = sub_meta_dict.get("user_id")
            if uid:
                user = db.query(User).filter(User.id == int(uid)).first()

            # Strategy 2: match via Subscriptions table (User has no stripe_subscription_id column)
            if not user:
                sub_rec = db.query(Subscriptions).filter(
                    Subscriptions.stripe_subscription_id == stripe_sub.id
                ).first()
                if sub_rec:
                    user = db.query(User).filter(User.id == sub_rec.user_id).first()

            # Strategy 3: customer_id fallback
            if not user and stripe_sub.customer:
                user = db.query(User).filter(
                    User.stripe_customer_id == stripe_sub.customer
                ).first()

            if user:
                status_map = {
                    "active": "active", "past_due": "past_due",
                    "unpaid": "unpaid", "canceled": "cancelled", "trialing": "active"
                }
                mapped = status_map.get(getattr(stripe_sub, 'status', ''))
                if mapped and hasattr(user, 'subscription_status'):
                    logger.info(
                        f"📋 subscription.updated: user={user.email}, "                        f"sub={stripe_sub.id}, status={stripe_sub.status} → {mapped}"
                    )
                    user.subscription_status = mapped
                db.commit()
            else:
                logger.info(
                    f"ℹ️ subscription.updated: no matching user for sub {stripe_sub.id} "                    f"(customer={getattr(stripe_sub, 'customer', 'unknown')}) — skipping"
                )

        elif event.type == "payment_intent.succeeded":
            payment_intent = event.data.object
            meta_obj = getattr(payment_intent, 'metadata', None)
            metadata = (meta_obj.to_dict() if hasattr(meta_obj, 'to_dict') else dict(meta_obj)) if meta_obj else {}
            if not metadata.get("legacy_payment_intent"):
                return {"status": "success"}
            existing = db.query(Subscriptions).filter(
                Subscriptions.transaction_id == payment_intent.id
            ).first()
            if existing:
                if existing.status != "completed":
                    existing.status = "completed"
                    db.commit()
                return {"status": "success"}
            user_id = int(metadata.get("user_id", 0))
            plan_type = metadata.get("plan_type", "monthly")
            tx_ref = metadata.get("tx_ref", generate_tx_ref("STRIPE"))
            if user_id:
                start = datetime.now(timezone.utc)
                delta_map = {"monthly": 30, "quarterly": 90, "yearly": 365}
                end = start + timedelta(days=delta_map.get(plan_type, 30))
                subscription = Subscriptions(
                    user_id=user_id, subscription_plan=plan_type,
                    transaction_id=payment_intent.id, tx_ref=tx_ref,
                    amount=Decimal(str(payment_intent.amount / 100)),
                    currency=payment_intent.currency.upper(),
                    status="completed", subscription_status="active",
                    payment_provider="stripe", start_date=start, end_date=end
                )
                db.add(subscription)
                db.flush()
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    if hasattr(user, 'subscription_status'):
                        user.subscription_status = "active"
                    if hasattr(user, 'subscription_plan'):
                        user.subscription_plan = plan_type
                    if hasattr(user, 'subscription_expires_at'):
                        user.subscription_expires_at = end
                from subscriptions.commission_service import CommissionService
                CommissionService.calculate_commission(subscription=subscription, db=db)
                db.commit()

        elif event.type == "payout.paid":
            handle_payout_paid(event, db)
        elif event.type in ("payout.failed", "payout.canceled"):
            handle_payout_failed(event, db)

        return {"status": "success"}

    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception as e:
        event_type = event.type if 'event' in locals() else 'unknown'
        logger.error(f"❌ Webhook error [{event_type}]: {str(e)}")
        traceback.print_exc()
        # Return 200 so Stripe does not keep retrying unhandled/unknown events.
        # Only signature failures warrant a 400.
        return {"status": "error", "detail": str(e)}


def handle_payout_paid(event: dict, db: Session):
    stripe_payout = event.data.object
    # Use getattr as StripeObject in v14+ does not have .get()
    metadata = getattr(stripe_payout, "metadata", {}) or {}
    internal_payout_id = metadata.get("stripe_connect_payout_id")
    if not internal_payout_id:
        return
    from database.pg_models import Payout
    from subscriptions.payout_service import PayoutService
    payout = db.query(Payout).get(internal_payout_id)
    if not payout or payout.status == "completed":
        return
    from fastapi import BackgroundTasks
    PayoutService.complete_stripe_payout(payout.id, BackgroundTasks(), "paid", db)


def handle_payout_failed(event: dict, db: Session):
    stripe_payout = event.data.object
    # Use getattr as StripeObject in v14+ does not have .get()
    metadata = getattr(stripe_payout, "metadata", {}) or {}
    internal_payout_id = metadata.get("stripe_connect_payout_id")
    if not internal_payout_id:
        return
    from subscriptions.payout_service import PayoutService
    PayoutService.reverse_payout(
        internal_payout_id,
        # Use getattr as StripeObject in v14+ does not have .get()
        getattr(stripe_payout, "failure_message", None) or "Stripe payout failed",
        db
    )


# =============================================================================
# CREATE SUBSCRIPTION WITH SAVED CARD (explicit checkout for returning users)
# =============================================================================

@router.post("/create-subscription-with-saved-card")
async def create_subscription_with_saved_card(
    request: CreateSubscriptionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        currency = get_currency_from_request(request)
        price_id = get_stripe_price_id(request.plan_type, currency)
        if not price_id:
            raise HTTPException(
                status_code=400,
                detail=f"Price not configured for plan '{request.plan_type}' / currency '{currency}'. "
                       f"Set STRIPE_{request.plan_type.upper()}_PRICE_ID_{currency} in environment."
            )

        customer_id = StripeService.get_or_create_customer(
            user_id=user_id, email=user.email, name=user.name,
            stripe_customer_id=getattr(user, 'stripe_customer_id', None)
        )
        if not getattr(user, 'stripe_customer_id', None) and hasattr(user, 'stripe_customer_id'):
            user.stripe_customer_id = customer_id
            db.commit()

        StripeService.attach_payment_method(
            payment_method_id=request.payment_method_id,
            customer_id=customer_id, set_as_default=True
        )

        # Get the real price amount directly from Stripe — transparent, no hardcoding
        amount = get_amount_from_stripe_price(price_id)

        state = resolve_stripe_subscription_state(user, db)
        logger.info(f"🔍 create-sub state for user {user.id}: case='{state['case']}'")

        # Fully subscribed — update plan only
        if state["case"] == "fully_subscribed":
            try:
                updated_sub = StripeService.update_subscription_price(
                    subscription_id=state["stripe_sub_id"],
                    new_price_id=price_id, prorate=True
                )
                sub_record = db.query(Subscriptions).filter(
                    Subscriptions.user_id == user_id,
                    Subscriptions.subscription_status == "active"
                ).first()
                if sub_record:
                    sub_record.subscription_plan = request.plan_type
                    period_end = updated_sub.get("current_period_end")
                    if period_end:
                        sub_record.end_date = datetime.fromtimestamp(period_end)
                    db.commit()
                return {"status": "active", "subscription_id": updated_sub["id"], "message": "Subscription updated"}
            except Exception:
                pass

        # Active in Stripe but missing DB — adopt
        if state["case"] == "stripe_active_no_db":
            subscription, end_date = _create_active_subscription_record(
                db=db, user=user, sub_result=state["stripe_sub"],
                plan_type=request.plan_type, amount=amount, tx_ref_prefix="ADOPT"
            )
            try:
                _sk = os.getenv("STRIPE_SECRET_KEY")
                pm = stripe.PaymentMethod.retrieve(request.payment_method_id, api_key=_sk)
                user.stripe_payment_method_id = request.payment_method_id
                user.card_last4 = pm.card.last4
                user.card_brand = pm.card.brand
                user.card_exp_month = pm.card.exp_month
                user.card_exp_year = pm.card.exp_year
                user.card_saved_at = datetime.now(timezone.utc)
            except Exception as card_err:
                logger.warning(f"⚠️ Could not save card details: {str(card_err)}")
            db.commit()
            db.refresh(subscription)
            background_tasks.add_task(
                email_service.send_payment_success_email,
                user.email, user.name, float(amount),
                request.plan_type, end_date.strftime("%B %d, %Y")
            )
            return {"status": "active", "subscription_id": state["stripe_sub_id"], "subscription": subscription}

        # Needs new sub — cancel stale incomplete first
        stale_sub_id = state["stripe_sub_id"]
        if stale_sub_id:
            try:
                stale_status = (state["stripe_sub"] or {}).get("status", "")
                if stale_status == "incomplete":
                    stripe.Subscription.delete(stale_sub_id)
                    logger.info(f"🗑️ Cancelled stale incomplete sub {stale_sub_id}")
                    if hasattr(user, 'stripe_subscription_id'):
                        user.stripe_subscription_id = None
            except Exception:
                pass

        tx_ref = generate_tx_ref("STRIPE-SUB")
        subscription_result = StripeService.create_subscription_with_saved_card(
            customer_id=customer_id, price_id=price_id,
            payment_method_id=request.payment_method_id,
            metadata={
                "user_id": str(user_id),
                "plan_type": request.plan_type,
                "currency": currency,
                "tx_ref": tx_ref
            }
        )

        if subscription_result["status"] == "active":
            if db.query(Subscriptions).filter(
                Subscriptions.transaction_id == subscription_result["subscription_id"]
            ).first():
                return {"status": "active", "subscription_id": subscription_result["subscription_id"]}

            subscription, end_date = _create_active_subscription_record(
                db=db, user=user, sub_result=subscription_result,
                plan_type=request.plan_type, amount=amount, tx_ref_prefix="STRIPE-SUB"
            )
            try:
                _sk = os.getenv("STRIPE_SECRET_KEY")
                pm = stripe.PaymentMethod.retrieve(request.payment_method_id, api_key=_sk)
                user.stripe_payment_method_id = request.payment_method_id
                user.card_last4 = pm.card.last4
                user.card_brand = pm.card.brand
                user.card_exp_month = pm.card.exp_month
                user.card_exp_year = pm.card.exp_year
                user.card_saved_at = datetime.now(timezone.utc)
            except Exception as card_err:
                logger.warning(f"⚠️ Could not save card details: {str(card_err)}")
            db.commit()
            db.refresh(subscription)
            background_tasks.add_task(
                email_service.send_payment_success_email,
                user.email, user.name, float(amount),
                request.plan_type, end_date.strftime("%B %d, %Y")
            )
            return {"status": "active", "subscription_id": subscription_result["subscription_id"], "subscription": subscription}

        elif subscription_result["status"] == "incomplete":
            if not subscription_result.get("client_secret"):
                raise HTTPException(status_code=500, detail="3DS required but client_secret missing")
            if hasattr(user, 'stripe_subscription_id'):
                user.stripe_subscription_id = subscription_result["subscription_id"]
            db.commit()
            return {
                "status": "requires_action",
                "subscription_id": subscription_result["subscription_id"],
                "payment_intent_id": subscription_result.get("payment_intent_id"),
                "client_secret": subscription_result.get("client_secret"),
                "message": "Additional authentication required"
            }

        raise HTTPException(status_code=400, detail=f"Unexpected status: {subscription_result['status']}")

    except HTTPException:
        db.rollback()
        raise
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# CONFIRM SUBSCRIPTION  (after 3DS authentication)
# =============================================================================

@router.post("/confirm-subscription")
async def confirm_subscription(
    request: ConfirmSubscriptionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Creates the Subscriptions DB record after 3DS succeeds.
    Called by the frontend after stripe.confirmCardPayment() resolves.
    Works for both save-card-beta and create-subscription-with-saved-card flows.
    """
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        verification = StripeService.verify_payment(request.payment_intent_id)
        if verification["status"] != "succeeded":
            raise HTTPException(
                status_code=400,
                detail=f"Payment not confirmed. Status: {verification['status']}"
            )

        subscription_details = StripeService.retrieve_subscription(request.subscription_id)
        if subscription_details["status"] != "active":
            raise HTTPException(
                status_code=400,
                detail=f"Subscription not active after 3DS. Status: {subscription_details['status']}"
            )

        sub_meta = subscription_details.get('metadata') or {}
        plan_type = (
            sub_meta.get("plan_type")
            or verification.get("metadata", {}).get("plan_type")
            or getattr(user, 'subscription_plan', None)
            or "monthly"
        )
        tx_ref = verification.get("metadata", {}).get("tx_ref") or generate_tx_ref("STRIPE-CONFIRM")

        logger.info(
            f"✅ confirm-subscription: user={user.email}, "
            f"sub={request.subscription_id}, plan='{plan_type}'"
        )

        from api.routes.control.settings import get_settings
        settings = get_settings(db=db, current_user=user)
        price_map = {
            "monthly": settings.get("monthly_price") or 29.95,
            "quarterly": settings.get("quarterly_price") or 79.95,
            "yearly": settings.get("yearly_price") or 299.95
        }
        amount = price_map.get(plan_type, 29.95)
        start_date, end_date = get_subscription_dates_from_stripe(subscription_details, plan_type)

        existing = db.query(Subscriptions).filter(
            Subscriptions.transaction_id == request.subscription_id
        ).first()

        if existing:
            existing.subscription_status = "active"
            existing.status = "completed"
            existing.start_date = start_date
            existing.end_date = end_date
            subscription = existing
        else:
            subscription = Subscriptions(
                user_id=user_id, subscription_plan=plan_type,
                transaction_id=request.subscription_id, tx_ref=tx_ref,
                amount=Decimal(str(amount)), currency="USD",
                status="completed", subscription_status="active",
                payment_provider="stripe", start_date=start_date, end_date=end_date
            )
            db.add(subscription)
        db.flush()

        if hasattr(user, 'subscription_status'):
            user.subscription_status = "active"
        if hasattr(user, 'subscription_plan'):
            user.subscription_plan = plan_type
        if hasattr(user, 'subscription_expires_at'):
            user.subscription_expires_at = end_date
        if hasattr(user, 'stripe_subscription_id'):
            user.stripe_subscription_id = request.subscription_id

        try:
            pm_id = verification.get("payment_method")
            if pm_id:
                pm = stripe.PaymentMethod.retrieve(pm_id, api_key=os.getenv("STRIPE_SECRET_KEY"))
                user.stripe_payment_method_id = pm_id
                user.card_last4 = pm.card.last4
                user.card_brand = pm.card.brand
                user.card_exp_month = pm.card.exp_month
                user.card_exp_year = pm.card.exp_year
                user.card_saved_at = datetime.now(timezone.utc)
        except Exception as card_err:
            logger.warning(f"⚠️ Could not save card details: {str(card_err)}")

        if not existing:
            from subscriptions.commission_service import CommissionService
            CommissionService.calculate_commission(subscription=subscription, db=db)

        db.commit()
        db.refresh(subscription)

        background_tasks.add_task(
            email_service.send_payment_success_email,
            user.email, user.name, float(amount),
            plan_type, end_date.strftime("%B %d, %Y")
        )
        NotificationService.create_notification(
            db=db, user_id=user_id, type="subscription_active",
            title="🎉 Subscription Activated!",
            message=f"Your subscription is now active until {end_date.strftime('%B %d, %Y')}.",
            link="/dashboard"
        )
        return {"status": "success", "subscription": subscription}

    except HTTPException:
        db.rollback()
        raise
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# CANCEL / UPDATE / MANAGE
# =============================================================================

@router.post("/cancel-subscription")
async def cancel_subscription_endpoint(
    at_period_end: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        sub_id = str(getattr(user, 'stripe_subscription_id', '') or '').strip()
        if not sub_id:
            raise HTTPException(status_code=404, detail="No active subscription found")
        result = StripeService.cancel_subscription(subscription_id=sub_id, at_period_end=at_period_end)
        sub_record = db.query(Subscriptions).filter(
            Subscriptions.user_id == user_id, Subscriptions.subscription_status == "active"
        ).first()
        if sub_record:
            sub_record.subscription_status = "canceling" if at_period_end else "cancelled"
            if not at_period_end:
                sub_record.status = "cancelled"
        if hasattr(user, 'subscription_status'):
            user.subscription_status = "canceling" if at_period_end else "cancelled"
        db.commit()
        return {
            "status": "success",
            "message": "Subscription cancelled" + (" at period end" if at_period_end else " immediately"),
            "cancel_at_period_end": result["cancel_at_period_end"]
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/update-payment-method")
async def update_payment_method(
    request: UpdatePaymentMethodRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not getattr(user, 'stripe_customer_id', None):
            raise HTTPException(status_code=404, detail="No Stripe customer found")
        StripeService.attach_payment_method(
            payment_method_id=request.payment_method_id,
            customer_id=user.stripe_customer_id, set_as_default=True
        )
        return {"status": "success", "message": "Payment method updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/payment-methods")
async def get_payment_methods(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not getattr(user, 'stripe_customer_id', None):
            return {"payment_methods": []}
        payment_methods = StripeService.get_customer_payment_methods(user.stripe_customer_id)
        return {"payment_methods": payment_methods}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/subscription/{user_id}")
async def get_user_subscription(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    current_user_id = extract_user_id(current_user)
    if user_id != current_user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    subscription = db.query(Subscriptions).filter(
        Subscriptions.user_id == user_id,
        Subscriptions.status == "completed",
        Subscriptions.end_date > datetime.now(timezone.utc)
    ).order_by(Subscriptions.created_at.desc()).first()
    if not subscription:
        return {"message": "No active subscription found"}
    return subscription


@router.post("/remove-card")
async def remove_card(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not getattr(user, 'stripe_payment_method_id', None):
            raise HTTPException(status_code=400, detail="No saved card found")
        try:
            StripeService.detach_payment_method(user.stripe_payment_method_id)
        except Exception as e:
            logger.warning(f"⚠️ Could not detach from Stripe: {str(e)}")
        user.stripe_payment_method_id = None
        user.card_last4 = None
        user.card_brand = None
        user.card_exp_month = None
        user.card_exp_year = None
        user.card_saved_at = None
        db.commit()
        return {"status": "success", "message": "Card removed successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# PAYMENT FAILED NOTIFY
# Called by the frontend when 3DS authentication fails or is declined.
# Creates an in-app notification so the user knows their payment didn't go
# through and can try again with a different card.
# =============================================================================

class PaymentFailedNotifyRequest(BaseModel):
    subscription_id: str
    error_message: str = "Payment authentication failed"


@router.post("/payment-failed-notify")
async def payment_failed_notify(
    request: PaymentFailedNotifyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Called by the frontend after a 3DS authentication failure or card decline.
    Records an in-app notification so the user sees what happened.
    Also cleans up the incomplete subscription from Stripe so it doesn't
    block future checkout attempts.
    """
    try:
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        logger.warning(
            f"⚠️ 3DS payment failed: user={user.email} (id={user_id}), "
            f"sub={request.subscription_id}, error='{request.error_message}'"
        )

        # Cancel the incomplete Stripe subscription so it doesn't linger
        # and block future checkout attempts (Stripe cancels it after 23h anyway,
        # but cleaning up immediately prevents edge cases)
        if request.subscription_id:
            try:
                stripe.Subscription.delete(request.subscription_id)
                logger.info(f"🗑️ Cancelled incomplete sub {request.subscription_id} after 3DS failure")
            except Exception as cancel_err:
                # Don't block notification on cancel failure — sub may already be gone
                logger.warning(f"⚠️ Could not cancel sub {request.subscription_id}: {cancel_err}")

            # Clear the sub ID from user record so next checkout starts fresh
            if (
                hasattr(user, 'stripe_subscription_id')
                and user.stripe_subscription_id == request.subscription_id
            ):
                user.stripe_subscription_id = None
                db.flush()

        NotificationService.create_notification(
            db=db, user_id=user_id,
            type="payment_failed",
            title="⚠️ Payment Failed",
            message=(
                "Your payment could not be completed. "
                "Please try again with a different card or contact your bank."
            ),
            link="/dashboard/upgrade"
        )

        db.commit()
        return {"status": "success"}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"❌ payment-failed-notify error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))