
# api/routes/stripe_connect.py
"""
Stripe Connect integration for user payouts
"""
import logging
import traceback
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from pydantic import BaseModel
from sqlalchemy.orm import Session
import stripe

from database.pg_connections import get_db
from database.pg_models import User, PayoutAccount
from api.routes.auth.login import get_current_user
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stripe/connect", tags=["stripe-connect"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

BASE_URL = (
    os.getenv("BASE_URL")
    or os.getenv("FRONTEND_URL")
    or "https://lavooai.com"
)

logger.info(f"[Stripe Connect] BASE_URL resolved to: {BASE_URL}")


class OnboardRequest(BaseModel):
    country: str = "US"


def extract_user_from_token(current_user):
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                return user_data
            elif hasattr(user_data, "id"):
                return {"id": user_data.id, "email": user_data.email, "name": user_data.name}
        return current_user
    return {"id": current_user.id, "email": current_user.email, "name": current_user.name}


def _search_stripe_account_by_user(user_id) -> str | None:
    """
    Search Stripe for an Express account whose metadata.user_id matches.
    Returns the account ID string, or None if not found.
    StripeObject does not support dict() coercion — iterate keys explicitly.
    """
    try:
        for acc in stripe.Account.list(limit=100, api_key=os.getenv("STRIPE_SECRET_KEY")).auto_paging_iter():
            meta: dict = {}
            try:
                if acc.metadata:
                    for k in acc.metadata.keys():
                        try:
                            meta[k] = acc.metadata[k]
                        except Exception:
                            pass
            except Exception:
                pass
            if str(meta.get("user_id")) == str(user_id):
                return acc.id
    except stripe.error.StripeError as e:
        logger.warning(f"[Stripe Connect] account search by user_id failed: {e}")
    return None


