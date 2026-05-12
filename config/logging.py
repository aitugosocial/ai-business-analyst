# config/logging.py
"""
Logging configuration for both local development and cloud deployments.

This module provides a unified logging setup that works across all platforms:
- Local development (colored console output)
- Railway (stdout with JSON formatting)
- DigitalOcean (stdout with structured logs)
- Render (stdout with timestamps)
- Heroku (stdout with app metrics)

Features:
- Automatic platform detection
- Colored logs for local development
- Structured logs for cloud (easier filtering)
- Request ID tracking
- Performance metrics
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone


class CloudFormatter(logging.Formatter):
    """
    Formatter for cloud platforms - outputs structured JSON logs.
    Makes it easier to filter and search logs in cloud dashboards.
    """

    def format(self, record):
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id

        if hasattr(record, "duration"):
            log_data["duration_ms"] = record.duration

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class LocalFormatter(logging.Formatter):
    """
    Formatter for local development - colored output with readable format.
    """

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
        "RESET": "\033[0m",  # Reset
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        reset = self.COLORS["RESET"]

        # Format: [TIME] LEVEL - module.function:line - message
        formatted = (
            f"{color}[{datetime.now().strftime('%H:%M:%S')}] "
            f"{record.levelname:8s}{reset} - "
            f"{record.name}:{record.lineno} - "
            f"{record.getMessage()}"
        )

        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


def is_cloud_environment():
    """
    Detect if running in a cloud environment.
    """
    cloud_indicators = [
        "RAILWAY_ENVIRONMENT",  # Railway
        "RENDER",  # Render
        "DO_APP_NAME",  # DigitalOcean
        "DYNO",  # Heroku
        "AWS_EXECUTION_ENV",  # AWS
        "GOOGLE_CLOUD_PROJECT",  # Google Cloud
    ]
    return any(os.getenv(key) for key in cloud_indicators)


def setup_logging(level=logging.INFO, log_sql=False, log_requests=True):
    """
    Configure logging for the application.

    Args:
        level: Logging level (default: INFO)
        log_sql: Whether to log SQL queries (default: False, set True for debugging)
        log_requests: Whether to log HTTP requests (default: True)

    Returns:
        Logger instance
    """

    # Determine environment
    is_cloud = is_cloud_environment()

    # Choose formatter
    if is_cloud:
        formatter = CloudFormatter()
        print("🌐 Cloud environment detected - using structured JSON logging")
    else:
        formatter = LocalFormatter()
        print("💻 Local environment detected - using colored console logging")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    root_logger.handlers = []

    # Add stdout handler (works on all platforms)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Configure library loggers

    # SQLAlchemy (database queries)
    if log_sql:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    else:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # Uvicorn (web server)
    if log_requests:
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    else:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # FastAPI
    logging.getLogger("fastapi").setLevel(logging.INFO)

    # Reduce noise from other libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return root_logger


# Convenience function for application code
def get_logger(name):
    """
    Get a logger for a module.

    Usage:
        from config.logging import get_logger
        logger = get_logger(__name__)
        logger.info("Hello world")
    """
    return logging.getLogger(name)


# Request ID middleware (for FastAPI)
class RequestIDMiddleware:
    """
    Middleware to add unique request IDs to logs.
    Helps trace requests across microservices.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            import uuid

            request_id = str(uuid.uuid4())[:8]
            scope["request_id"] = request_id

        await self.app(scope, receive, send)


# Example usage
if __name__ == "__main__":
    # Test logging in both environments
    logger = setup_logging(level=logging.DEBUG)

    test_logger = get_logger(__name__)
    test_logger.debug("Debug message - very detailed")
    test_logger.info("Info message - general information")
    test_logger.warning("Warning message - something unusual")
    test_logger.error("Error message - something failed")

    try:
        1 / 0
    except Exception:
        test_logger.exception("Exception occurred - includes traceback")
