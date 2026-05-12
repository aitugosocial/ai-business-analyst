import os
import secrets
import uuid
from email_validator import validate_email, EmailNotValidError
import logging
from datetime import datetime, timedelta, timezone

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie, Header, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic_settings import BaseSettings
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database.pg_connections import get_db
from typing import Optional
from database.pg_models import ShowUser, User, AuthResponse, FailedLoginAttempt, SecurityEvent, IPBlacklist
from api.utils.sub_utils import sync_user_subscription

bearer_scheme = HTTPBearer()
router = APIRouter(prefix="", tags=["authenticate"])

def log_debug(message):
    logger.debug(message)

"""Generating and storing the secret key"""

class Settings(BaseSettings):
    secret_key: str = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.secret_key:
            # Check if we have JWT_SECRET again (in case BaseSettings didn't pick it up)
            self.secret_key = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY")

            if not self.secret_key:
                print("⚠️  Neither SECRET_KEY nor JWT_SECRET found in environment. Generating a new one and saving it to .env...")
                self.secret_key = secrets.token_hex(32)
                self._save_to_env("SECRET_KEY", self.secret_key)

    def _save_to_env(self, key, value):
        try:
            env_path = os.path.join(os.getcwd(), ".env")
            # Create .env if it doesn't exist
            if not os.path.exists(env_path):
                with open(env_path, "w") as f:
                    f.write(f"{key}={value}\n")
            else:
                # Check if key exists
                with open(env_path, "r") as f:
                    lines = f.readlines()

                key_exists = False
                with open(env_path, "w") as f:
                    for line in lines:
                        if line.startswith(f"{key}="):
                            f.write(f"{key}={value}\n")
                            key_exists = True
                        else:
                            f.write(line)

                    if not key_exists:
                        # Append if not found
                        if lines and not lines[-1].endswith('\n'):
                             f.write('\n')
                        f.write(f"{key}={value}\n")

            print(f"✅ Saved new {key} to {env_path}")
            # Also update current process env
            os.environ[key] = value
        except Exception as e:
            print(f"❌ Failed to save {key} to .env: {e}")

settings = Settings()
SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"

# Extended token expiration times
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days (was 30 minutes)
REFRESH_TOKEN_EXPIRE_DAYS = 30  # 30 days (was 7 days)

# Environment detection
IS_PRODUCTION = os.getenv("ENVIRONMENT", "development") == "production"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def get_current_user(authorization: Optional[str] = Header(None), access_token_cookie: Optional[str] = Cookie(None), db: Session = Depends(get_db)):
    """
    Get current user from either Authorization header or cookie.
    Priority: Authorization header > Cookie
    """
    token = None

    # First, try to get token from Authorization header
    if authorization:
        try:
            scheme, token = authorization.split()
            if scheme.lower() != 'bearer':
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication scheme",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # If no Authorization header, try cookie
    elif access_token_cookie:
        token = access_token_cookie

    # If no token found anywhere
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate token
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")

        if email is None:
            raise credentials_exception

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as e:
        log_debug(f"DEBUG AUTH: (get_current_user) JWT decode error: {str(e)}")
        raise credentials_exception

    # Get user from database
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        log_debug(f"DEBUG AUTH: (get_current_user) User not found: {email}")
        raise credentials_exception

    # Sync Subscription Status (Strictly based on FIRST transaction)
    try:
        # from api.utils.sub_utils import sync_user_subscription
        user = sync_user_subscription(db, user)
    except Exception as e:
        logger.error(f"Error checking subscription status: {e}")
        db.rollback()

    return user

@router.get("/me", response_model=AuthResponse)
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
    access_token_cookie: Optional[str] = Cookie(None)
):
    """
    Get current user details.
    Syncs subscription status lazily without blocking.
    """
    # Sync subscription status (non-blocking lazy load)
    sync_user_subscription(db, current_user)

    # Auto-generate referral code if missing (Self-healing)
    if not current_user.referral_code:
        try:
            # Simple referral code generation if not imported
            import random
            import string
            chars = string.ascii_uppercase + string.digits
            code = ''.join(random.choice(chars) for _ in range(8))
            current_user.referral_code = code
            db.commit()
            logger.info(f"Auto-generated referral code {code} for user {current_user.id}")
        except Exception as e:
            logger.error(f"Failed to auto-generate referral code: {e}")
            db.rollback()

    # Helper to get user role
    role = "user"
    if current_user.is_admin:
        role = "admin"

    # Get token from header or cookie for return
    token = authorization.split()[1] if authorization else access_token_cookie

    # Return matched fields
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "subscription_status": current_user.subscription_status,
        "subscription_plan": current_user.subscription_plan,
        "role": role,
        "referral_code": current_user.referral_code,
        "department": current_user.department,
        "location": current_user.location,
        "bio": current_user.bio,
        "two_factor_enabled": current_user.two_factor_enabled,
        "email_notifications": current_user.email_notifications,
        "created_at": current_user.created_at,
        "access_token": token or "",
        "token_type": "bearer",
        "is_beta_user": current_user.is_beta_user,
        "subscription_expires_at": current_user.subscription_expires_at,
        "stripe_customer_id": current_user.stripe_customer_id,
        "stripe_payment_method_id": current_user.stripe_payment_method_id,
        "card_last4": current_user.card_last4,
        "card_brand": current_user.card_brand,
        "card_exp_month": current_user.card_exp_month,
        "card_exp_year": current_user.card_exp_year
    }


