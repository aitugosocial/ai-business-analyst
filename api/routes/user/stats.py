# api/routes/user_stats.py
"""
User Statistics API Routes
Provides user-specific stats like total analyses count.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date
from datetime import datetime, timedelta, timezone

from database.pg_connections import get_db
from database.pg_models import BusinessAnalysis, User, Commission, Referral
from api.routes.auth.login import get_current_user

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["User Stats"])


@router.get("/stats")
async def get_user_stats(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        # Get user ID robustly (matching business_analyzer logic)
        user_id = None
        if isinstance(current_user, dict):
            user_id = current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
            if not user_id and "user" in current_user:
                user_data = current_user["user"]
                if isinstance(user_data, dict):
                    user_id = user_data.get("id") or user_data.get("user_id")
                elif hasattr(user_data, 'id'):
                    user_id = user_data.id
        elif hasattr(current_user, "id"):
             user_id = current_user.id
        
        # Handle string 'sub' (email or id)
        if user_id and str(user_id).isdigit():
            user_id = int(str(user_id))
        elif user_id and "@" in str(user_id):
             user_obj = db.query(User).filter(User.email == str(user_id)).first()
             if user_obj:
                 user_id = user_obj.id
             else:
                 logger.warning(f"Stats: User not found for email {user_id}")
                 user_id = None

        if not user_id:
             logger.warning(f"Stats: Could not resolve user_id from token: {current_user}")
             raise HTTPException(status_code=401, detail="User identity not found in token")

        # 1. Analyses Stats
        total_analyses = 0
        avg_confidence = 0
        total_seconds = 0.0

        try:
            total_analyses = db.query(func.count(BusinessAnalysis.id)).filter(
                BusinessAnalysis.user_id == user_id
            ).scalar() or 0
        except Exception as e:
             logger.exception(f"Failed to count analyses for user {user_id}: {e}")

        try:
            avg_confidence = db.query(func.avg(BusinessAnalysis.confidence_score)).filter(
                BusinessAnalysis.user_id == user_id,
                BusinessAnalysis.confidence_score.isnot(None)
            ).scalar() or 0

            analyses_with_duration = db.query(BusinessAnalysis.duration).filter(
                BusinessAnalysis.user_id == user_id,
                BusinessAnalysis.duration.isnot(None)
            ).all()

            for row in analyses_with_duration:
                if row.duration and isinstance(row.duration, str):
                    try:
                        cleaned = row.duration.lower().replace('s', '').strip()
                        total_seconds += float(cleaned)
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Analysis stats partial fail (confidence/duration) for user {user_id}: {e}")

        # 2. Commission Stats
        total_commissions = 0.0
        paid_commissions = 0.0

        try:
            total_commissions = db.query(func.sum(Commission.amount)).filter(
                Commission.user_id == user_id,
                Commission.status.in_(['paid', 'pending', 'processing', 'approved'])
            ).scalar() or 0.0

            paid_commissions = db.query(func.sum(Commission.amount)).filter(
                Commission.user_id == user_id,
                Commission.status == 'paid'
            ).scalar() or 0.0
        except Exception as e:
             logger.warning(f"Commission stats partial fail for user {user_id}: {e}")

        # 3. Referral Stats
        total_referrals = 0
        referrals_this_month = 0

        try:
            user_obj = db.query(User).filter(User.id == user_id).first()
            if user_obj:
                total_referrals = user_obj.referral_count or 0

            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            referrals_this_month = db.query(func.count(Referral.id)).filter(
                Referral.referrer_id == user_id,
                Referral.created_at >= month_start
            ).scalar() or 0
        except Exception as e:
            logger.warning(f"Referral stats partial fail for user {user_id}: {e}")

        result = {
            "total_analyses": int(total_analyses),
            "avg_confidence": int(avg_confidence),
            "total_duration_seconds": int(total_seconds),
            "total_duration_formatted": f"{int(total_seconds / 60)}m {int(total_seconds % 60)}s" if total_seconds > 60 else f"{int(total_seconds)}s",
            "total_commissions": float(total_commissions),
            "paid_commissions": float(paid_commissions),
            "total_referrals": int(total_referrals),
            "referrals_this_month": int(referrals_this_month)
        }
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"/api/user/stats CRITICAL FAIL: {e}")
        # Return valid zero structure to prevent dashboard crash
        return {
            "total_analyses": 0,
            "avg_confidence": 0,
            "total_duration_seconds": 0,
            "total_duration_formatted": "0s",
            "total_commissions": 0.0,
            "paid_commissions": 0.0,
            "total_referrals": 0,
            "referrals_this_month": 0
        }

# ─── Level config (AiTugo Chops Framework v6 — Levels 1–51) ────────────────
LEVELS = [
    # Tier 1 – Foundation
    {"level": 1,  "title": "Builder",               "tier": "Foundation",  "min_chops": 0,              "max_chops": 999},
    {"level": 2,  "title": "Initiate",              "tier": "Foundation",  "min_chops": 1_000,          "max_chops": 1_999},
    {"level": 3,  "title": "Apprentice",            "tier": "Foundation",  "min_chops": 2_000,          "max_chops": 3_999},
    {"level": 4,  "title": "Forgehand",             "tier": "Foundation",  "min_chops": 4_000,          "max_chops": 7_999},
    {"level": 5,  "title": "Pioneer",               "tier": "Foundation",  "min_chops": 8_000,          "max_chops": 11_999},
    {"level": 6,  "title": "Vision Crafter",        "tier": "Foundation",  "min_chops": 12_000,         "max_chops": 16_999},
    {"level": 7,  "title": "Operator",              "tier": "Foundation",  "min_chops": 17_000,         "max_chops": 21_999},
    {"level": 8,  "title": "Trailblazer",           "tier": "Foundation",  "min_chops": 22_000,         "max_chops": 30_999},
    {"level": 9,  "title": "Strategist",            "tier": "Foundation",  "min_chops": 31_000,         "max_chops": 34_999},
    {"level": 10, "title": "Groundbreaker",         "tier": "Foundation",  "min_chops": 35_000,         "max_chops": 40_999},
    # Tier 2 – Growth
    {"level": 11, "title": "Connector",             "tier": "Growth",      "min_chops": 41_000,         "max_chops": 50_999},
    {"level": 12, "title": "Catalyst",              "tier": "Growth",      "min_chops": 51_000,         "max_chops": 61_999},
    {"level": 13, "title": "Innovator",             "tier": "Growth",      "min_chops": 62_000,         "max_chops": 73_999},
    {"level": 14, "title": "Navigator",             "tier": "Growth",      "min_chops": 74_000,         "max_chops": 86_999},
    {"level": 15, "title": "Signal Weaver",         "tier": "Growth",      "min_chops": 87_000,         "max_chops": 101_999},
    {"level": 16, "title": "Momentum Maker",        "tier": "Growth",      "min_chops": 102_000,        "max_chops": 118_999},
    {"level": 17, "title": "Orchestrator",          "tier": "Growth",      "min_chops": 119_000,        "max_chops": 137_999},
    {"level": 18, "title": "Insight Sculptor",      "tier": "Growth",      "min_chops": 138_000,        "max_chops": 159_999},
    {"level": 19, "title": "Opportunity Engineer",  "tier": "Growth",      "min_chops": 160_000,        "max_chops": 185_999},
    {"level": 20, "title": "Alchemist",             "tier": "Growth",      "min_chops": 186_000,        "max_chops": 214_999},
    # Tier 3 – Mastery
    {"level": 21, "title": "Growth Architect",      "tier": "Mastery",     "min_chops": 215_000,        "max_chops": 250_999},
    {"level": 22, "title": "Commerce Marshal",      "tier": "Mastery",     "min_chops": 251_000,        "max_chops": 301_999},
    {"level": 23, "title": "Mentor Maven",          "tier": "Mastery",     "min_chops": 302_000,        "max_chops": 365_999},
    {"level": 24, "title": "Blueprint Maker",       "tier": "Mastery",     "min_chops": 366_000,        "max_chops": 444_999},
    {"level": 25, "title": "Insight Sage",          "tier": "Mastery",     "min_chops": 445_000,        "max_chops": 537_999},
    {"level": 26, "title": "Community Leader",      "tier": "Mastery",     "min_chops": 538_000,        "max_chops": 654_999},
    {"level": 27, "title": "Strategy Guardian",     "tier": "Mastery",     "min_chops": 655_000,        "max_chops": 798_999},
    {"level": 28, "title": "Network Navigator",     "tier": "Mastery",     "min_chops": 799_000,        "max_chops": 966_999},
    {"level": 29, "title": "Ecosystem Builder",     "tier": "Mastery",     "min_chops": 967_000,        "max_chops": 1_169_999},
    {"level": 30, "title": "Market Sculptor",       "tier": "Mastery",     "min_chops": 1_170_000,      "max_chops": 1_436_999},
    # Tier 4 – Prestige
    {"level": 31, "title": "Culture Steward",       "tier": "Prestige",    "min_chops": 1_437_000,      "max_chops": 1_865_999},
    {"level": 32, "title": "Ecosystem Keeper",      "tier": "Prestige",    "min_chops": 1_866_000,      "max_chops": 2_398_999},
    {"level": 33, "title": "Market Commander",      "tier": "Prestige",    "min_chops": 2_399_000,      "max_chops": 3_078_999},
    {"level": 34, "title": "Vision Chancellor",     "tier": "Prestige",    "min_chops": 3_079_000,      "max_chops": 3_927_999},
    {"level": 35, "title": "Growth Sage",           "tier": "Prestige",    "min_chops": 3_928_000,      "max_chops": 4_947_999},
    {"level": 36, "title": "Opportunity Sentinel",  "tier": "Prestige",    "min_chops": 4_948_000,      "max_chops": 6_172_999},
    {"level": 37, "title": "Innovation Regent",     "tier": "Prestige",    "min_chops": 6_173_000,      "max_chops": 7_699_999},
    {"level": 38, "title": "Legacy Maker",          "tier": "Prestige",    "min_chops": 7_700_000,      "max_chops": 9_549_999},
    {"level": 39, "title": "Empire Guardian",       "tier": "Prestige",    "min_chops": 9_550_000,      "max_chops": 11_931_999},
    {"level": 40, "title": "Luminary",              "tier": "Prestige",    "min_chops": 11_932_000,     "max_chops": 14_916_999},
    # Tier 5 – Legendary
    {"level": 41, "title": "Vision Keeper",         "tier": "Legendary",   "min_chops": 14_917_000,     "max_chops": 18_645_999},
    {"level": 42, "title": "Momentum Master",       "tier": "Legendary",   "min_chops": 18_646_000,     "max_chops": 23_880_999},
    {"level": 43, "title": "Domain Sage",           "tier": "Legendary",   "min_chops": 23_881_000,     "max_chops": 30_711_999},
    {"level": 44, "title": "The Chairman",          "tier": "Legendary",   "min_chops": 30_712_000,     "max_chops": 39_460_999},
    {"level": 45, "title": "Reality Shaper",        "tier": "Legendary",   "min_chops": 39_461_000,     "max_chops": 50_000_999},
    {"level": 46, "title": "Epoch Driver",          "tier": "Legendary",   "min_chops": 50_001_000,     "max_chops": 65_501_299},
    {"level": 47, "title": "Ascendant Visionary",   "tier": "Legendary",   "min_chops": 65_501_300,     "max_chops": 86_951_699},
    {"level": 48, "title": "Super Creator",         "tier": "Legendary",   "min_chops": 86_951_700,     "max_chops": 100_000_999},
    {"level": 49, "title": "Supreme Creator",       "tier": "Legendary",   "min_chops": 100_001_000,    "max_chops": 249_999_999},
    {"level": 50, "title": "Top Boss (Oga at The Top)", "tier": "Legendary","min_chops": 250_000_000,   "max_chops": 49_999_999_999},
    {"level": 51, "title": "The Oracle",            "tier": "Legendary",   "min_chops": 50_000_000_000, "max_chops": 999_999_999_999},
]

def _resolve_level(chops: int) -> dict:
    current = LEVELS[0]
    for lvl in LEVELS:
        if chops >= lvl["min_chops"]:
            current = lvl
    next_lvl = next((l for l in LEVELS if l["level"] == current["level"] + 1), None)
    return {
        "level": current["level"],
        "level_title": current["title"],
        "level_tier": current.get("tier", "Foundation"),
        "next_level": next_lvl["level"] if next_lvl else current["level"],
        "next_level_title": next_lvl["title"] if next_lvl else current["title"],
        "next_level_chops": next_lvl["min_chops"] if next_lvl else current["max_chops"],
        "chops_to_next_level": max(0, (next_lvl["min_chops"] if next_lvl else current["max_chops"]) - chops),
        "level_progress_pct": min(100, round(
            ((chops - current["min_chops"]) / max(1, (next_lvl["min_chops"] if next_lvl else current["max_chops"]) - current["min_chops"])) * 100
        )),
    }


def _compute_streak(db: Session, user_id: int) -> int:
    """Count consecutive days with at least one analysis, going backwards from today.
    Uses a single query fetching all active days in the past year instead of one query per day.
    """
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=365)

    rows = db.query(
        cast(BusinessAnalysis.created_at, Date).label("day")
    ).filter(
        BusinessAnalysis.user_id == user_id,
        BusinessAnalysis.created_at >= cutoff,
    ).distinct().all()

    active_days = {row.day for row in rows}

    streak = 0
    check_date = today
    # Allow today to have no activity yet without breaking yesterday's streak
    if today not in active_days:
        check_date = today - timedelta(days=1)

    while check_date >= cutoff:
        if check_date in active_days:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break
    return streak


def _compute_badges(analyses_count: int, streak: int, missions_done: int, chops: int) -> list:
    """Return list of badge dicts with earned status based on real data."""
    all_badges = [
        {"id": 1, "name": "First Analysis",  "icon": "Target",  "earned": analyses_count >= 1,  "color": "orange",  "description": "Complete your first analysis"},
        {"id": 2, "name": "7-Day Streak",     "icon": "Flame",   "earned": streak >= 7,           "color": "orange",  "description": "Maintain a 7-day activity streak"},
        {"id": 3, "name": "Closer",           "icon": "CheckCircle2", "earned": missions_done >= 3, "color": "purple", "description": "Complete 3 missions"},
        {"id": 4, "name": "Speed Operator",   "icon": "Zap",     "earned": analyses_count >= 10,  "color": "blue",    "description": "Complete 10 analyses"},
        {"id": 5, "name": "Strategic Thinker","icon": "Brain",   "earned": analyses_count >= 25,  "color": "indigo",  "description": "Complete 25 analyses"},
        {"id": 6, "name": "Community Pillar", "icon": "Users",   "earned": chops >= 1000,         "color": "green",   "description": "Earn 1,000 Chops"},
        {"id": 7, "name": "Elite Founder",    "icon": "Crown",   "earned": analyses_count >= 100, "color": "amber",   "description": "Complete 100 analyses"},
    ]
    return all_badges


@router.get("/progress")
async def get_user_progress(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Gamification / progress data for the current user.
    Returns: chops, level, streak, execution score, badges, missions stats.
    """
    try:
        user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        chops = user.total_chops or 0

        # Analyses count (used as proxy for score dimensions)
        total_analyses = db.query(func.count(BusinessAnalysis.id)).filter(
            BusinessAnalysis.user_id == user_id
        ).scalar() or 0

        # 30-day analyses (for "this month" missions done metric)
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        analyses_30d = db.query(func.count(BusinessAnalysis.id)).filter(
            BusinessAnalysis.user_id == user_id,
            BusinessAnalysis.created_at >= thirty_days_ago
        ).scalar() or 0

        # Execution streak
        streak = _compute_streak(db, user_id)

        # Level info
        level_info = _resolve_level(chops)

        # Execution score — weighted composite
        avg_confidence = db.query(func.avg(BusinessAnalysis.confidence_score)).filter(
            BusinessAnalysis.user_id == user_id,
            BusinessAnalysis.confidence_score.isnot(None)
        ).scalar() or 0
        streak_score = min(100, streak * 14)
        analyses_score = min(100, total_analyses * 5)
        execution_score = int(
            (float(avg_confidence) * 0.35) +
            (streak_score * 0.30) +
            (analyses_score * 0.35)
        )

        badges = _compute_badges(total_analyses, streak, analyses_30d, chops)

        return {
            "total_chops": chops,
            "execution_streak": streak,
            "total_analyses": total_analyses,
            "analyses_last_30_days": analyses_30d,
            "execution_score": execution_score,
            "execution_metrics": [
                {"name": "Consistency",       "score": min(100, streak_score),   "color": "bg-green-500",  "textColor": "text-green-500"},
                {"name": "Action Completion", "score": min(100, analyses_score), "color": "bg-orange-500", "textColor": "text-orange-500"},
                {"name": "Execution Speed",   "score": min(100, int(float(avg_confidence) * 0.9)), "color": "bg-orange-500", "textColor": "text-orange-500"},
                {"name": "Follow-Through",    "score": min(100, int(float(avg_confidence))),        "color": "bg-blue-500",   "textColor": "text-blue-500"},
            ],
            **level_info,
            "badges": badges,
            "better_than_pct": min(98, execution_score + 10),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Progress endpoint error for user: {e}")
        raise HTTPException(status_code=500, detail=str(e))