@router.post("/onboard")
async def create_stripe_connect_account(
    body: OnboardRequest = Body(default_factory=OnboardRequest),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Generate a Stripe Connect hosted onboarding URL.

    The PayoutAccount DB row is NOT created here.  It is created (or updated)
    only when the webhook confirms that the user has fully completed onboarding
    (details_submitted=True AND charges_enabled=True AND payouts_enabled=True).
    This prevents a partially-completed flow from appearing as active to the user.
    """
    user_data = extract_user_from_token(current_user)
    user_id = user_data.get("id")
    user_email = user_data.get("email")
    country = (body.country or "US").upper().strip()

    logger.info(f"[Stripe Connect /onboard] user_id={user_id} email={user_email} country={country}")

    try:
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        stripe_account_id = None

        # ── Already fully verified ────────────────────────────────────────────
        if payout_account and payout_account.stripe_account_id and payout_account.is_verified:
            logger.info(
                f"[Stripe Connect /onboard] account already verified for user_id={user_id}"
            )
            return {
                "status": "already_connected",
                "message": "Stripe account already connected",
                "account_id": payout_account.stripe_account_id,
            }

        # ── DB row exists with a stripe_account_id (incomplete from old flow) ─
        if payout_account and payout_account.stripe_account_id:
            stripe_account_id = payout_account.stripe_account_id
            try:
                account = stripe.Account.retrieve(
                    stripe_account_id, api_key=os.getenv("STRIPE_SECRET_KEY")
                )
                if account.details_submitted:
                    logger.info(
                        f"[Stripe Connect /onboard] account {stripe_account_id} already "
                        f"fully onboarded (details_submitted=True)"
                    )
                    return {
                        "status": "already_connected",
                        "message": "Stripe account already connected",
                        "account_id": stripe_account_id,
                    }
                logger.info(
                    f"[Stripe Connect /onboard] resuming incomplete onboarding "
                    f"for existing account {stripe_account_id}"
                )
            except stripe.error.InvalidRequestError as e:
                logger.warning(
                    f"[Stripe Connect /onboard] stored account {stripe_account_id} is "
                    f"invalid in Stripe — will search/create: {e}"
                )
                stripe_account_id = None

        # ── No usable account yet — search Stripe by metadata then create ─────
        if not stripe_account_id:
            found_id = _search_stripe_account_by_user(user_id)
            if found_id:
                stripe_account_id = found_id
                logger.info(
                    f"[Stripe Connect /onboard] found existing Stripe account "
                    f"{stripe_account_id} for user_id={user_id} — resuming"
                )
            else:
                # Create a brand-new Express account.
                # NOTE: we do NOT write to the DB here.  The webhook is the
                # single source of truth for DB registration.
                logger.info(
                    f"[Stripe Connect /onboard] creating new Stripe Express account "
                    f"for user_id={user_id} email={user_email}"
                )
                try:
                    new_account = stripe.Account.create(
                        type="express",
                        country=country,
                        email=user_email,
                        capabilities={
                            "card_payments": {"requested": True},
                            "transfers": {"requested": True},
                        },
                        business_type="individual",
                        metadata={
                            "user_id": str(user_id),
                            "platform": "Lavoo Business Decision Engine",
                        },
                        api_key=os.getenv("STRIPE_SECRET_KEY"),
                    )
                except stripe.error.StripeError as e:
                    logger.error(
                        f"[Stripe Connect /onboard] stripe.Account.create failed for "
                        f"user_id={user_id}: {e}\n{traceback.format_exc()}"
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to create Stripe account: {e.user_message or str(e)}",
                    )

                stripe_account_id = new_account.id
                logger.info(
                    f"[Stripe Connect /onboard] new Stripe account {stripe_account_id} "
                    f"created for user_id={user_id} — DB row will be created by webhook "
                    f"upon successful completion"
                )

        # ── Generate the hosted onboarding link ───────────────────────────────
        return_url = f"{BASE_URL}/dashboard/upgrade?stripe_connect=success"
        refresh_url = f"{BASE_URL}/dashboard/upgrade?stripe_connect=refresh"
        logger.info(
            f"[Stripe Connect /onboard] creating AccountLink for {stripe_account_id}"
        )

        try:
            account_link = stripe.AccountLink.create(
                account=stripe_account_id,
                refresh_url=refresh_url,
                return_url=return_url,
                type="account_onboarding",
                api_key=os.getenv("STRIPE_SECRET_KEY"),
            )
        except stripe.error.StripeError as e:
            logger.error(
                f"[Stripe Connect /onboard] AccountLink.create failed for "
                f"account={stripe_account_id}: {e}\n{traceback.format_exc()}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Failed to generate onboarding link: {e.user_message or str(e)}",
            )

        logger.info(
            f"[Stripe Connect /onboard] onboarding URL generated for {stripe_account_id}"
        )
        return {
            "status": "success",
            "onboarding_url": account_link.url,
            "account_id": stripe_account_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[Stripe Connect /onboard] unexpected error for user_id={user_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.get("/account-status")
async def get_stripe_account_status(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_data = extract_user_from_token(current_user)
    user_id = user_data.get("id")
    logger.info(f"[Stripe Connect /account-status] user_id={user_id}")

    try:
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        if not payout_account or not payout_account.stripe_account_id:
            logger.info(
                f"[Stripe Connect /account-status] no Stripe account for user_id={user_id}"
            )
            return {"status": "not_connected", "message": "No Stripe account connected"}

        account_id = payout_account.stripe_account_id
        logger.info(
            f"[Stripe Connect /account-status] retrieving Stripe account {account_id}"
        )

        try:
            account = stripe.Account.retrieve(
                account_id, api_key=os.getenv("STRIPE_SECRET_KEY")
            )
        except stripe.error.StripeError as e:
            logger.error(
                f"[Stripe Connect /account-status] Account.retrieve failed for "
                f"{account_id}: {e}\n{traceback.format_exc()}"
            )
            raise HTTPException(status_code=400, detail=f"Stripe error: {e.user_message or str(e)}")

        if account.details_submitted and account.charges_enabled and account.payouts_enabled:
            payout_account.stripe_account_status = "verified"
            payout_account.is_verified = True
            payout_account.verified_at = datetime.now(timezone.utc)
        elif account.charges_enabled:
            payout_account.stripe_account_status = "active"
            payout_account.is_verified = False
        else:
            payout_account.stripe_account_status = "pending"
            payout_account.is_verified = False

        try:
            db.commit()
        except Exception as db_err:
            db.rollback()
            logger.error(
                f"[Stripe Connect /account-status] DB commit failed: "
                f"{db_err}\n{traceback.format_exc()}"
            )

        logger.info(
            f"[Stripe Connect /account-status] account {account_id} status="
            f"{payout_account.stripe_account_status} details_submitted={account.details_submitted}"
        )
        return {
            "status": "connected",
            "account_id": account_id,
            "account_status": payout_account.stripe_account_status,
            "details_submitted": account.details_submitted,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[Stripe Connect /account-status] unexpected error for user_id={user_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.post("/refresh-onboarding")
async def refresh_stripe_onboarding(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_data = extract_user_from_token(current_user)
    user_id = user_data.get("id")
    logger.info(f"[Stripe Connect /refresh-onboarding] user_id={user_id}")

    try:
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        # Resolve stripe_account_id from DB row or by searching Stripe metadata.
        # A user may have started onboarding but not yet completed it, so there
        # may be no DB row — the Stripe account exists but is awaiting webhook confirmation.
        account_id = None
        if payout_account and payout_account.stripe_account_id:
            account_id = payout_account.stripe_account_id
            logger.info(
                f"[Stripe Connect /refresh-onboarding] using account_id={account_id} from DB"
            )
        else:
            logger.info(
                f"[Stripe Connect /refresh-onboarding] no DB row — searching Stripe "
                f"for user_id={user_id}"
            )
            account_id = _search_stripe_account_by_user(user_id)

        if not account_id:
            logger.warning(
                f"[Stripe Connect /refresh-onboarding] no Stripe account found for "
                f"user_id={user_id}"
            )
            raise HTTPException(
                status_code=404,
                detail="No Stripe account found. Please start onboarding first.",
            )

        return_url = f"{BASE_URL}/dashboard/upgrade?stripe_connect=success"
        refresh_url = f"{BASE_URL}/dashboard/upgrade?stripe_connect=refresh"
        logger.info(
            f"[Stripe Connect /refresh-onboarding] creating AccountLink for {account_id}"
        )

        try:
            account_link = stripe.AccountLink.create(
                account=account_id,
                refresh_url=refresh_url,
                return_url=return_url,
                type="account_onboarding",
                api_key=os.getenv("STRIPE_SECRET_KEY"),
            )
        except stripe.error.StripeError as e:
            logger.error(
                f"[Stripe Connect /refresh-onboarding] AccountLink.create failed for "
                f"{account_id}: {e}\n{traceback.format_exc()}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Failed to generate onboarding link: {e.user_message or str(e)}",
            )

        logger.info(
            f"[Stripe Connect /refresh-onboarding] URL generated for {account_id}"
        )
        return {"status": "success", "onboarding_url": account_link.url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[Stripe Connect /refresh-onboarding] unexpected error for user_id={user_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.get("/dashboard-link")
async def get_stripe_dashboard_link(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return a Stripe Express Dashboard login link so the user can view their account."""
    user_data = extract_user_from_token(current_user)
    user_id = user_data.get("id")
    logger.info(f"[Stripe Connect /dashboard-link] user_id={user_id}")

    try:
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        if not payout_account or not payout_account.stripe_account_id:
            raise HTTPException(status_code=404, detail="No Stripe account connected")

        account_id = payout_account.stripe_account_id
        try:
            login_link = stripe.Account.create_login_link(
                account_id, api_key=os.getenv("STRIPE_SECRET_KEY")
            )
        except stripe.error.StripeError as e:
            logger.error(
                f"[Stripe Connect /dashboard-link] create_login_link failed for "
                f"{account_id}: {e}\n{traceback.format_exc()}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Could not generate dashboard link: {e.user_message or str(e)}",
            )

        logger.info(f"[Stripe Connect /dashboard-link] link generated for {account_id}")
        return {"status": "success", "url": login_link.url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[Stripe Connect /dashboard-link] unexpected error for user_id={user_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.delete("/disconnect")
async def disconnect_stripe_account(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_data = extract_user_from_token(current_user)
    user_id = user_data.get("id")
    logger.info(f"[Stripe Connect /disconnect] user_id={user_id}")

    try:
        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.user_id == user_id
        ).first()

        if not payout_account or not payout_account.stripe_account_id:
            raise HTTPException(status_code=404, detail="No Stripe account to disconnect")

        account_id = payout_account.stripe_account_id
        logger.info(f"[Stripe Connect /disconnect] deleting Stripe account {account_id}")

        try:
            stripe.Account.delete(account_id, api_key=os.getenv("STRIPE_SECRET_KEY"))
            logger.info(f"[Stripe Connect /disconnect] Stripe account {account_id} deleted")
        except stripe.error.InvalidRequestError as e:
            logger.warning(
                f"[Stripe Connect /disconnect] account {account_id} already gone in Stripe: {e}"
            )

        payout_account.stripe_account_id = None
        payout_account.stripe_account_status = None

        # Use the Python attribute `payment_method`, not the DB column name
        if payout_account.payment_method == "stripe":
            payout_account.payment_method = None
            payout_account.is_verified = False
            payout_account.verified_at = None

        try:
            db.commit()
            logger.info(
                f"[Stripe Connect /disconnect] DB updated — account {account_id} cleared"
            )
        except Exception as db_err:
            db.rollback()
            logger.error(
                f"[Stripe Connect /disconnect] DB commit failed: "
                f"{db_err}\n{traceback.format_exc()}"
            )
            raise HTTPException(status_code=500, detail=f"Database error: {db_err}")

        return {"status": "success", "message": "Stripe account disconnected successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(
            f"[Stripe Connect /disconnect] unexpected error for user_id={user_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@router.post("/webhook")
async def stripe_connect_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle Stripe Connect webhooks (account.updated, etc.)"""
    try:
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")
        webhook_secret = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET")

        if webhook_secret:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            except stripe.error.SignatureVerificationError as e:
                logger.error(
                    f"[Stripe Connect /webhook] signature verification failed: "
                    f"{e}\n{traceback.format_exc()}"
                )
                raise HTTPException(status_code=400, detail="Invalid webhook signature")
        else:
            logger.warning(
                "[Stripe Connect /webhook] STRIPE_CONNECT_WEBHOOK_SECRET not set — "
                "skipping signature verification (unsafe in production)"
            )
            event_data = json.loads(payload)
            event = stripe.Event.construct_from(event_data, stripe.api_key)

        logger.info(f"[Stripe Connect /webhook] received event type={event.type}")

        if event.type == "account.updated":
            account = event.data.object
            account_id = account.id
            all_complete = (
                account.details_submitted
                and account.charges_enabled
                and account.payouts_enabled
            )
            logger.info(
                f"[Stripe Connect /webhook] account.updated for {account_id} "
                f"charges_enabled={account.charges_enabled} "
                f"payouts_enabled={account.payouts_enabled} "
                f"details_submitted={account.details_submitted} "
                f"all_complete={all_complete}"
            )

            payout_account = db.query(PayoutAccount).filter(
                PayoutAccount.stripe_account_id == account_id
            ).first()

            if all_complete:
                if not payout_account:
                    # Onboarding completed but no DB row exists yet — this is the
                    # expected path for new signups.  Resolve user_id from Stripe
                    # account metadata and create the row now.
                    meta: dict = {}
                    try:
                        if account.metadata:
                            for k in account.metadata.keys():
                                try:
                                    meta[k] = account.metadata[k]
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    user_id_str = meta.get("user_id")
                    if not user_id_str:
                        logger.error(
                            f"[Stripe Connect /webhook] account {account_id} has no "
                            f"user_id in metadata — cannot create PayoutAccount row"
                        )
                    else:
                        try:
                            uid = int(user_id_str)
                        except (ValueError, TypeError):
                            logger.error(
                                f"[Stripe Connect /webhook] invalid user_id in metadata: "
                                f"{user_id_str!r}"
                            )
                            uid = None

                        if uid is not None:
                            # Check for an existing row by user_id (e.g. bank-only account)
                            existing = db.query(PayoutAccount).filter(
                                PayoutAccount.user_id == uid
                            ).first()
                            if existing:
                                existing.stripe_account_id = account_id
                                existing.payment_method = "stripe"
                                existing.stripe_account_status = "verified"
                                existing.is_verified = True
                                existing.verified_at = datetime.now(timezone.utc)
                                existing.updated_at = datetime.now(timezone.utc)
                                payout_account = existing
                                logger.info(
                                    f"[Stripe Connect /webhook] updated existing PayoutAccount "
                                    f"for user_id={uid} with stripe_account_id={account_id}"
                                )
                            else:
                                payout_account = PayoutAccount(
                                    user_id=uid,
                                    payment_method="stripe",
                                    stripe_account_id=account_id,
                                    stripe_account_status="verified",
                                    is_verified=True,
                                    verified_at=datetime.now(timezone.utc),
                                )
                                db.add(payout_account)
                                logger.info(
                                    f"[Stripe Connect /webhook] created PayoutAccount for "
                                    f"user_id={uid} account={account_id}"
                                )
                else:
                    # Row already exists — mark verified
                    payout_account.stripe_account_status = "verified"
                    payout_account.is_verified = True
                    payout_account.verified_at = datetime.now(timezone.utc)
                    payout_account.updated_at = datetime.now(timezone.utc)
                    logger.info(
                        f"[Stripe Connect /webhook] account {account_id} marked VERIFIED"
                    )
            elif payout_account:
                # Partial update — keep row in sync but do not mark verified
                payout_account.stripe_account_status = (
                    "active" if account.charges_enabled else "pending"
                )
                payout_account.updated_at = datetime.now(timezone.utc)
                if payout_account.is_verified and not account.details_submitted:
                    payout_account.is_verified = False
                    payout_account.verified_at = None
                    logger.warning(
                        f"[Stripe Connect /webhook] account {account_id} — "
                        f"details_submitted=False, rolling back is_verified"
                    )
            else:
                # Incomplete webhook for an account with no DB row — nothing to do yet
                logger.info(
                    f"[Stripe Connect /webhook] account {account_id} not yet complete "
                    f"and no DB row — skipping"
                )

            if payout_account:
                try:
                    db.commit()
                    logger.info(
                        f"[Stripe Connect /webhook] DB committed for account {account_id}"
                    )
                except Exception as db_err:
                    db.rollback()
                    logger.error(
                        f"[Stripe Connect /webhook] DB commit failed for {account_id}: "
                        f"{db_err}\n{traceback.format_exc()}"
                    )

        return {"status": "success", "event": event.type}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[Stripe Connect /webhook] unexpected error: {e}\n{traceback.format_exc()}"
        )
        return {"status": "error_acknowledged", "message": str(e)}


@router.post("/test/fix-restricted-account/{account_id}")
async def fix_restricted_test_account(
    account_id: str,
    db: Session = Depends(get_db),
):
    """TEST MODE ONLY: Force a restricted test account to active status."""
    if not stripe.api_key or not stripe.api_key.startswith("sk_test_"):
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available in test mode",
        )

    logger.info(f"[Stripe Connect /test/fix-restricted-account] account_id={account_id}")

    try:
        try:
            stripe.Account.modify(
                account_id,
                business_type="individual",
                individual={
                    "first_name": "Test", "last_name": "User",
                    "email": "test@example.com", "phone": "+15005550000",
                    "address": {
                        "line1": "123 Test St", "city": "San Francisco",
                        "state": "CA", "postal_code": "94103", "country": "US",
                    },
                    "dob": {"day": 1, "month": 1, "year": 1990},
                    "ssn_last_4": "0000",
                },
                tos_acceptance={
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "ip": "127.0.0.1",
                },
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                settings={"payouts": {"schedule": {"interval": "manual"}}},
                api_key=os.getenv("STRIPE_SECRET_KEY"),
            )
            logger.info(
                f"[Stripe Connect /test/fix-restricted-account] Stripe account modified: {account_id}"
            )
        except stripe.error.StripeError as e:
            logger.warning(
                f"[Stripe Connect /test/fix-restricted-account] Stripe modify failed "
                f"(still updating DB): {e}"
            )

        payout_account = db.query(PayoutAccount).filter(
            PayoutAccount.stripe_account_id == account_id
        ).first()

        if not payout_account:
            raise HTTPException(
                status_code=404,
                detail=f"No payout account found for Stripe ID: {account_id}",
            )

        payout_account.stripe_account_status = "active"
        payout_account.is_verified = True
        payout_account.verified_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            f"[Stripe Connect /test/fix-restricted-account] DB set to active for {account_id}"
        )

        return {
            "status": "success",
            "message": "Account force-verified for testing",
            "account_id": account_id,
            "note": "Test mode bypass — not for production",
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(
            f"[Stripe Connect /test/fix-restricted-account] error: "
            f"{e}\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Failed to fix restricted account: {e}")
