# load the signup page

# import the fastAPI library into
import os

from fastapi import APIRouter, Depends, Form, HTTPException

from datetime import datetime, timedelta, timezone

# import the function to hash the passwords
from passlib.context import CryptContext

# import the session
from sqlalchemy.orm import Session

# import the function for rendering the HTML sites
# import the database files (PostgreSQL/Neon)
from database.pg_connections import get_db

# import the user models for PostgreSQL
from database.pg_models import User, Referral, PendingSignup

# import the email function
from fastapi import BackgroundTasks
from emailing.email_service import email_service
from api.services.notification_service import NotificationService
from database.pg_models import NotificationType

import random, string
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["signup"])

# call the function for hashing the user's password
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# for the project's static folder done with react.js
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))  # get the absolute path of the file
BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(CURRENT_DIR))
)  # get the current directory of the file
OUT_DIR = os.path.join(BASE_DIR, "web")  # get the absolute path of the out folder

VERIFICATION_CODE_TTL_MINUTES = 15
MAX_VERIFY_ATTEMPTS = 5  # per pending signup, before it must be re-requested

def generate_referral_code(length=8):
    chars = string.ascii_uppercase + string.digits  # Allowed symbols
    return ''.join(random.choice(chars) for _ in range(length))

def generate_verification_code() -> str:
    return ''.join(random.choice(string.digits) for _ in range(6))


@router.post("/signup")
def signup(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    company_name: str = Form(None),
    referrer_code: str = Form(None),
    db: Session = Depends(get_db),
):
    """
    Stage 1 of signup: validate the form, email a verification code via
    Resend, and hold everything in `pending_signups` — no User row exists
    yet. This is the gate against fake/typo'd signup emails: a submission
    is never "successful" (a real account created) until that code comes
    back through POST /signup/verify. Resubmitting the same email before
    verifying just refreshes the code (e.g. "didn't receive it, try again"),
    rather than erroring.
    """
    logger.info(f"[SIGNUP] Received signup request for email: {email}, name: {name}, company: {company_name}")

    # Check if email exists as a REAL account already
    logger.info(f"[SIGNUP] Checking if email {email} already exists...")
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        logger.warning(f"[SIGNUP] Email {email} already exists, user_id: {existing_user.id}")
        raise HTTPException(status_code=400, detail="User already exists")

    # Validate passwords match
    logger.info(f"[SIGNUP] Validating password match...")
    if password != confirm_password:
        logger.warning("[SIGNUP] Passwords do not match")
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # Referral code, if any, is only *validated* (not consumed) at this
    # stage — an unrecognized code degrades to "sign up without a referrer"
    # here too, same as before, so a stale link never blocks registration.
    referrer_code_clean = None
    if referrer_code and referrer_code.strip():
        search_code = referrer_code.upper().strip()
        referrer = db.query(User).filter(User.referral_code == search_code).first()
        if referrer:
            referrer_code_clean = search_code
            logger.info(f"[SIGNUP] Referral code '{search_code}' validated for referrer ID {referrer.id}")
        else:
            logger.warning(f"[SIGNUP] Unrecognized referral code '{search_code}' — continuing without a referrer")

    code = generate_verification_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)

    pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
    if pending:
        logger.info(f"[SIGNUP] Refreshing existing pending signup for {email}")
        pending.name = name
        pending.password_hash = pwd_context.hash(password)
        pending.confirm_password_hash = pwd_context.hash(confirm_password)
        pending.company_name = company_name if company_name else None
        pending.referrer_code = referrer_code_clean
        pending.verification_code = code
        pending.code_expires_at = expires_at
        pending.attempts = 0
    else:
        pending = PendingSignup(
            email=email,
            name=name,
            password_hash=pwd_context.hash(password),
            confirm_password_hash=pwd_context.hash(confirm_password),
            company_name=company_name if company_name else None,
            referrer_code=referrer_code_clean,
            verification_code=code,
            code_expires_at=expires_at,
        )
        db.add(pending)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[SIGNUP] Failed to save pending signup for {email}: {e}")
        raise HTTPException(status_code=500, detail="Could not start signup — please try again")

    logger.info(f"[SIGNUP] Sending verification code to {email}")
    background_tasks.add_task(
        email_service.send_verification_code,
        email,
        name,
        code,
    )

    return {
        "message": "Verification code sent",
        "email": email,
        "requires_verification": True,
    }


