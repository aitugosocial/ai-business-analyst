# api/cache.py
"""
Caching configuration and utilities using Redis with fastapi-cache2
Falls back to in-memory cache if Redis is unavailable

IMPORTANT: The @cache decorator from fastapi-cache2 does NOT work with
FastAPI endpoints that use Depends() for authentication. Instead, use
the manual caching functions provided below (get_cached, set_cached).
"""

import os
import json
import logging
from typing import Optional, Any
from datetime import timedelta
from functools import wraps
from fastapi import Request, Response
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from redis import asyncio as aioredis

logger = logging.getLogger(__name__)

# Redis connection instance
redis_client: Optional[aioredis.Redis] = None

# In-memory fallback cache (thread-safe dict)
_memory_cache: dict[str, tuple[Any, float]] = {}
import time


async def init_cache():
    """
    Initialize caching backend (Redis or fallback to in-memory)
    Call this during FastAPI startup event.

    IMPORTANT: Only connects to Redis if REDIS_URL is explicitly set.
    This prevents startup hangs on platforms like Railway when no Redis
    service is provisioned (would otherwise block on localhost:6379).
    In-memory fallback is always available and sufficient for most use cases.
    """
    global redis_client

    # Get Redis URL from environment (optional — do NOT default to localhost)
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        # Fast path: no Redis configured (common on Railway, Render, etc.)
        logger.info("📦 No REDIS_URL set — initializing in-memory cache (no network connection attempted)")
        FastAPICache.init(InMemoryBackend(), prefix="aianalyst:")
        redis_client = None
        return

    try:
        # Attempt to connect to Redis only when explicitly configured
        redis_client = await aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )

        # Test connection
        await redis_client.ping()

        # Initialize FastAPICache with Redis backend
        FastAPICache.init(RedisBackend(redis_client), prefix="aianalyst:")

        logger.info(f"✅ Redis cache initialized successfully (URL: {redis_url})")

    except Exception as e:
        logger.warning(f"⚠️  Redis connection failed: {e}")
        logger.info("📦 Falling back to in-memory cache")

        # Fallback to in-memory cache
        FastAPICache.init(InMemoryBackend(), prefix="aianalyst:")
        redis_client = None


async def close_cache():
    """
    Close Redis connection gracefully
    Call this during FastAPI shutdown event
    """
    global redis_client

    if redis_client:
        try:
            await redis_client.close()
            logger.info("✅ Redis connection closed")
        except Exception as e:
            logger.error(f"❌ Error closing Redis: {e}")


def cache_key_builder(
    func,
    namespace: str = "",
    request: Request = None,
    response: Response = None,
    *args,
    **kwargs,
):
    """
    Custom cache key builder for fastapi-cache2
    Builds hierarchical cache keys with request parameters

    Example key: "aianalyst:admin:users:page=1:limit=10:status=all"
    """
    from fastapi_cache import FastAPICache

    prefix = FastAPICache.get_prefix()
    cache_key = f"{prefix}{namespace}:{func.__module__}:{func.__name__}"

    # Add query parameters to cache key
    if request and request.query_params:
        query_string = ":".join(
            f"{k}={v}" for k, v in sorted(request.query_params.items())
        )
        cache_key = f"{cache_key}:{query_string}"

    return cache_key


async def invalidate_cache_pattern(pattern: str):
    """
    Invalidate all cache keys matching a pattern

    Args:
        pattern: Redis pattern (e.g., "aianalyst:admin:users:*")

    Example:
        await invalidate_cache_pattern("aianalyst:admin:users:*")
    """
    global redis_client

    if not redis_client:
        logger.warning("⚠️  Cannot invalidate cache pattern: Redis not available")
        return

    try:
        # Find all keys matching pattern
        keys = []
        async for key in redis_client.scan_iter(match=pattern):
            keys.append(key)

        # Delete all matching keys
        if keys:
            await redis_client.delete(*keys)
            logger.info(f"✅ Invalidated {len(keys)} cache keys matching '{pattern}'")
        else:
            logger.debug(f"No cache keys found matching '{pattern}'")

    except Exception as e:
        logger.error(f"❌ Error invalidating cache pattern '{pattern}': {e}")