class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    bio: Optional[str] = None
    two_factor_enabled: Optional[bool] = None
    email_notifications: Optional[bool] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.patch("/me", response_model=AuthResponse)
def update_profile(
    update_data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update user profile.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = current_user
    role = "admin" if user.is_admin else "user"

    email_changed = False
    if update_data.email and update_data.email != user.email:
        existing_user = db.query(User).filter(
            User.email == update_data.email,
            User.id != user.id,
        ).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Email is already in use by another account")
        user.email = update_data.email
        email_changed = True

    if update_data.name:
        user.name = update_data.name
    if update_data.department:
        user.department = update_data.department
    if update_data.location:
        user.location = update_data.location
    if update_data.bio:
        user.bio = update_data.bio
    if update_data.two_factor_enabled is not None:
        user.two_factor_enabled = update_data.two_factor_enabled
    if update_data.email_notifications is not None:
        user.email_notifications = update_data.email_notifications

    db.commit()
    db.refresh(user)

    # JWT sub is the user's email. Changing the email invalidates the existing
    # token immediately, so issue a fresh one with the new address as sub.
    if email_changed:
        new_token = create_access_token(
            {"sub": user.email},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
    else:
        new_token = ""

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": role,
        "subscription_status": user.subscription_status,
        "subscription_plan": user.subscription_plan,
        "referral_code": user.referral_code,
        "department": user.department,
        "location": user.location,
        "bio": user.bio,
        "two_factor_enabled": user.two_factor_enabled,
        "email_notifications": user.email_notifications,
        "created_at": user.created_at,
        "access_token": new_token,
        "token_type": "bearer",
        "is_beta_user": user.is_beta_user,
        "subscription_expires_at": user.subscription_expires_at,
        "stripe_customer_id": user.stripe_customer_id,
        "stripe_payment_method_id": user.stripe_payment_method_id,
        "card_last4": user.card_last4,
        "card_brand": user.card_brand,
        "card_exp_month": user.card_exp_month,
        "card_exp_year": user.card_exp_year
    }


@router.post("/change-password")
def change_password(
    password_data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = current_user

    if not pwd_context.verify(password_data.current_password, user.password):
        raise HTTPException(status_code=400, detail="Incorrect current password")

    user.password = pwd_context.hash(password_data.new_password)
    db.commit()

    return {"message": "Password updated successfully"}


def get_admin_user(
    authorization: Optional[str] = Header(None),
    access_token_cookie: Optional[str] = Cookie(None),
    db: Session = Depends(get_db)
):
    """
    Get current user and ensure they have admin privileges.
    Supports both Authorization header and access_token cookie.
    """
    user = get_current_user(authorization, access_token_cookie, db)

    if not user.is_admin:
        log_debug(f"DEBUG AUTH: User {user.email} is NOT an admin")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this resource.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    log_debug(f"DEBUG AUTH: Successfully authenticated admin: {user.email}")
    return user


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)





@router.post("/login", response_model=AuthResponse)
def login(request: ShowUser, response: Response, fastapi_request: Request, db: Session = Depends(get_db)):
    """User login endpoint - returns JWT access token"""
    user = db.query(User).filter(User.email == request.email).first()

    # Check if the email is registered
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )

    # Get client IP address
    client_ip = fastapi_request.headers.get("x-forwarded-for", fastapi_request.client.host if fastapi_request.client else "unknown").split(",")[0].strip()

    # Check if IP is blacklisted BEFORE any authentication
    blacklist_check = db.query(IPBlacklist).filter(
        IPBlacklist.ip_address == client_ip,
        IPBlacklist.is_active == True
    ).first()

    if blacklist_check:
        log_debug(f"Blocked login attempt from blacklisted IP: {client_ip}")
        raise HTTPException(
            status_code=403,
            detail="IP address blacklisted. Access denied."
        )

    # Verify password policy: Check password BEFORE is_active
    if not pwd_context.verify(request.password, user.password):
        # Record failed login attempt - CRITICAL for security tracking
        # This section records the failure in two places:
        # 1. failed_login_attempts table (for tracking patterns)
        # 2. security_events table (triggers auto-blocking after 3 attempts)

        client_ip = fastapi_request.headers.get("x-forwarded-for", fastapi_request.client.host if fastapi_request.client else "unknown").split(",")[0].strip()
        user_agent = fastapi_request.headers.get("user-agent", "unknown")

        # Convert integer user_id to UUID format for database compatibility
        user_uuid = uuid.UUID(int=user.id) if isinstance(user.id, int) else user.id

        # Record in failed_login_attempts and create security event
        try:
            failed_attempt = FailedLoginAttempt(
                email=request.email,
                ip_address=client_ip,
                user_agent=user_agent
            )
            db.add(failed_attempt)

            event = SecurityEvent(
                type="failed_login",
                severity="medium",
                description=f"Failed login attempt for {user.email}",
                ip_address=client_ip,
                user_id=user_uuid,
                status="logged"
            )
            db.add(event)
            db.commit()  # Commit both records together
            logger.info(f"Recorded failed login for {user.email} from IP {client_ip}")
        except Exception as e:
            logger.error(f"CRITICAL: Failed to record security diagnostics: {e}")
            db.rollback()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been deactivated. Please contact support with lavoo@gmail.com to verify your account."
        )

    # Update Last Active - this also auto-reactivates inactive users
    # (inactive = no login for 30 days, but is_active is still True)
    # When they login again, they become active automatically
    user.updated_at = datetime.now(timezone.utc)
    user.last_login = datetime.now(timezone.utc)

    # Note: Subscription status sync moved to dashboard load (lazy loading)
    # to prevent blocking login endpoint

    db.commit()

    role = "admin" if user.is_admin else "user"

    # Generate access token with extended expiration
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email, "role": role, "id": user.id},
        expires_delta=access_token_expires
    )

    refresh_token = create_refresh_token({"sub": user.email, "role": role, "id": user.id})

    # Set cookies with extended max_age
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=False,  # Allow frontend to read for API headers (e.g., Stripe History)
        secure=IS_PRODUCTION,  # True in production, False in development
        samesite="None" if IS_PRODUCTION else "lax",  # "None" for production (Stripe), "lax" for development
        max_age=60 * 60 * 24 * 7  # 7 days to match token expiration
    )

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="None" if IS_PRODUCTION else "lax",
        max_age=60 * 60 * 24 * 30  # 30 days
    )

    logger.info(f"User logged in: {user.email}, role: {role}")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": role,
        "referral_code": user.referral_code,
        "subscription_status": user.subscription_status,
        "subscription_plan": user.subscription_plan,
        "is_beta_user": user.is_beta_user,
        "subscription_expires_at": user.subscription_expires_at,
        "stripe_customer_id": user.stripe_customer_id,
        "stripe_payment_method_id": user.stripe_payment_method_id,
        "card_last4": user.card_last4,
        "card_brand": user.card_brand,
        "card_exp_month": user.card_exp_month,
        "card_exp_year": user.card_exp_year
    }


