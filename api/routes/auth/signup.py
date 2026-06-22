# load the signup page

# import the fastAPI library into
import os

from fastapi import APIRouter, Depends, Form, HTTPException

from datetime import datetime, timezone

# import the function to hash the passwords
from passlib.context import CryptContext

# import the session
from sqlalchemy.orm import Session

# import the function for rendering the HTML sites
# import the database files (PostgreSQL/Neon)
from database.pg_connections import get_db

# import the user models for PostgreSQL
from database.pg_models import User, Referral

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

def generate_referral_code(length=8):
    chars = string.ascii_uppercase + string.digits  # Allowed symbols
    return ''.join(random.choice(chars) for _ in range(length))

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
    Create a new user account with optional referral support.
    """
    logger.info(f"[SIGNUP] Received signup request for email: {email}, name: {name}, company: {company_name}")
    
    # Check if email exists
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

    # Validate and fetch referrer if code provided
    referrer = None
    if referrer_code and referrer_code.strip():
        search_code = referrer_code.upper().strip()
        logger.info(f"[SIGNUP] Processing referral code: {search_code}")
        referrer = db.query(User).filter(
            User.referral_code == search_code
        ).first()

        if not referrer:
            logger.warning(f"[SIGNUP] Invalid referral code: {search_code}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid referral code. No matching user found."
            )

        logger.info(f"[SIGNUP] Referral code '{search_code}' validated for referrer ID {referrer.id}")

    # ── Waitlist referral continuity ─────────────────────────────────────────
    # The waitlist and main-app share the same database.  If this email existed
    # on the waitlist, carry over their referral_code (so existing referral links
    # keep working) and their referral_count (so earned rewards are preserved).
    waitlist_refcode: str | None = None
    waitlist_refcount: int = 0
    try:
        from sqlalchemy import text as _text
        wl = db.execute(
            _text("SELECT referral_code, referral_count FROM waitlist WHERE LOWER(email) = LOWER(:email) LIMIT 1"),
            {"email": email}
        ).fetchone()
        if wl:
            waitlist_refcode = wl[0]
            waitlist_refcount = int(wl[1] or 0)
            logger.info(f"[SIGNUP] Waitlist record found for {email}: code={waitlist_refcode}, count={waitlist_refcount}")
    except Exception as wl_err:
        logger.warning(f"[SIGNUP] Could not read waitlist table: {wl_err}")

    # Use waitlist referral code if available and not already taken in users table
    if waitlist_refcode and not db.query(User).filter(User.referral_code == waitlist_refcode).first():
        user_refcode = waitlist_refcode
        logger.info(f"[SIGNUP] Using waitlist referral code: {user_refcode}")
    else:
        # Generate unique referral code for new user
        logger.info(f"[SIGNUP] Generating referral code...")
        user_refcode = generate_referral_code()
        while db.query(User).filter(User.referral_code == user_refcode).first():
            user_refcode = generate_referral_code()

    logger.info(f"[SIGNUP] Created user referral code: {user_refcode}")

    # Create user
    logger.info(f"[SIGNUP] Hashing password and creating user object...")
    hashed = pwd_context.hash(password)
    passcode = pwd_context.hash(confirm_password)
    
    logger.info(f"[SIGNUP] Creating User object in database...")
    new_user = User(
        name=name,
        email=email,
        password=hashed,
        confirm_password=passcode,
        referral_code=user_refcode,
        referrer_code=referrer.referral_code if referrer else None,
        company_name=company_name if company_name else None,
        referral_count=waitlist_refcount,  # carry over waitlist referral history
    )

    logger.info(f"[SIGNUP] Calling BetaService.initialize_grace_period...")
    from subscriptions.beta_service import BetaService
    BetaService.initialize_grace_period(new_user, db)

    logger.info(f"[SIGNUP] Adding user to database session...")
    db.add(new_user)
    db.flush()
    logger.info(f"[SIGNUP] User object added, new user ID: {new_user.id}")

    # Process referral rewards
    if referrer:
        logger.info(f"[SIGNUP] Processing referral for referrer ID: {referrer.id}")
        referrer.referral_count = (referrer.referral_count or 0) + 1

        # Award 10 chops to the referred user for signing up via referral link
        new_user.total_chops = (new_user.total_chops or 0) + 10
        logger.info(f"[SIGNUP] Awarded 10 chops to referred user {new_user.email}")

        # Award 10 chops to the referrer for a successful signup using their code
        referrer.total_chops = (referrer.total_chops or 0) + 10
        referrer.referral_chops = (referrer.referral_chops or 0) + 10
        logger.info(f"[SIGNUP] Awarded 10 chops to referrer {referrer.email}")

        # Create referral record (chops_awarded tracks referrer's reward, set on subscription)
        referral = Referral(
            referrer_id=referrer.id,
            referred_user_id=new_user.id,
            chops_awarded=10,
            created_at=datetime.now(timezone.utc)
        )
        db.add(referral)

        # Notify new user of their welcome chops
        NotificationService.create_notification(
            db=db,
            user_id=new_user.id,
            type=NotificationType.REFERRAL_REGISTERED.value,
            title="Welcome Bonus!",
            message="You received 10 chops for joining via a referral link.",
            link="/dashboard/earnings"
        )

        # Notify referrer of the new signup + their chops reward
        NotificationService.create_notification(
            db=db,
            user_id=referrer.id,
            type=NotificationType.REFERRAL_REGISTERED.value,
            title="New Referral! +10 Chops",
            message=f"{new_user.name} signed up using your referral link. You earned 10 chops!",
            link="/dashboard/referrals"
        )

    try:
        logger.info(f"[SIGNUP] Committing to database...")
        db.commit()
        db.refresh(new_user)
        logger.info(f"[SIGNUP] User {new_user.email} created successfully (ID: {new_user.id})")
    except Exception as e:
        db.rollback()
        logger.error(f"[SIGNUP] Database commit failed during signup: {e}")
        logger.error(f"[SIGNUP] Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"[SIGNUP] Full traceback: {traceback.format_exc()}")
        # Send the actual error string to frontend to diagnose the production issue
        raise HTTPException(status_code=500, detail=f"Database error occurred during signup: {str(e)}")

    # Send welcome email
    logger.info(f"[SIGNUP] Scheduling welcome email to: {new_user.email}")
    background_tasks.add_task(
        email_service.send_welcome_email,
        new_user.email,
        new_user.name
    )

    logger.info(f"[SIGNUP] Completed successfully for email: {email}")
    return {
        "message": "User created successfully",
        "user_id": new_user.id,
        "referral_code": new_user.referral_code,
        "referral_applied": referrer is not None
    }
