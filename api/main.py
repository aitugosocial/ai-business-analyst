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
from api.routes.admin import admin, security, firewall_scanner, revenue, users, dashboard, settings, content as admin_content

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
    logger.info(f"INCOMING REQUEST: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"RESPONSE STATUS: {request.method} {request.url.path} -> {response.status_code}")
    return response


_origins_base = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
    "http://localhost:8080",
]
# Allow additional origins from environment (comma-separated list)
_extra_origins = os.getenv("ALLOWED_ORIGINS", "")
if _extra_origins:
    _origins_base.extend([o.strip() for o in _extra_origins.split(",") if o.strip()])

origins = _origins_base


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
    Health check endpoint for cloud platform monitoring.
    Returns 200 OK if app is running and database is connected.
    """
    try:
        db_info = get_db_info()
        return {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "database": {"type": db_info.get("type"), "connected": True},
            "version": "1.0.0",
        }
    except Exception as e:
        return {"status": "unhealthy", "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.get("/api/beta-status")
async def get_beta_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Beta status endpoint for the frontend.
    Directly exposed at /api/beta-status as expected by the React-based components.
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
    """Background task to run vulnerability scans every 15 minutes"""
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
async def create_admin_user(db: Session=Depends(get_db)):
    admin_email = os.getenv("admin_email","admin@gmail.com")
    admin_password = os.getenv("admin_password","admin123")
    password = pwd_context.hash(admin_password)
    admin_name = os.getenv("admin_name","Admin")

    try:
        existing_admin = db.query(User).filter(User.email == admin_email).first()
        if existing_admin:
            logger.info("✓ Admin user already exists")
            return

        new_admin = User(
                name="Admin",
                email=admin_email,
                password= password,
                confirm_password = password,
                is_admin=True
        )
        db.add(new_admin)
        db.commit()
        logger.info("✓ Admin user created",admin_email)
    except Exception as e:
        logger.error(f"❌ Failed to create admin user: {e}")


# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database tables and caching on application startup"""
    # Start scheduled scans task
    asyncio.create_task(run_scheduled_scans())
    try:
        init_db()
        db_info = get_db_info()
        logger.info(f"✓ Database initialized: {db_info['type']} at {db_info['host']}")

        # Auto-migration for is_active column
        db = SessionLocal()
        try:
            # Check if column exists (PostgreSQL specific, but 'ADD COLUMN IF NOT EXISTS' handles it in modern PG)
            # However, IF NOT EXISTS is PG 9.6+. Assuming safe.
            # If SQLite, this syntax might fail. The project uses PG exclusively per line 32.
            # Optimizing partial migrations into fewer round-trips
            # Postgres supports adding multiple columns in one statement
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

                # Subscription table updates
                db.execute(text("""
                    ALTER TABLE subscriptions
                    ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20);
                """))

                # Initialize subscription_status for existing records
                # If end_date < now, it's expired. If status is not successful, it's 'Payment failed'.
                # Otherwise it's active.
                db.execute(text("""
                    UPDATE subscriptions
                    SET subscription_status = CASE
                        WHEN end_date < NOW() THEN 'expired'
                        WHEN status NOT IN ('completed', 'active', 'paid', 'successful') THEN 'Payment failed'
                        ELSE 'active'
                    END
                    WHERE subscription_status IS NULL;
                """))


                # Add recommended_tool_stacks to business_analyses if it doesn't exist
                try:
                    db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS recommended_tool_stacks JSON"))
                except Exception as e:
                    logger.warning(f"Failed to add recommended_tool_stacks to business_analyses: {e}")

                # Add recommendation_mode and single_tool_recommendation to business_analyses
                try:
                    db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS recommendation_mode VARCHAR"))
                    db.execute(text("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS single_tool_recommendation JSONB"))
                except Exception as e:
                    logger.warning(f"Failed to add recommendation mode columns to business_analyses: {e}")

                # Security table fixes
                try:
                    # Rename attempt_time to created_at if it exists
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
                    # Add is_active to firewall_rules if it doesn't exist
                    db.execute(text("ALTER TABLE firewall_rules ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
                except Exception as e:
                    logger.warning(f"Failed to add is_active to firewall_rules: {e}")

                try:
                    # Add created_at and updated_at to system_settings
                    db.execute(text("""
                        ALTER TABLE system_settings
                        ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
                    """))
                except Exception as e:
                    logger.warning(f"Failed to add columns to system_settings: {e}")

                # Create user_notifications table
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
                # If batch fails (e.g. SQLite doesn't support multiple ADD COLUMN), fall back to individual or log
                logger.warning(f"Batch migration warning (will attempt individual if critical): {e}")

            db.commit()
            logger.info("✓ Checked/Added columns to users and reviews tables")
        except Exception as e:
            logger.warning(f"Migration warning: {e}")
            db.rollback()
        finally:
            db.close()

        # Create admin user
        await create_admin_user(SessionLocal())

        # Schema migrations
        logger.info("Running schema migrations...")

        # Add email column to ip_blacklist if it doesn't exist
        db = SessionLocal() # Create a new session for this migration
        try:
            db.execute(text("""
                ALTER TABLE ip_blacklist
                ADD COLUMN IF NOT EXISTS email VARCHAR(255);
            """))
            db.commit()
            logger.info("✓ Added email column to ip_blacklist table")
        except Exception as e:
            logger.warning(f"Email column migration: {e}")
            db.rollback()
        finally:
            db.close() # Close the session

        # Execute security setup SQL
        try:
            # BASE_DIR is defined below, but we can use it here if we define it earlier
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            sql_file = os.path.join(project_root, "database", "security_setup.sql")
            if os.path.exists(sql_file):
                with open(sql_file, "r") as f:
                    sql_content = f.read()

                db = SessionLocal()
                try:

                    # First, drop security_metrics_summary if it exists as a table (not a view)
                    # This allows us to recreate it as a view
                    try:
                        db.execute(text("DROP TABLE IF EXISTS security_metrics_summary CASCADE"))
                        db.commit()
                        logger.info("Dropped existing security_metrics_summary table if present")
                    except Exception as drop_error:
                        logger.debug(f"No table to drop: {drop_error}")
                        db.rollback()

                    # Execute the entire SQL file as one block to preserve function definitions
                    # PostgreSQL can handle multiple statements in one execute call
                    db.execute(text(sql_content))
                    db.commit()
                    logger.info("✓ Security views and triggers initialized from security_setup.sql")

                    # Initialize firewall rules
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

        # Create community tables
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
            logger.info("✓ Community tables created/verified")
        except Exception as e:
            logger.warning(f"Community table migration: {e}")
            db.rollback()

        # Marketplace tables
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
            logger.info("✓ Marketplace tables created/verified")
        except Exception as e:
            logger.warning(f"Marketplace table migration: {e}")
            db.rollback()
        finally:
            db.close()

        # Initialize Redis/in-memory cache
        await init_cache()

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on application shutdown"""
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
