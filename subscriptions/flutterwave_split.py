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

# Meta key carrying the TRUE subscription price (before Flutterwave's fee/
# VAT was added on top for the customer to pay — see get_flutterwave_processing_fee
# and checkoutForm.tsx). Verification reads this back rather than trusting
# the raw Flutterwave-verified charge amount, which is inflated by the fee;
# Subscriptions.amount (and everything derived from it — commission_service's
# split math) must reflect the real 300 NGN plan price, not the ~306 NGN the
# customer's card/account was actually debited for.
BASE_AMOUNT_META_KEY = "lavoo_base_amount"


# Nigerian VAT on Flutterwave's processing fee (FIRS-mandated, applied by
# Flutterwave to its own fee, not to the transaction amount). Confirmed
# directly, 2026-09-03, two independent ways: (1) a live /transactions/fee
# response only ever returns "fee" (6.12 for a 306 NGN charge) — no VAT
# field exists in that response at all, despite this function's old
# docstring claiming it already covered "fee + VAT". (2) a real settled
# transaction's receipt showed "Transaction fee: NGN 6.12" AND a separate
# "VAT: NGN 0.46" — and 6.12 * 0.075 = 0.459 ≈ 0.46, to the kobo. Omitting
# this was the exact gap behind the very first report of this feature ("in
# the completed 300 naira transaction, the fee and vat is about 6.45" —
# 6 (fee on 300) + 0.45 (VAT) = 6.45, not just the bare fee).
FLUTTERWAVE_FEE_VAT_RATE = Decimal("0.075")