async def invalidate_user_cache(user_id: int):
    """
    Invalidate all caches for a specific user

    Args:
        user_id: User ID to invalidate caches for

    Example:
        await invalidate_user_cache(123)
    """
    pattern = f"aianalyst:*user*{user_id}*"
    await invalidate_cache_pattern(pattern)


async def clear_all_caches():
    """
    Clear all application caches
    Use with caution - only for admin operations
    """
    global redis_client

    if not redis_client:
        logger.warning("⚠️  Cannot clear caches: Redis not available")
        return

    try:
        # Delete all keys with our prefix
        pattern = "aianalyst:*"
        keys = []
        async for key in redis_client.scan_iter(match=pattern):
            keys.append(key)

        if keys:
            await redis_client.delete(*keys)
            logger.info(f"✅ Cleared all caches ({len(keys)} keys)")
        else:
            logger.info("No caches to clear")

    except Exception as e:
        logger.error(f"❌ Error clearing all caches: {e}")


# Export cache decorator for easy use
__all__ = [
    "cache",
    "init_cache",
    "close_cache",
    "cache_key_builder",
    "invalidate_cache_pattern",
    "invalidate_user_cache",
    "clear_all_caches",
    "get_cached",
    "set_cached",
    "delete_cached",
]


# ====================
# MANUAL CACHING FUNCTIONS
# (Works with FastAPI Depends())
# ====================

async def get_cached(key: str) -> Optional[Any]:
    """
    Get a value from cache by key.
    Works with both Redis and in-memory fallback.

    Args:
        key: Cache key (automatically prefixed with 'aianalyst:')

    Returns:
        Cached value or None if not found/expired

    Example:
        cached = await get_cached(f"alerts:user:{user_id}")
        if cached:
            return cached
    """
    global redis_client, _memory_cache

    full_key = f"aianalyst:{key}"

    try:
        if redis_client:
            # Try Redis
            value = await redis_client.get(full_key)
            if value:
                return json.loads(value)
        else:
            # Fallback to in-memory
            if full_key in _memory_cache:
                value, expiry = _memory_cache[full_key]
                if expiry > time.time():
                    return value
                else:
                    # Expired - remove it
                    _memory_cache.pop(full_key, None)
    except Exception as e:
        logger.warning(f"Cache get error for '{key}': {e}")

    return None


async def set_cached(key: str, value: Any, ttl_seconds: int = 300):
    """
    Set a value in cache with TTL (time to live).
    Works with both Redis and in-memory fallback.

    Args:
        key: Cache key (automatically prefixed with 'aianalyst:')
        value: Value to cache (must be JSON serializable)
        ttl_seconds: Time to live in seconds (default: 5 minutes)

    Example:
        await set_cached(f"alerts:user:{user_id}", alerts_data, ttl_seconds=120)
    """
    global redis_client, _memory_cache

    full_key = f"aianalyst:{key}"

    try:
        if redis_client:
            # Store in Redis with TTL
            await redis_client.setex(
                full_key,
                ttl_seconds,
                json.dumps(value, default=str)
            )
            logger.debug(f"Cached '{key}' in Redis (TTL: {ttl_seconds}s)")
        else:
            # Store in memory with expiry timestamp
            _memory_cache[full_key] = (value, time.time() + ttl_seconds)
            logger.debug(f"Cached '{key}' in memory (TTL: {ttl_seconds}s)")
    except Exception as e:
        logger.warning(f"Cache set error for '{key}': {e}")


async def delete_cached(key: str):
    """
    Delete a specific cache key.

    Args:
        key: Cache key (automatically prefixed with 'aianalyst:')

    Example:
        await delete_cached(f"alerts:user:{user_id}")
    """
    global redis_client, _memory_cache

    full_key = f"aianalyst:{key}"

    try:
        if redis_client:
            await redis_client.delete(full_key)
        else:
            _memory_cache.pop(full_key, None)

        logger.debug(f"Deleted cache key '{key}'")
    except Exception as e:
        logger.warning(f"Cache delete error for '{key}': {e}")


# TTL constants for common cache durations
class CacheTTL:
    """Common cache TTL values in seconds"""
    SHORT = 60  # 1 minute - for rapidly changing data
    MEDIUM = 300  # 5 minutes - for moderately changing data
    LONG = 600  # 10 minutes - for stable data
    VERY_LONG = 1800  # 30 minutes - for rarely changing data
    HOUR = 3600  # 1 hour
    DAY = 86400  # 24 hours