@router.post("/signup/verify")
def verify_signup(
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Stage 2 of signup: the actual account only gets created here, once the
    submitted code matches what was emailed for this address (see /signup
    above). This is everything the old single-step /signup handler used to
    do directly.
    """
    logger.info(f"[SIGNUP VERIFY] Verifying {email}")

    pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
    if not pending:
        raise HTTPException(status_code=400, detail="No pending signup found for this email — please sign up again")

    if datetime.now(timezone.utc) > pending.code_expires_at.replace(tzinfo=timezone.utc):
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=400, detail="Verification code expired — please sign up again")

    if pending.attempts >= MAX_VERIFY_ATTEMPTS:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=400, detail="Too many incorrect attempts — please sign up again")

    if code.strip() != pending.verification_code:
        pending.attempts = (pending.attempts or 0) + 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect verification code")

    # Code is correct — re-check the email hasn't been registered in the
    # meantime (e.g. two tabs, a retried request) before creating the account.
    existing_user = db.query(User).filter(User.email == pending.email).first()
    if existing_user:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=400, detail="User already exists")

    referrer = None
    if pending.referrer_code:
        referrer = db.query(User).filter(User.referral_code == pending.referrer_code).first()

    # ── Waitlist referral continuity ─────────────────────────────────────────
    # The waitlist and main-app share the same database. If this email existed
    # on the waitlist, carry over their referral_code (so existing referral links
    # keep working) and their referral_count (so earned rewards are preserved).
    waitlist_refcode: str | None = None
    waitlist_refcount: int = 0
    try:
        from sqlalchemy import text as _text
        wl = db.execute(
            _text("SELECT referral_code, referral_count FROM waitlist WHERE LOWER(email) = LOWER(:email) LIMIT 1"),
            {"email": pending.email}
        ).fetchone()
        if wl:
            waitlist_refcode = wl[0]
            waitlist_refcount = int(wl[1] or 0)
            logger.info(f"[SIGNUP VERIFY] Waitlist record found for {pending.email}: code={waitlist_refcode}, count={waitlist_refcount}")
    except Exception as wl_err:
        logger.warning(f"[SIGNUP VERIFY] Could not read waitlist table: {wl_err}")
        db.rollback()  # Reset the failed transaction so subsequent queries work

    if waitlist_refcode and not db.query(User).filter(User.referral_code == waitlist_refcode).first():
        user_refcode = waitlist_refcode
    else:
        user_refcode = generate_referral_code()
        while db.query(User).filter(User.referral_code == user_refcode).first():
            user_refcode = generate_referral_code()

    logger.info(f"[SIGNUP VERIFY] Creating User object in database for {pending.email}...")
    new_user = User(
        name=pending.name,
        email=pending.email,
        password=pending.password_hash,
        confirm_password=pending.confirm_password_hash,
        referral_code=user_refcode,
        referrer_code=referrer.referral_code if referrer else None,
        company_name=pending.company_name,
        referral_count=waitlist_refcount,
    )

    from subscriptions.beta_service import BetaService
    BetaService.initialize_grace_period(new_user, db)

    db.add(new_user)
    db.flush()
    logger.info(f"[SIGNUP VERIFY] User object added, new user ID: {new_user.id}")

    if referrer:
        logger.info(f"[SIGNUP VERIFY] Processing referral for referrer ID: {referrer.id}")
        referrer.referral_count = (referrer.referral_count or 0) + 1

        new_user.total_chops = (new_user.total_chops or 0) + 50
        referrer.total_chops = (referrer.total_chops or 0) + 50
        referrer.referral_chops = (referrer.referral_chops or 0) + 50

        referral = Referral(
            referrer_id=referrer.id,
            referred_user_id=new_user.id,
            chops_awarded=50,
            created_at=datetime.now(timezone.utc)
        )
        db.add(referral)

        NotificationService.create_notification(
            db=db,
            user_id=new_user.id,
            type=NotificationType.REFERRAL_REGISTERED.value,
            title="Welcome Bonus!",
            message="You received 50 chops for joining via a referral link.",
            link="/dashboard/earnings"
        )
        NotificationService.create_notification(
            db=db,
            user_id=referrer.id,
            type=NotificationType.REFERRAL_REGISTERED.value,
            title="New Referral! +50 Chops",
            message=f"{new_user.name} signed up using your referral link. You earned 50 chops!",
            link="/dashboard/referrals"
        )

    db.delete(pending)

    try:
        db.commit()
        db.refresh(new_user)
        logger.info(f"[SIGNUP VERIFY] User {new_user.email} created successfully (ID: {new_user.id})")
    except Exception as e:
        db.rollback()
        logger.error(f"[SIGNUP VERIFY] Database commit failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error occurred during signup: {str(e)}")

    logger.info(f"[SIGNUP VERIFY] Scheduling welcome email to: {new_user.email}")
    background_tasks.add_task(
        email_service.send_welcome_email,
        new_user.email,
        new_user.name
    )

    return {
        "message": "User created successfully",
        "user_id": new_user.id,
        "referral_code": new_user.referral_code,
        "referral_applied": referrer is not None
    }
