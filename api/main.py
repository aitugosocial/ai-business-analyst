# Install uvloop for better async performance (must be before any asyncio usage)
try:
    import uvloop
    uvloop.install()
except (ImportError, RuntimeError):
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "uvloop not available; using default asyncio event loop"
    )

# Standard library imports
import asyncio
import logging
import os
from datetime import datetime

# Third-party imports
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

# Local application imports
from api.cache import init_cache, close_cache
from api.routes import dependencies, notifications
from api.routes.admin import admin, security, firewall_scanner, revenue, users, dashboard, settings
from api.routes.auth import login, signup, forgot_password
from api.routes.auth.login import get_current_user
from api.routes.decision_engine import analyzer as business_analyzer
from api.routes.support import customer_service, reviews
from api.routes.signals import signals
from api.routes.user import stats as user_stats, alerts, insights, referrals, earnings, settings as user_settings, missions as user_missions
from api.security.firewall import FirewallMiddleware, initialize_default_firewall_rules, firewall_manager
from api.security.vulnerability_scanner import vulnerability_scanner
from config.logging import get_logger, setup_logging
from database.pg_connections import get_db_info, init_db, get_db, SessionLocal
from database.pg_models import User, CreateOrderRequest, CaptureRequest
from emailing import email_service
from subscriptions import paypal, flutterwave, stripe, commissions, stripe_connect
from subscriptions.beta_service import BetaService

# Load environment variables from .env file (must be done early)
try:
    from dotenv import load_dotenv

    # Get project root directory (one level up from api/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Try .env.local first (local development), fallback to .env
    env_local = os.path.join(project_root, '.env.local')
    env_file = os.path.join(project_root, '.env')

    if os.path.exists(env_local):
        load_dotenv(env_local)
        print("✅ Environment variables loaded from .env.local file")
    elif os.path.exists(env_file):
        load_dotenv(env_file)
        print("✅ Environment variables loaded from .env file")
    else:
        load_dotenv()  # Try default locations
        print("✅ Environment variables loaded from default location")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment")
except Exception as e:
    print(f"⚠️  Error loading environment variables: {e}")

# Initialize logging system
setup_logging(level=logging.INFO if os.getenv("DEBUG") != "true" else logging.DEBUG)
logger = get_logger(__name__)

# import the router page
# from api.routes import ai_db as ai  # PostgreSQL-based AI routes - DEPRECATED (uses deleted analyst_db)
from api.routes import dependencies
from api.routes.auth import login, signup, forgot_password, google_oauth
from api.routes.decision_engine import analyzer as business_analyzer
from api.routes.user import stats as user_stats, alerts, insights, referrals, earnings, settings as user_settings, missions as user_missions, profile as user_profile
from api.routes.support import customer_service, reviews
from api.routes.admin import admin, security, firewall_scanner, revenue, users, dashboard, settings, permissions, content as admin_content

# Payment routes
from subscriptions import paypal, flutterwave, stripe, commissions, stripe_connect

# Email service
from emailing import email_service


logger.info("✓ Using Neon PostgreSQL database")


app = FastAPI(debug=os.getenv("DEBUG", "false").lower() == "true")

# Password context for admin creation
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


