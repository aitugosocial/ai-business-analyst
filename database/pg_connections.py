# db/pg_connections.py
"""
PostgreSQL database connection manager.

This module handles PostgreSQL connections for any cloud deployment platform
(Railway, DigitalOcean, Render, Heroku, AWS, etc.) or local development.

Environment Variables Required:
    DATABASE_URL: Full PostgreSQL connection URL
    Format: postgresql://user:password@host:port/database?sslmode=require

Logging:
    - Outputs to stdout (works in all cloud environments)
    - Compatible with Railway, DigitalOcean, Render, Heroku, etc.
"""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    import os.path

    # Get the project root directory (one level up from database/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Try .env.local first (local development), fallback to .env
    env_local = os.path.join(project_root, '.env.local')
    env_file = os.path.join(project_root, '.env')

    if os.path.exists(env_local):
        load_dotenv(env_local)
        print(f"✓ Loaded environment from {env_local}")
    elif os.path.exists(env_file):
        load_dotenv(env_file)
        print(f"✓ Loaded environment from {env_file}")
    else:
        load_dotenv()  # Try loading from default locations
        print("✓ Loaded environment from default location")
except ImportError:
    # python-dotenv not installed, will use system environment variables
    print("⚠️  python-dotenv not installed, using system environment variables")
    pass

# Base class for all ORM models
Base = declarative_base()

# Get database URL from environment
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: DATABASE_URL environment variable not set!")
    print("Please set DATABASE_URL in your .env file or environment")
    print("Format: postgresql://user:password@host:port/database")
    sys.exit(1)

print("✓ Connecting to PostgreSQL database...")

try:
    # Create PostgreSQL engine
    # Connection pooling settings optimized for cloud deployments like Neon/Railway
    # pool_recycle: Set to 120 (2 mins) because Neon's proxy/pooler often terminates
    # idle connections after 5 minutes. Recycling sooner prevents "SSL SYSCALL error: EOF detected".
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,       # Health-check connection before each use
        pool_recycle=90,          # Recycle before Neon's 300s idle timeout
        pool_timeout=20,          # Fail fast — don't block the request for 30 s
        pool_size=15,             # Raised from 10: supports more concurrent requests
        max_overflow=30,          # Raised from 20: burst headroom for heavy pages
        echo=False,
        # execution_options for statement-level timeout — prevents runaway queries
        # from holding a connection and starving other requests.
        execution_options={"options": "-c statement_timeout=15000"},  # 15 s hard cap
        connect_args={
            "sslmode": "require",
            "channel_binding": "require",
            "keepalives": 1,
            "keepalives_idle": 20,   # Reduced from 30: detect drops faster
            "keepalives_interval": 5,
            "keepalives_count": 3,
            "connect_timeout": 10,   # Fail connection attempt after 10 s
        },
    )

    # Test the connection
    with engine.connect() as conn:
        print("✓ Successfully connected to PostgreSQL!")

    # Attach an event listener to silence harmless "SSL connection has been closed unexpectedly" errors
    # during connection pool check-ins.
    from sqlalchemy import event
    import psycopg2

    # Provide a resilient callback for connection resets
    @event.listens_for(engine, "reset")
    def _gracefully_handle_reset(dbapi_connection, connection_record, reset_state=None): # reset_state is for SQLAlchemy 2.0+
        try:
            dbapi_connection.rollback()
        except psycopg2.OperationalError as e:
            # If the server closed the connection or dropped due to network issues,
            # invalidate the record quietly instead of bubbling up a traceback to the QueuePool logger.
            if "closed unexpectedly" in str(e) or "terminating connection" in str(e) or "already closed" in str(e):
                connection_record.invalidate()
            else:
                raise
        except Exception as e:
            # Re-raise any other unexpected exceptions
            raise

except Exception as e:
    print("❌ Failed to connect to PostgreSQL database!")
    error_msg = str(e)
    if "could not translate host name" in error_msg or "Temporary failure in name resolution" in error_msg:
        print("\n⚠️  DIAGNOSIS: DNS RESOLUTION ERROR DETECTED")
        print("This is likely a WSL2 networking issue. Try restarting WSL:")
        print(" PowerShell (Admin): wsl --shutdown")
        print(" Then restart your backend.")

    print(f"Error: {e}")
    print(f"URL format: {DATABASE_URL.split('@')[0]}@***")
    sys.exit(1)

# Create SessionLocal class for database sessions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """
    Initialize database tables.
    Creates all tables defined in the models.
    """
    Base.metadata.create_all(bind=engine)
    print("✓ Database tables created successfully")


def get_db_info():
    """
    Get information about current database connection.

    Returns:
        dict: Database connection info
    """
    return {
        "type": "postgresql",
        "driver": "psycopg2",
        "host": str(engine.url).split("@")[-1].split("/")[0]
        if "@" in str(engine.url)
        else "unknown",
        "database": engine.url.database,
        "url": str(engine.url).replace(str(engine.url.password), "***")
        if engine.url.password
        else str(engine.url),
        "pool_size": engine.pool.size() if hasattr(engine.pool, "size") else None,
    }


def get_db():
    """
    Dependency function for FastAPI routes.
    Yields a database session and closes it after use.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