@router.post("/refresh", response_model=AuthResponse)
def refresh_token_endpoint(refresh_token: str = Cookie(None), response: Response = None, db: Session = Depends(get_db)):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token provided")

    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        user_id = payload.get("id")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token has expired")
    except JWTError as e:
        logger.warning(f"Refresh token decode error: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Issue a new access token
    access_token = create_access_token(
        {"sub": email, "role": role, "id": user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    # Also issue a new refresh token
    new_refresh_token = create_refresh_token({"sub": email, "role": role, "id": user.id})

    # Update cookies if response is provided
    if response:
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite="None" if IS_PRODUCTION else "lax",
            max_age=60 * 60 * 24 * 7
        )
        response.set_cookie(
            key="refresh_token",
            value=new_refresh_token,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite="None" if IS_PRODUCTION else "lax",
            max_age=60 * 60 * 24 * 30
        )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": role
    }


@router.post("/token")
def login_for_swagger(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
):
    """
    OAuth2 compatible token login for Swagger UI.
    Use email as username in the form.
    """
    user = db.query(User).filter(User.email == form_data.username).first()

    if not user:
        logger.warning(f"Login attempt for unknown user: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not pwd_context.verify(form_data.password, user.password):
        logger.warning(f"Password verification failed for: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = "admin" if user.is_admin else "user"

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email, "role": role, "id": user.id},
        expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer", "role": role}