def get_flutterwave_processing_fee(
    amount: Decimal, currency: str, payment_type: str = "card"
) -> Optional[Decimal]:
    """Ask Flutterwave's own /transactions/fee endpoint what it would deduct
    in processing fee + VAT for a charge of `amount`, so the checkout amount
    can be grossed up (charge = amount + fee) — the customer absorbs
    Flutterwave's cut instead of it being carved out of the subscription
    price, which is what the 40/60 referrer/Lavoo split is supposed to apply
    to in full. Returns None on any failure; every caller must fall back to
    charging the bare `amount` unmodified rather than blocking checkout —
    an un-grossed-up charge is a smaller inconvenience (Lavoo/referrer
    absorb the fee, same as before this feature) than a broken payment flow.

    The returned value is fee + VAT (see FLUTTERWAVE_FEE_VAT_RATE above) —
    the API's own "fee" field is VAT-exclusive, which previously left every
    gross-up short by exactly the VAT portion.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        return None
    try:
        response = requests.get(
            f"{FLUTTERWAVE_BASE_URL}/transactions/fee",
            params={"amount": str(amount), "currency": currency, "payment_type": payment_type},
            headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning("[FLW fee] non-200 status=%s body=%s", response.status_code, response.text[:300])
            return None
        data = response.json()
        if data.get("status") != "success":
            return None
        fee = data.get("data", {}).get("fee")
        if fee is None:
            return None
        fee = Decimal(str(fee))
        vat = (fee * FLUTTERWAVE_FEE_VAT_RATE).quantize(Decimal("0.01"))
        return fee + vat
    except requests.RequestException as e:
        logger.error("[FLW fee] request failed: %s", e)
        return None


def get_flutterwave_fx_rate(source_currency: str, destination_currency: str) -> Optional[Decimal]:
    """Live spot rate for converting source_currency -> destination_currency,
    via Flutterwave's own /v3/transfers/rates endpoint — used to convert a
    commission earned in one currency into what's actually sent when the
    referrer's only payout method is in a different currency (e.g. a
    Nigerian referrer's Flutterwave/NGN payout for a USD/GBP Stripe
    commission — there is structurally no way to "split" a Stripe charge
    into a Flutterwave account, so this converts and sends a plain transfer
    instead — see subscriptions/commission_service.py::_attempt_immediate_payout).

    Same-currency callers should skip this entirely (rate is trivially 1)
    rather than call it.

    Verified directly against the live endpoint: the `rate` field is
    amount-independent (querying with amount=1 vs amount=100000 for the
    same currency pair returns the same rate) — so a small nominal `amount`
    is always used here regardless of the real amount being converted.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        return None
    try:
        response = requests.get(
            f"{FLUTTERWAVE_BASE_URL}/transfers/rates",
            params={"amount": 1, "destination_currency": destination_currency, "source_currency": source_currency},
            headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning("[FLW fx rate] non-200 status=%s body=%s", response.status_code, response.text[:300])
            return None
        data = response.json()
        if data.get("status") != "success":
            return None
        rate = data.get("data", {}).get("rate")
        if rate is None:
            return None
        return Decimal(str(rate))
    except requests.RequestException as e:
        logger.error("[FLW fx rate] request failed: %s", e)
        return None


def find_existing_flutterwave_subaccount(account_number: str, bank_code: str) -> Optional[str]:
    """Search Flutterwave's own subaccount list for one already registered
    against this exact bank account, and return its subaccount_id. Used as
    the recovery path when create_flutterwave_subaccount hits "already
    exists" — confirmed directly (2026-09-02): a real subaccount existed on
    Flutterwave for a referrer's bank details that our own PayoutAccount row
    had no record of, so no split was ever possible for them despite a
    genuinely working subaccount sitting right there. Paginated defensively
    (capped at 20 pages / ~2000 subaccounts) — small in practice today, but
    a hard cap keeps this from looping forever if that ever changes.
    """
    if not FLUTTERWAVE_SECRET_KEY:
        return None
    try:
        for page in range(1, 21):
            resp = requests.get(
                f"{FLUTTERWAVE_BASE_URL}/subaccounts",
                params={"page": page},
                headers={"Authorization": f"Bearer {FLUTTERWAVE_SECRET_KEY}"},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != "success":
                return None
            for row in data.get("data", []) or []:
                if str(row.get("account_number")) == str(account_number) and str(row.get("account_bank")) == str(bank_code):
                    return row.get("subaccount_id")
            total_pages = (data.get("meta", {}) or {}).get("page_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
        return None
    except requests.RequestException as e:
        logger.error("[FLW subaccount] lookup failed: %s", e)
        return None


def create_flutterwave_subaccount(
    account_number: str,
    bank_code: str,
    business_name: str,
    business_email: str,
    main_account_charge_percentage: float,
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

    main_account_charge_percentage sets the subaccount's own STORED default
    split (its `split_value`), as a decimal fraction — same meaning and
    scale as build_split_config's main_account_charge_percentage (Lavoo's
    cut). This used to be hardcoded to 0.40 regardless of the referrer's
    actual rate. The per-transaction override (build_split_config, sent on
    every real charge) settles correctly — confirmed directly, 2026-09-03,
    against a real transaction's actual settled amounts (subaccount
    received ~40% of the settled total, matching the referrer's Commission
    ledger entry to the naira). But Flutterwave's own dashboard summarises
    a subaccount using its STORED default, not the last transaction's
    override — so with the old hardcoded 0.40 that summary permanently
    read "Your share: 40%, Subaccount's share: 60%" even though every real
    charge was correctly splitting 60/40, which is exactly the "why is my
    share still 40%" confusion reported directly. Passing the real
    computed value here keeps that dashboard summary honest, and removes
    the one path (a charge that somehow reaches Flutterwave with no
    per-transaction override at all) where the stored default is what
    actually gets used to move money.
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
                # Stored default split on the subaccount itself — see this
                # function's docstring for why this must already be
                # correct, not just a same-value safety net.
                "split_value": float(main_account_charge_percentage),
            },
            timeout=20,
        )
        logger.info(
            "[FLW subaccount] create response status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        if resp.status_code not in (200, 201):
            # Flutterwave rejects creating a second subaccount for a bank
            # account that already has one ("A subaccount with the account
            # number and bank already exists") — hit directly, for a
            # referrer whose subaccount was created in an earlier session
            # but whose id was never saved to our own PayoutAccount row
            # (e.g. before flutterwave_subaccount_id existed as a column,
            # or a save that silently failed). Recover the existing
            # subaccount's id instead of giving up — the row it belongs to
            # is real and already correctly split-configured on
            # Flutterwave's side, our database was just the thing out of
            # sync with it.
            existing_id = find_existing_flutterwave_subaccount(account_number, bank_code)
            if existing_id:
                logger.info(
                    "[FLW subaccount] recovered existing subaccount instead of creating a duplicate | "
                    "account=%s bank=%s subaccount_id=%s",
                    account_number, bank_code, existing_id,
                )
                return {"subaccount_id": existing_id}
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
    Flutterwave subaccount, return {"subaccount_id",
    "main_account_charge_percentage"} to attach to that user's charge
    (initial checkout or off-session renewal). Returns None when there's no
    referral, the referrer hasn't set up Flutterwave payout details, or
    subaccount creation previously failed — in every such case the caller
    charges 100% to Lavoo, same as before this module existed.

    main_account_charge_percentage is deliberately named for exactly what
    Flutterwave's API does with it, not what the referrer earns. Two
    separate bugs lived here, fixed at two different times — both
    confirmed directly against real behavior, not just documentation:

    1. DIRECTION: Flutterwave's own `transaction_charge` (with
       transaction_charge_type: "percentage") is the cut the MAIN account
       keeps; the subaccount automatically gets whatever remains, not the
       number you pass. A referrer earning 40% (COMMISSION_RATE_STANDARD)
       means Lavoo's main account keeps the other 60% — so this must be
       1 - the referrer's rate, not the referrer's rate itself. Confirmed
       against Flutterwave's dashboard: passing the referrer's 40% produced
       "Your share: 40%, Subaccount's share: 60%" — inverted.

    2. SCALE: the value must be a decimal FRACTION (0.6 for 60%), not a
       0-100 number. Flutterwave's docs state it plainly: "To collect a 9%
       commission, transaction_charge_type will be 'percentage' and
       transaction_charge will be 0.09." Sending 60 for "60%" was read as a
       6000% charge — confirmed directly at checkout: "The total subaccount
       transaction charge cannot be greater than the amount to be charged."
    """
    from database.pg_models import PayoutAccount, Referral
    from subscriptions.commission_service import CommissionService

    logger.info(f"[FLW split] checking split config | referred_user={referred_user_id}")

    referral = db.query(Referral).filter(Referral.referred_user_id == referred_user_id).first()
    if not referral:
        logger.info(f"[FLW split] no referral found | referred_user={referred_user_id} — no split")
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

    # Computed up front (not just after the self-heal block below) so a
    # freshly self-healed subaccount is created with the correct stored
    # default split too — see create_flutterwave_subaccount's docstring for
    # why that stored default matters even though a correct per-transaction
    # override is also sent on every charge.
    rate = CommissionService._get_rate_for_referrer(referral.referrer_id, db)
    main_account_charge_fraction = float(Decimal("1") - rate)

    if not account:
        # Self-heal: a verified Flutterwave bank account with no subaccount
        # yet is a real, observed case — id 1 (the very first referrer this
        # feature was built for) saved bank details before this module
        # existed and never got backfilled, so its split silently fell back
        # to "no subaccount" forever, with no automatic way to notice or
        # recover. Reported directly: "the settlement from the nigerian
        # transaction we did didn't arrive" — this is why. Rather than a
        # one-off backfill script (which only fixes accounts that exist
        # today, not a future transient subaccount-creation failure), retry
        # creation right here, every time it's needed, for any verified
        # Flutterwave bank account still missing one.
        candidate = (
            db.query(PayoutAccount)
            .filter(
                PayoutAccount.user_id == referral.referrer_id,
                PayoutAccount.payment_method == "flutterwave",
                PayoutAccount.bank_name.isnot(None),
                PayoutAccount.account_number.isnot(None),
                PayoutAccount.bank_code.isnot(None),
            )
            .order_by(PayoutAccount.updated_at.desc().nullslast(), PayoutAccount.created_at.desc())
            .first()
        )
        if candidate:
            from database.pg_models import User
            referrer_user = db.query(User).filter(User.id == referral.referrer_id).first()
            subaccount = create_flutterwave_subaccount(
                account_number=candidate.account_number,
                bank_code=candidate.bank_code,
                business_name=candidate.account_name or (referrer_user.name if referrer_user else "Lavoo Referral Partner"),
                business_email=referrer_user.email if referrer_user else "",
                main_account_charge_percentage=main_account_charge_fraction,
            )
            if subaccount and subaccount.get("subaccount_id"):
                candidate.flutterwave_subaccount_id = subaccount["subaccount_id"]
                candidate.subaccount_status = "active"
                db.flush()
                account = candidate
                logger.info(
                    f"[FLW split] Self-healed missing subaccount for referrer {referral.referrer_id}: "
                    f"created {subaccount['subaccount_id']}"
                )
            else:
                logger.warning(f"[FLW split] Self-heal subaccount creation failed for referrer {referral.referrer_id}")

    if not account:
        logger.info(f"[FLW split] no usable subaccount | referrer={referral.referrer_id} — no split, falls back to pending commission")
        return None

    logger.info(
        f"[FLW split] split config ready | referrer={referral.referrer_id} "
        f"subaccount={account.flutterwave_subaccount_id} referrer_rate={float(rate) * 100}% "
        f"main_account_keeps={main_account_charge_fraction * 100}% (sent to Flutterwave as {main_account_charge_fraction})"
    )
    return {
        "subaccount_id": account.flutterwave_subaccount_id,
        # A DECIMAL FRACTION (0.6 for 60%), not a 0-100 number — Flutterwave's
        # own docs are explicit: "To collect a 9% commission,
        # transaction_charge_type will be 'percentage' and transaction_charge
        # will be 0.09." Sending 60 (meant as "60%") was read literally as a
        # 6000% charge, which is why checkout showed "the total subaccount
        # transaction charge cannot be greater than the amount to be
        # charged" — the *0-100 scaling itself* was the bug, not the 40/60
        # split direction fixed earlier.
        "main_account_charge_percentage": main_account_charge_fraction,
    }


def build_split_config(subaccount_id: str, main_account_charge_percentage: float) -> dict:
    """Flutterwave v3 charge-payload fragment for a percentage split,
    shared by the tokenized renewal charge (server-side) and the split-info
    endpoint the frontend reads before building its checkout config.

    main_account_charge_percentage must already be Lavoo's cut, as a
    decimal fraction (0.6, not 60) — see get_split_config_for_referred_user's
    docstring for both of those. This function does not transform it at
    all; it only shapes it into the field name Flutterwave's API expects
    (`transaction_charge`, which — despite the name — is what the MAIN
    account keeps, not what the subaccount receives)."""
    return {
        "id": subaccount_id,
        "transaction_charge_type": "percentage",
        "transaction_charge": main_account_charge_percentage,
    }
