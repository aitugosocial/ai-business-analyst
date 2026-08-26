"""
Flutterwave Split Payment support.

Before this module, a referrer's builder bonus was calculated (see
commission_service.py) but always sat as a `Commission` row with
status='pending' until the referrer clicked "Request Payout" and an admin
(or the auto-approve cron) processed a separate bank transfer later —
Lavoo collected 100% of every charge first, then moved a share out
afterward. This module lets Flutterwave itself split the CHARGE, so the
referrer's percentage lands in their own account (via a Flutterwave
Subaccount) at the moment of payment, with no separate transfer step.

A referrer only gets a subaccount once they save verified Flutterwave bank
details (see subscriptions/commissions.py::setup_payout_account). Until
then, get_split_config_for_referred_user returns None and the caller falls
back to charging 100% to Lavoo — the existing pending-Commission /
manual-payout path is exactly that fallback, not a separate system.
"""
import logging
import os
from decimal import Decimal
from typing import Optional

import requests
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

FLUTTERWAVE_SECRET_KEY = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY")
FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"

# Meta key echoed back by Flutterwave on transaction verification (data.meta)
# when the initial checkout config included it — see checkoutForm.tsx. This
# is how verify_flutterwave_payment knows, deterministically, whether the
# ACTUAL charge that already happened included a split, rather than
# re-querying "does a subaccount exist right now" — that second approach has
# a real race: a referrer could add their subaccount in the gap between the
# frontend building the checkout config and the backend verifying payment,
# which would make a charge that was NEVER split look auto-settled.
SPLIT_META_KEY = "lavoo_split_subaccount_id"


def create_flutterwave_subaccount(
    account_number: str,
    bank_code: str,
    business_name: str,
    business_email: str,
) -> Optional[dict]:
    """
    Register a Flutterwave Subaccount for a payout account. Returns the
    created subaccount dict (contains 'subaccount_id') on success, None on
    any failure — callers must treat a failure as "no subaccount yet", not
    raise, since this runs inline with a user saving their bank details and
    a transient Flutterwave API issue should not block that save. The most
    common real failure is a bad/unsupported bank_code, which the existing
    verify_bank_account endpoint (used by the frontend's bank-account form)
    should already have caught before this ever runs.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        logger.error("[FLW subaccount] secret key not configured")
        return None
    if not account_number or not bank_code:
        logger.warning("[FLW subaccount] missing account_number/bank_code — skipping")
        return None
    try:
        resp = requests.post(
            f"{FLUTTERWAVE_BASE_URL}/subaccounts",
            headers={
                "Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "account_bank": bank_code,
                "account_number": account_number,
                "business_name": business_name or "Lavoo Referral Partner",
                "business_email": business_email,
                "business_contact": business_name or "Lavoo Referral Partner",
                "business_contact_mobile": "N/A",
                "business_mobile": "N/A",
                "country": "NG",
                "split_type": "percentage",
                # Fallback split value on the subaccount itself. Always
                # overridden per-transaction (build_split_config below) with
                # the actual referrer/partner rate from commission_service —
                # this only applies if a charge is ever sent without an
                # explicit transaction_charge, which should not happen.
                "split_value": float(Decimal("0.40")),
            },
            timeout=20,
        )
        logger.info(
            "[FLW subaccount] create response status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        if resp.status_code not in (200, 201):
            return None
        data = resp.json()
        if data.get("status") != "success":
            return None
        return data.get("data")
    except requests.RequestException as e:
        logger.error("[FLW subaccount] create failed: %s", e)
        return None


def get_split_config_for_referred_user(referred_user_id: int, db: Session) -> Optional[dict]:
    """
    If `referred_user_id` was referred by someone with a verified, active
    Flutterwave subaccount, return {"subaccount_id", "split_percentage"} to
    attach to that user's charge (initial checkout or off-session renewal).
    Returns None when there's no referral, the referrer hasn't set up
    Flutterwave payout details, or subaccount creation previously failed —
    in every such case the caller charges 100% to Lavoo, same as before this
    module existed.
    """
    from database.pg_models import PayoutAccount, Referral
    from subscriptions.commission_service import CommissionService

    referral = db.query(Referral).filter(Referral.referred_user_id == referred_user_id).first()
    if not referral:
        return None

    account = (
        db.query(PayoutAccount)
        .filter(
            PayoutAccount.user_id == referral.referrer_id,
            PayoutAccount.payment_method == "flutterwave",
            PayoutAccount.flutterwave_subaccount_id.isnot(None),
            PayoutAccount.subaccount_status == "active",
        )
        # A referrer can have more than one saved bank account (see
        # commissions.py::setup_payout_account) — prefer whichever one was
        # most recently touched rather than an arbitrary row.
        .order_by(PayoutAccount.updated_at.desc().nullslast(), PayoutAccount.created_at.desc())
        .first()
    )
    if not account:
        return None

    rate = CommissionService._get_rate_for_referrer(referral.referrer_id, db)
    return {
        "subaccount_id": account.flutterwave_subaccount_id,
        "split_percentage": float(rate * 100),
    }


def build_split_config(subaccount_id: str, split_percentage: float) -> dict:
    """Flutterwave v3 charge-payload fragment for a percentage split,
    shared by the tokenized renewal charge (server-side) and the split-info
    endpoint the frontend reads before building its checkout config."""
    return {
        "id": subaccount_id,
        "transaction_charge_type": "percentage",
        "transaction_charge": split_percentage,
    }