# DEBUG: Global Request Logger to confirm traffic
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    HTTP middleware that logs every incoming request path/method and the
    corresponding response status code. Useful for debugging traffic and
    verifying that the app is receiving requests in cloud environments.
    """
    logger.info(f"INCOMING REQUEST: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"RESPONSE STATUS: {request.method} {request.url.path} -> {response.status_code}")
    return response


_origins_base = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
    "http://localhost:8080",
    # Production frontend domains — always allowed regardless of ALLOWED_ORIGINS env var
    "https://lavoo.io",
    "https://www.lavoo.io",
    "https://control.lavooai.com/",
]
# Allow additional origins from environment (comma-separated list)
_extra_origins = os.getenv("ALLOWED_ORIGINS", "")
if _extra_origins:
    _origins_base.extend([o.strip() for o in _extra_origins.split(",") if o.strip()])

origins = list(dict.fromkeys(_origins_base))  # deduplicate while preserving order


# GZip compression — reduces JSON payload size by ~70% for typical API responses
app.add_middleware(GZipMiddleware, minimum_size=500)

# Initialize and Register Firewall Middleware
app.add_middleware(FirewallMiddleware)

# Enable CORS for (React form requests)
# CORSMiddleware MUST be added after FirewallMiddleware to be the outermost layer
# (FastAPI processes middlewares in reverse order of addition)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)


# Health check endpoint for monitoring (Railway, Render, DigitalOcean, etc.)
@app.api_route("/health", methods=["GET", "HEAD"])
@app.api_route("/api/health", methods=["GET", "HEAD"]) # Alias for consistency
async def health_check():
    """
    Liveness / readiness probe for Railway, Docker HEALTHCHECK, load balancers, etc.

    Returns a small JSON payload containing database type and a "healthy" status.
    Actually executes `SELECT 1` against the database rather than trusting local
    pool metadata, so `database.connected` reflects real connectivity — a stale
    or dropped connection is detected here instead of silently reporting healthy.
    Because this is defined before the heavy startup work, once the startup
    coroutine finishes this endpoint becomes reachable.
    """
    db_connected = False
    db_error = None
    db_type = None
    try:
        db_type = get_db_info().get("type")
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_connected = True
        finally:
            db.close()
    except Exception as e:
        db_error = str(e)

    return {
        "status": "healthy" if db_connected else "unhealthy",
        "timestamp": datetime.now().isoformat(),
        "database": {"type": db_type, "connected": db_connected, **({"error": db_error} if db_error else {})},
        "version": "1.0.0",
    }


@app.get("/api/beta-status")
async def get_beta_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Return the current beta / subscription status for the logged-in user,
    augmented with card info when appropriate. Used by the frontend to decide
    which UI elements (paywall, grace period banners, etc.) to show.
    """
    try:
        status = BetaService.get_user_status(current_user)

        if status.get("show_card_info") and current_user.card_last4:
            status["card_info"] = {
                "last4": current_user.card_last4, "brand": current_user.card_brand,
                "exp_month": current_user.card_exp_month, "exp_year": current_user.card_exp_year
            }

        status["is_beta_mode"] = BetaService.is_beta_mode()
        status["is_in_grace_period"] = BetaService.is_in_grace_period(current_user)
        return status
    except Exception as e:
        logger.error(f"Error in /api/beta-status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

async def run_scheduled_scans():
    """
    Infinite background loop (started via create_task at startup) that sleeps
    15 minutes then runs a full vulnerability/firewall scan using the
    vulnerability_scanner service. Failures are logged but do not crash the loop.
    """
    while True:
        try:
            # Wait 15 minutes between scans
            await asyncio.sleep(15 * 60)
            logger.info("Starting scheduled vulnerability scan...")
            db = SessionLocal()
            try:
                await vulnerability_scanner.run_full_scan(db)
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Scheduled scan failed: {e}")
            await asyncio.sleep(60) # Wait a bit before retrying if it fails


# try creating an admin user if not exists
def create_admin_user(db: Session):
    """
    Idempotent helper that ensures a default administrative user exists.

    Credentials come from environment variables (admin_email, admin_password,
    admin_name) with safe development defaults. The function is called during
    startup; the passed Session is committed and should be closed by the caller.
    """
    admin_email = os.getenv("admin_email", "admin@gmail.com")
    admin_password = os.getenv("admin_password", "admin123")
    password = pwd_context.hash(admin_password)

    try:
        existing_admin = db.query(User).filter(User.email == admin_email).first()
        if existing_admin:
            logger.info("✓ Admin user already exists")
            return

        new_admin = User(
            name=os.getenv("admin_name", "Admin"),
            email=admin_email,
            password=password,
            confirm_password=password,
            is_admin=True
        )
        db.add(new_admin)
        db.commit()
        logger.info(f"✓ Admin user created: {admin_email}")
    except Exception as e:
        logger.error(f"❌ Failed to create admin user: {e}")


# =============================================================================
# SUBSCRIPTION RENEWAL HELPERS
#
# Design rules enforced here:
#   1. Per user, only the MOST RECENT expired subscription is processed.
#      Older expired rows (already superseded) are just marked expired.
#   2. The payment method used for THAT subscription is tried FIRST.
#      The other method is the fallback ONLY if the primary fails.
#   3. Bank transfer subscriptions: cannot auto-renew (no stored token).
#      User is notified and subscription expires.
#   4. A user is NEVER charged by both methods for the same renewal cycle.
# =============================================================================

def _attempt_flutterwave_renewal(user_id: int, plan: str, amount: float, currency: str) -> bool:
    """
    Attempt an off-session renewal charge via Flutterwave using a previously
    stored card token. Returns True only on a confirmed successful charge.
    Creates a fresh Subscriptions row on success and updates the user's status.
    """
    try:
        import requests as _req
        import secrets
        from database.pg_connections import SessionLocal
        from database.pg_models import User, Subscriptions
        from decimal import Decimal

        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not getattr(user, "flutterwave_card_token", None):
                logger.info("[flw-renewal] no token for user %s — skipped", user_id)
                return False

            secret_key = os.getenv("NEXT_PUBLIC_FLUTTERWAVE_SECRET_KEY", "")
            if not secret_key:
                logger.error("[flw-renewal] FLUTTERWAVE_SECRET_KEY not set")
                return False

            tx_ref = f"LAVOO-RENEW-{plan.upper()}-{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(3).upper()}"
            parts = (user.name or "Customer Name").split()
            payload = {
                "token": user.flutterwave_card_token,
                "currency": currency,
                "country": "NG",
                "amount": amount,
                "email": user.email,
                "first_name": parts[0],
                "last_name": parts[-1] if len(parts) > 1 else ".",
                "tx_ref": tx_ref,
                "narration": f"Lavoo {plan.title()} Subscription Renewal",
            }
            headers = {
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
            }
            resp = _req.post(
                "https://api.flutterwave.com/v3/charges?type=token",
                json=payload, headers=headers, timeout=30
            )
            logger.info("[flw-renewal] charge response status=%s body=%s",
                        resp.status_code, resp.text[:300])

            if resp.status_code == 200:
                data = resp.json()
                charge = data.get("data", {})
                if data.get("status") == "success" and charge.get("status") in ("successful", "completed"):
                    start = datetime.now(timezone.utc)
                    end = start + timedelta(days=PLAN_DURATION_MAP.get(plan, 30))
                    new_sub = Subscriptions(
                        user_id=user_id,
                        tx_ref=tx_ref,
                        transaction_id=str(charge.get("id", "")),
                        amount=Decimal(str(amount)),
                        payment_provider="Flutterwave",
                        currency=currency,
                        subscription_plan=plan,
                        status="successful",
                        subscription_status="active",
                        start_date=start,
                        end_date=end,
                    )
                    db.add(new_sub)
                    user.subscription_status = "active"
                    user.subscription_plan = plan
                    db.commit()
                    logger.info("[flw-renewal] ✅ renewed user %s plan=%s tx=%s", user_id, plan, tx_ref)
                    return True
                logger.warning("[flw-renewal] charge not successful: %s", data.get("message"))
    except Exception as exc:
        logger.error("[flw-renewal] error for user %s: %s", user_id, exc, exc_info=True)
    return False


# Plan duration map reused by both renewal functions
PLAN_DURATION_MAP = {"monthly": 30, "quarterly": 90, "yearly": 365}


def _attempt_stripe_renewal(user_id: int, plan_type: str) -> bool:
    """
    Attempt an off-session Stripe Subscription.create using the customer's
    saved payment method. On success a local Subscriptions record is also
    written via the shared _create_active_subscription_record helper.
    """
    try:
        import stripe as _stripe
        import os
        from database.pg_connections import SessionLocal
        from database.pg_models import User

        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not user.stripe_customer_id or not user.stripe_payment_method_id:
                return False

            from subscriptions.stripe import get_stripe_price_id, get_amount_from_stripe_price, _create_active_subscription_record
            price_id = get_stripe_price_id(plan_type, "GBP") or get_stripe_price_id(plan_type, "USD")
            if not price_id:
                logger.warning("[renewal] no price_id for plan=%s user=%s", plan_type, user_id)
                return False

            try:
                _stripe.PaymentMethod.attach(user.stripe_payment_method_id, customer=user.stripe_customer_id)
            except Exception:
                pass  # already attached

            sub = _stripe.Subscription.create(
                customer=user.stripe_customer_id,
                items=[{"price": price_id}],
                default_payment_method=user.stripe_payment_method_id,
                expand=["latest_invoice.payment_intent"],
                metadata={"user_id": str(user_id), "plan_type": plan_type, "source": "auto_renewal"},
            )
            if sub.get("status") in ("active", "trialing"):
                amount = get_amount_from_stripe_price(price_id)
                _create_active_subscription_record(db, user, sub, plan_type, amount, tx_ref_prefix="RENEW")
                logger.info("[renewal] ✅ renewed user %s plan=%s", user_id, plan_type)
                return True
            logger.warning("[renewal] Stripe status=%s for user %s", sub.get("status"), user_id)
    except Exception as exc:
        logger.error("[renewal] failed for user %s: %s", user_id, exc, exc_info=True)
    return False


async def run_subscription_expiry_job():
    """
    Background job — runs once at startup then every 24 h.

    For each expired Stripe subscription that has a saved payment method,
    attempts an automatic renewal off-session.  If renewal succeeds the sub
    stays active.  If renewal fails (or no Stripe card), the sub is expired
    and the user's status is set to Free.

    Covers users who haven't logged in — complementing the lazy per-request
    sync in sync_user_subscription().

    Per-user deduplication: only the MOST RECENT expired sub per user drives
    the renewal decision.  Older superseded rows are silently marked expired.
    """
    # Wait before the first run so we don't block the startup event handler
    await asyncio.sleep(60)

    while True:
        try:
            from database.pg_connections import SessionLocal
            from database.pg_models import User, Subscriptions
            from datetime import timezone as _tz
            from api.services.notification_service import NotificationService
            db = SessionLocal()
            try:
                now = datetime.now(_tz.utc)
                active_statuses = ('active', 'completed', 'paid', 'successful', 'succeeded')
                all_expired = db.query(Subscriptions).filter(
                    Subscriptions.end_date < now,
                    Subscriptions.status.in_(active_statuses),
                ).order_by(Subscriptions.user_id, Subscriptions.end_date.desc()).all()

                # Deduplicate: keep only the most-recent expired sub per user.
                # Older rows for the same user are already superseded — just mark
                # them expired without attempting another charge.
                latest_per_user: dict[int, Subscriptions] = {}
                older_subs: list[Subscriptions] = []
                for sub in all_expired:
                    if sub.user_id not in latest_per_user:
                        latest_per_user[sub.user_id] = sub
                    else:
                        older_subs.append(sub)

                # Silently expire older superseded rows
                for sub in older_subs:
                    sub.status = 'expired'
                    sub.subscription_status = 'Free'

                renewed = 0
                expired_count = 0
                affected_users: set[int] = set()

                for user_id, sub in latest_per_user.items():
                    # Yield to the event loop between users to prevent blocking health checks
                    await asyncio.sleep(0.1)

                    user = db.query(User).filter(User.id == user_id).first()
                    plan = sub.subscription_plan or "monthly"
                    provider = (sub.payment_provider or "").lower()
                    amount = float(sub.amount or 0)
                    currency = sub.currency or "NGN"

                    has_stripe = bool(user and user.stripe_customer_id and user.stripe_payment_method_id)
                    has_flw_token = bool(user and getattr(user, "flutterwave_card_token", None))
                    is_bank_transfer = (provider == "flutterwave" and not has_flw_token)

                    # ── Bank transfer: no token stored, cannot auto-renew ─────────────
                    if is_bank_transfer:
                        logger.info(
                            "[expiry-job] user %s paid via bank transfer — "
                            "no auto-renewal possible; notifying", user_id
                        )
                        if user:
                            NotificationService.create_notification(
                                db=db, user_id=user_id,
                                type="subscription_expired",
                                title="Your subscription has expired",
                                message=(
                                    "Your subscription period has ended. "
                                    "Please make a new payment to continue using Lavoo."
                                ),
                                link="/dashboard/upgrade",
                            )
                        sub.status = 'expired'
                        sub.subscription_status = 'Free'
                        affected_users.add(user_id)
                        expired_count += 1
                        continue

                    # ── Build ordered renewal strategy from the original provider ────
                    # Primary = the method the user originally paid with.
                    # Fallback = the other method, only if both tokens are on file.
                    # This guarantees ONE charge attempt per cycle.
                    if provider == "stripe":
                        strategy = (
                            [("stripe", {})] +
                            ([("flutterwave", {"amount": amount, "currency": currency})] if has_flw_token and amount > 0 else [])
                        )
                    else:  # flutterwave card
                        strategy = (
                            ([("flutterwave", {"amount": amount, "currency": currency})] if has_flw_token and amount > 0 else []) +
                            ([("stripe", {})] if has_stripe else [])
                        )

                    success = False
                    for method, kwargs in strategy:
                        if method == "stripe":
                            success = _attempt_stripe_renewal(user_id, plan)
                        elif method == "flutterwave":
                            success = _attempt_flutterwave_renewal(user_id, plan, kwargs["amount"], kwargs["currency"])
                        if success:
                            logger.info("[expiry-job] ✅ renewed user %s via %s plan=%s", user_id, method, plan)
                            renewed += 1
                            break
                        logger.warning("[expiry-job] %s renewal failed for user %s — trying fallback", method, user_id)

                    if not success:
                        sub.status = 'expired'
                        sub.subscription_status = 'Free'
                        affected_users.add(user_id)
                        expired_count += 1
                        if user:
                            NotificationService.create_notification(
                                db=db, user_id=user_id,
                                type="subscription_expired",
                                title="Subscription renewal failed",
                                message=(
                                    "We were unable to renew your subscription automatically. "
                                    "Please update your payment method to continue."
                                ),
                                link="/dashboard/upgrade",
                            )

                if affected_users:
                    db.query(User).filter(User.id.in_(affected_users)).update(
                        {"subscription_status": "Free", "subscription_plan": None},
                        synchronize_session=False,
                    )
                db.commit()
                logger.info(
                    "[subscription-expiry-job] processed=%d renewed=%d expired=%d "
                    "users_downgraded=%d older_cleaned=%d",
                    len(latest_per_user), renewed, expired_count,
                    len(affected_users), len(older_subs),
                )
            except Exception as exc:
                logger.error("[subscription-expiry-job] error: %s", exc, exc_info=True)
                db.rollback()
            finally:
                db.close()
        except Exception as outer:
            logger.error("[subscription-expiry-job] outer error: %s", outer)
        await asyncio.sleep(24 * 60 * 60)


async def run_new_alert_notifications_job():
    """
    Runs every 5 minutes. Finds alerts inserted by the crawler cron job
    since the last check and fans out in-app notifications to all active
    subscribers. The admin-endpoint path already fires fan-out immediately;
    this job covers the cron-job insertion path where no HTTP handler runs.
    """
    from datetime import timezone as _tz
    _last_checked = datetime.now(_tz.utc)  # start from now; don't re-notify old alerts
    while True:
        await asyncio.sleep(5 * 60)  # wait 5 minutes before first poll
        try:
            from database.pg_connections import SessionLocal
            from database.pg_models import Alert, User
            from api.services.notification_service import NotificationService

            now = datetime.now(_tz.utc)
            with SessionLocal() as db:
                new_alerts = db.query(Alert).filter(
                    Alert.is_active == True,
                    Alert.created_at >= _last_checked,
                    Alert.created_at < now,
                ).all()

                if new_alerts:
                    subscriber_ids = [
                        row[0]
                        for row in db.query(User.id).filter(
                            User.is_active == True,
                            User.subscription_status == "active",
                        ).all()
                    ]
                    for alert in new_alerts:
                        short = (alert.why_act_now[:97] + "…") if len(alert.why_act_now or "") > 100 else (alert.why_act_now or "")
                        for uid in subscriber_ids:
                            NotificationService.create_notification(
                                db=db,
                                user_id=uid,
                                type="new_alert",
                                title=f"🆕 {alert.title}",
                                message=short,
                                link="/dashboard/opportunity-alerts",
                            )
                    logger.info(
                        "[alert-notify-job] fanned out %d new alerts to %d subscribers",
                        len(new_alerts), len(subscriber_ids)
                    )
            _last_checked = now
        except Exception as exc:
            logger.error("[alert-notify-job] error: %s", exc, exc_info=True)


def run_heavy_schema_migrations():
    """
    Background task that performs all non-critical, potentially slow schema
    evolution, table creation, index creation, and data back-filling.

    Executed via asyncio.create_task() from startup_event *after* the fast-path
    init (init_db + admin + cache) has completed. This guarantees that uvicorn
    reaches "Application startup complete" and the /health endpoint responds
    quickly even when there are many ALTER/CREATE/UPDATE statements or a large
    security_setup.sql file.

    All statements are written to be idempotent (CREATE TABLE IF NOT EXISTS,
    ALTER ... ADD COLUMN IF NOT EXISTS, etc.) so it is safe to run while the
    rest of the application is already serving traffic.
    """
    logger.info("Starting background schema migrations & index builds...")

    # Ensure admin exists (moved here from startup_event to prevent blocking the event loop)
    admin_db = SessionLocal()
    try:
        create_admin_user(admin_db)
    except Exception as e:
        logger.error(f"Failed to create admin user in background: {e}")
    finally:
        admin_db.close()

    # --- Auto-migration for users/reviews/payouts/subscriptions columns (was first big block) ---
    db = SessionLocal()
    try:
        try:
            db.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS department VARCHAR(100),
                ADD COLUMN IF NOT EXISTS location VARCHAR(100) DEFAULT 'Nigeria',
                ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT 'IT Operations',
                ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS email_notifications BOOLEAN DEFAULT TRUE,
                ADD COLUMN IF NOT EXISTS company_name VARCHAR(255),
                ADD COLUMN IF NOT EXISTS industry VARCHAR(100),
                ADD COLUMN IF NOT EXISTS avatar_url TEXT;
            """))

            db.execute(text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_attended BOOLEAN DEFAULT FALSE"))

            db.execute(text("""
                ALTER TABLE payout_accounts
                DROP CONSTRAINT IF EXISTS payout_accounts_user_id_key;
            """))

            db.execute(text("""
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20);
            """))

            db.execute(text("""
                UPDATE subscriptions
                SET subscription_status = CASE
                    WHEN end_date < NOW() THEN 'expired'
                    WHEN status NOT IN ('completed', 'active', 'paid', 'successful') THEN 'Payment failed'
                    ELSE 'active'
                END
                WHERE subscription_status IS NULL;
            """))

            try:
                db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS recommended_tool_stacks JSON"))
            except Exception as e:
                logger.warning(f"Failed to add recommended_tool_stacks: {e}")

            try:
                db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS roadmap_task_summaries JSON"))
            except Exception as e:
                logger.warning(f"Failed to add roadmap_task_summaries: {e}")

            try:
                db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS recommendation_mode VARCHAR"))
                db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS single_tool_recommendation JSON"))
            except Exception as e:
                logger.warning(f"Failed to add recommendation mode columns: {e}")

            try:
                db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS flutterwave_card_token VARCHAR(500)"))
                db.commit()
            except Exception as e:
                logger.warning(f"Failed to add flutterwave_card_token: {e}")

            # Security table fixes
            try:
                db.execute(text("""
                    DO $$
                    BEGIN
                        IF EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='failed_login_attempts' AND column_name='attempt_time') THEN
                            ALTER TABLE failed_login_attempts RENAME COLUMN attempt_time TO created_at;
                        END IF;
                    END $$;
                """))
            except Exception as e:
                logger.warning(f"Failed to rename attempt_time: {e}")

            try:
                db.execute(text("ALTER TABLE firewall_rules ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
            except Exception as e:
                logger.warning(f"Failed to add is_active to firewall_rules: {e}")

            try:
                db.execute(text("""
                    ALTER TABLE system_settings
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
                """))
            except Exception as e:
                logger.warning(f"Failed to add columns to system_settings: {e}")

            try:
                db.execute(text("""
                    CREATE TABLE IF NOT EXISTS user_notifications (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        type VARCHAR(50) NOT NULL,
                        title VARCHAR(255) NOT NULL,
                        message TEXT NOT NULL,
                        link VARCHAR(255),
                        is_read BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_user_notifications_user_unread ON user_notifications(user_id, is_read);
                """))
            except Exception as e:
                logger.warning(f"Failed to create user_notifications table: {e}")

        except Exception as e:
            logger.warning(f"Batch migration warning: {e}")

        db.commit()
        logger.info("✓ User/reviews/subscription column migrations checked")
    except Exception as e:
        logger.warning(f"Migration block warning: {e}")
        db.rollback()
    finally:
        db.close()

    # --- Email column on ip_blacklist ---
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE ip_blacklist ADD COLUMN IF NOT EXISTS email VARCHAR(255);"))
        db.commit()
        logger.info("✓ Added email column to ip_blacklist table")
    except Exception as e:
        logger.warning(f"Email column migration: {e}")
        db.rollback()
    finally:
        db.close()

    # --- Execute security_setup.sql (views, functions, triggers, firewall init) ---
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_file = os.path.join(project_root, "database", "security_setup.sql")
        if os.path.exists(sql_file):
            with open(sql_file, "r") as f:
                sql_content = f.read()

            db = SessionLocal()
            try:
                try:
                    db.execute(text("DROP TABLE IF EXISTS security_metrics_summary CASCADE"))
                    db.commit()
                    logger.info("Dropped existing security_metrics_summary table if present")
                except Exception as drop_error:
                    logger.debug(f"No table to drop: {drop_error}")
                    db.rollback()

                db.execute(text(sql_content))
                db.commit()
                logger.info("✓ Security views and triggers initialized from security_setup.sql")

                try:
                    initialize_default_firewall_rules(db)
                    firewall_manager.load_rules(db)
                    logger.info("✓ Firewall rules initialized")
                except Exception as fw_error:
                    logger.warning(f"Firewall initialization: {fw_error}")

            except Exception as e:
                logger.error(f"Failed to execute security setup SQL: {e}")
                db.rollback()
            finally:
                db.close()
        else:
            logger.warning(f"security_setup.sql not found at {sql_file}")
    except Exception as e:
        logger.error(f"Error during security SQL initialization: {e}")

    # --- Community tables (large block of CREATEs) ---
    db = SessionLocal()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS community_channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                slug VARCHAR(100) UNIQUE NOT NULL,
                description TEXT,
                category VARCHAR(50) NOT NULL DEFAULT 'General',
                member_count INTEGER DEFAULT 0,
                post_count INTEGER DEFAULT 0,
                icon VARCHAR(10),
                is_public BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channel_members (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel_id INTEGER NOT NULL REFERENCES community_channels(id) ON DELETE CASCADE,
                is_moderator BOOLEAN DEFAULT FALSE,
                joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS community_discussions (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER NOT NULL REFERENCES community_channels(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL,
                content TEXT NOT NULL,
                tags JSONB,
                like_count INTEGER DEFAULT 0,
                reply_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                is_pinned BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS discussion_replies (
                id SERIAL PRIMARY KEY,
                discussion_id INTEGER NOT NULL REFERENCES community_discussions(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                like_count INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS discussion_likes (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                discussion_id INTEGER NOT NULL REFERENCES community_discussions(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, discussion_id)
            );
            CREATE TABLE IF NOT EXISTS community_events (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                event_type VARCHAR(50) NOT NULL DEFAULT 'Webinar',
                scheduled_at TIMESTAMP WITH TIME ZONE,
                duration_minutes INTEGER DEFAULT 60,
                max_attendees INTEGER,
                attendee_count INTEGER DEFAULT 0,
                host_name VARCHAR(100),
                meeting_link VARCHAR(500),
                is_published BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS event_registrations (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_id INTEGER NOT NULL REFERENCES community_events(id) ON DELETE CASCADE,
                registered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS community_activities (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action_type VARCHAR(50) NOT NULL,
                target_id INTEGER,
                target_type VARCHAR(50),
                target_name VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS saved_items (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item_id INTEGER NOT NULL,
                item_type VARCHAR(50) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, item_id, item_type)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id);
            CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members(channel_id);
            CREATE INDEX IF NOT EXISTS idx_discussions_channel ON community_discussions(channel_id);
            CREATE INDEX IF NOT EXISTS idx_discussions_user ON community_discussions(user_id);
            CREATE INDEX IF NOT EXISTS idx_community_activities_user ON community_activities(user_id);
            CREATE INDEX IF NOT EXISTS idx_saved_items_user ON saved_items(user_id);
        """))
        db.commit()
        logger.info("✓ Community tables created/verified (background)")
    except Exception as e:
        logger.warning(f"Community table migration: {e}")
        db.rollback()
    finally:
        db.close()

    # --- Marketplace tables ---
    db = SessionLocal()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS marketplace_tools (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                author VARCHAR(100) NOT NULL,
                description TEXT NOT NULL,
                full_description TEXT,
                category VARCHAR(100) NOT NULL DEFAULT 'AI Tools',
                price FLOAT DEFAULT 0.0,
                tags JSONB,
                features JSONB,
                icon_name VARCHAR(50) NOT NULL DEFAULT 'Cpu',
                color_theme VARCHAR(30) NOT NULL DEFAULT 'orange',
                sales_count INTEGER DEFAULT 0,
                rating FLOAT DEFAULT 0.0,
                review_count INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                purchase_url VARCHAR(500),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS marketplace_purchases (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tool_id INTEGER NOT NULL REFERENCES marketplace_tools(id) ON DELETE CASCADE,
                status VARCHAR(50) NOT NULL DEFAULT 'pending',
                amount_paid FLOAT DEFAULT 0.0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, tool_id)
            );
            CREATE TABLE IF NOT EXISTS marketplace_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL,
                description TEXT NOT NULL,
                budget VARCHAR(100),
                timeline VARCHAR(100),
                status VARCHAR(50) NOT NULL DEFAULT 'open',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """))
        db.commit()
        logger.info("✓ Marketplace tables created/verified (background)")
    except Exception as e:
        logger.warning(f"Marketplace table migration: {e}")
        db.rollback()
    finally:
        db.close()

    # --- Performance indexes (many CREATE INDEX IF NOT EXISTS) ---
    try:
        db2 = SessionLocal()
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_ba_user_id ON business_analyses(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_ba_created_at ON business_analyses(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sub_user_id ON subscriptions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_sub_status ON subscriptions(subscription_status)",
            "CREATE INDEX IF NOT EXISTS idx_sub_created_at ON subscriptions(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_is_active ON alerts(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_ua_user_id ON user_alerts(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_ua_alert_id ON user_alerts(alert_id)",
            "CREATE INDEX IF NOT EXISTS idx_ua_composite ON user_alerts(user_id, alert_id)",
            "CREATE INDEX IF NOT EXISTS idx_upa_user_id ON user_pinned_alerts(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_upa_alert_id ON user_pinned_alerts(alert_id)",
            "CREATE INDEX IF NOT EXISTS idx_upa_composite ON user_pinned_alerts(user_id, alert_id)",
            "CREATE INDEX IF NOT EXISTS idx_ref_referrer_id ON referrals(referrer_id)",
            "CREATE INDEX IF NOT EXISTS idx_ref_referred_id ON referrals(referred_user_id)",
            "CREATE INDEX IF NOT EXISTS idx_com_user_id ON commissions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_com_referred_id ON commissions(referred_user_id)",
            "CREATE INDEX IF NOT EXISTS idx_com_sub_id ON commissions(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_com_status ON commissions(status)",
            "CREATE INDEX IF NOT EXISTS idx_com_created_at ON commissions(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_payout_user_id ON payouts(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_payout_status ON payouts(status)",
            "CREATE INDEX IF NOT EXISTS idx_pa_user_id ON payout_accounts(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_un_user_id ON user_notifications(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_un_created_at ON user_notifications(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_insights_is_active ON insights(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_insights_created_at ON insights(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_ui_user_id ON user_insights(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_ui_insight_id ON user_insights(insight_id)",
            "CREATE INDEX IF NOT EXISTS idx_upi_user_id ON user_pinned_insights(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_reviews_user_id ON reviews(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_conv_review_id ON conversations(review_id)",
            "CREATE INDEX IF NOT EXISTS idx_conv_is_read ON conversations(is_read)",
            "CREATE INDEX IF NOT EXISTS idx_users_sub_status ON users(subscription_status)",
            "CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_cm_user_id ON channel_members(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_cm_channel_id ON channel_members(channel_id)",
            "CREATE INDEX IF NOT EXISTS idx_cd_channel_id ON community_discussions(channel_id)",
            "CREATE INDEX IF NOT EXISTS idx_cd_user_id ON community_discussions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_dr_discussion_id ON discussion_replies(discussion_id)",
            "CREATE INDEX IF NOT EXISTS idx_dl_user_id ON discussion_likes(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_mp_user_id ON marketplace_purchases(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_mp_tool_id ON marketplace_purchases(tool_id)",
            "CREATE INDEX IF NOT EXISTS idx_mp_status ON marketplace_purchases(status)",
            "CREATE INDEX IF NOT EXISTS idx_si_user_id ON saved_items(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_si_composite ON saved_items(user_id, item_type)",
            "CREATE INDEX IF NOT EXISTS idx_um_user_id ON user_missions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_cs_user_id ON commission_summaries(user_id)",
            "ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS chops_gifted INTEGER DEFAULT 0",
            "ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS post_type VARCHAR(50) DEFAULT 'discussion'",
            "ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS spice_count INTEGER DEFAULT 0",
            "ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS quoted_discussion_id INTEGER REFERENCES community_discussions(id) ON DELETE SET NULL",
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS show_mission_comments_in_community BOOLEAN DEFAULT FALSE",
        ]
        for stmt in index_statements:
            try:
                db2.execute(text(stmt))
            except Exception:
                pass
        db2.commit()
        logger.info(f"✓ Performance indexes verified ({len(index_statements)} statements) (background)")
    except Exception as idx_err:
        logger.warning(f"Index creation batch failed: {idx_err}")
    finally:
        try:
            db2.close()
        except Exception:
            pass

    logger.info("✓ Background heavy schema migrations completed.")


@app.on_event("startup")
async def startup_event():
    """
    FastAPI startup handler.

    Performs only the *minimum* work required before the application can serve
    traffic:

      1. Starts the three long-running background job tasks (scans, expiry, alerts).
      2. Calls init_db() (creates core tables via SQLAlchemy metadata).
      3. Ensures an admin user exists.
      4. Initializes the cache backend (our Redis-safe version).
      5. Fires the heavy schema-migration / index / security-setup work as a
         background task via create_task so that uvicorn can emit
         "Application startup complete" and the /health endpoint becomes
         reachable quickly.

    All expensive DDL (CREATE TABLE IF NOT EXISTS for community/marketplace,
    the large security_setup.sql execution, dozens of CREATE INDEX, the
    subscription_status backfill UPDATE, etc.) now live in
    run_heavy_schema_migrations() which runs after the startup coroutine has
    finished. This directly addresses the Railway symptom of the startup
    handler hanging after init_db().

    Because the migrations use idempotent "IF NOT EXISTS" statements they are
    safe to execute while the app is already accepting requests.
    """
    asyncio.create_task(run_scheduled_scans())
    asyncio.create_task(run_subscription_expiry_job())
    asyncio.create_task(run_new_alert_notifications_job())

    try:
        # --- MINIMAL WORK REQUIRED FOR "Application startup complete" ---
        init_db()
        db_info = get_db_info()
        logger.info(f"✓ Database initialized: {db_info['type']} at {db_info['host']}")

        # Ensure admin exists (moved to a background task so synchronous DB queries don't block the event loop)
        # It is now called at the beginning of run_heavy_schema_migrations()

        # Init cache wrapped in a strict timeout to prevent network black-hole hangs
        try:
            await asyncio.wait_for(init_cache(), timeout=4.0)
        except Exception as e:
            logger.error(f"⚠️ Cache init timed out or failed: {e}. Forcing in-memory fallback.")
            from fastapi_cache.backends.inmemory import InMemoryBackend
            from fastapi_cache import FastAPICache
            FastAPICache.init(InMemoryBackend(), prefix="aianalyst:")

        # Schema columns — run as background tasks so the startup handler
        # returns immediately and uvicorn starts accepting health probes.
        # All columns already exist on Railway (added directly); IF NOT EXISTS
        # makes every statement a sub-millisecond no-op on subsequent boots.
        def _run_critical_migrations():
            _sync_db = SessionLocal()
            try:
                _sync_db.execute(text("SET lock_timeout = '5s'"))
                _sync_db.execute(text("ALTER TABLE community_discussions ADD COLUMN IF NOT EXISTS post_type VARCHAR(50) DEFAULT 'discussion'"))
                _sync_db.execute(text("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS show_mission_comments_in_community BOOLEAN DEFAULT FALSE"))
                _sync_db.execute(text("ALTER TABLE user_mission_steps ADD COLUMN IF NOT EXISTS reflection TEXT"))
                _sync_db.execute(text("ALTER TABLE user_mission_steps ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE"))
                _sync_db.execute(text("UPDATE user_settings SET show_mission_comments_in_community = FALSE WHERE show_mission_comments_in_community IS NULL"))
                _sync_db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'normal_user'"))
                _sync_db.execute(text("ALTER TABLE ai_tools ADD COLUMN IF NOT EXISTS embedding TEXT"))
                _sync_db.commit()
                logger.info("✓ Schema columns ensured (background)")
            except Exception as _e:
                logger.warning(f"Schema column migration warning (non-fatal): {_e}")
                _sync_db.rollback()
            finally:
                _sync_db.close()

        # Both migration jobs run in thread-pool workers — startup returns
        # instantly and /health becomes reachable before either job finishes.
        asyncio.create_task(asyncio.to_thread(_run_critical_migrations))
        asyncio.create_task(asyncio.to_thread(run_heavy_schema_migrations))

        logger.info("✓ Startup complete — schema migrations running in background.")
        # Returning from here lets uvicorn log "Application startup complete"
        # and makes /health (and all routers) reachable.

    except Exception as e:
        logger.error(f"❌ Critical startup failure: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """
    FastAPI shutdown handler. Currently only responsible for closing the
    (optional) Redis connection used by the cache layer.
    """
    await close_cache()

# Include API routers (specific routes)
# app.include_router(ai.router, prefix="/api")  # DEPRECATED: ai_db uses deleted analyst_db module
app.include_router(signup.router, prefix="/api")  # For React frontend that uses /api/signup
app.include_router(google_oauth.router, prefix="/api")  # Google OAuth — /api/auth/google/login, /api/auth/google/callback
app.include_router(login.router, prefix="/api")  # For React frontend that uses /api/login
app.include_router(forgot_password.router, prefix="/api")  # Password reset endpoints
app.include_router(signup.router)  # Also register without prefix for /signup
app.include_router(login.router)  # Also register without prefix for /login
app.include_router(forgot_password.router)  # Also register without prefix for /auth/forgot-password
app.include_router(business_analyzer.router)  # internally prefix /api/business
app.include_router(user_stats.router)  # internally prefix /api/user
app.include_router(admin.router, prefix="/api") # prefix /api to match frontend /api/admin
app.include_router(paypal.router) # endpoints start with /api/paypal
app.include_router(flutterwave.router) # internally prefix /api/payments
app.include_router(stripe.router) # internally prefix /api/stripe
app.include_router(customer_service.router, prefix="/api") # internally prefix /api/customer-service
app.include_router(reviews.router, prefix="/api") # endpoints: /api/reviews, /api/admin/reviews
app.include_router(alerts.router, prefix="/api")
app.include_router(insights.router, prefix="/api")  # internally prefix /api
app.include_router(referrals.router, prefix="/api")
app.include_router(earnings.router, prefix="/api")
app.include_router(commissions.router, prefix="/api") # internally prefix /api/commissions
app.include_router(revenue.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(user_settings.router, prefix="/api")  # User settings
app.include_router(user_missions.router, prefix="/api")  # Missions system
app.include_router(user_profile.router)  # User profile — prefix /api/profile is set internally
app.include_router(stripe_connect.router)  # router itself carries prefix="/api/stripe/connect"
app.include_router(security.router, prefix="/api")
app.include_router(firewall_scanner.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(permissions.router, prefix="/api")
app.include_router(signals.router, prefix="/api")

app.include_router(dashboard.router, prefix="/api")
app.include_router(admin_content.router, prefix="/api")
app.include_router(email_service.router)
app.include_router(notifications.router, prefix="/api")
from api.routes.community import community as community_routes
app.include_router(community_routes.router)  # internally prefixed /api/community
from api.routes.marketplace import marketplace as marketplace_routes
app.include_router(marketplace_routes.router)  # internally prefixed /api/marketplace
from api.routes.admin.mvp_features import admin_router as mvp_admin_router, public_router as mvp_public_router
app.include_router(mvp_admin_router)  # /api/admin/mvp-features
app.include_router(mvp_public_router)  # /api/mvp-features + /api/mvp-features/stream

# Note: Index/catch-all router removed as we're using Next.js frontend
