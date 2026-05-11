"""
Admin endpoints for managing MVP feature flags.
GET  /api/admin/mvp-features           — list all features
PATCH /api/admin/mvp-features/{id}     — toggle isInMvp for a feature or sub-page
GET  /api/mvp-features                 — public: customer app reads this
GET  /api/mvp-features/stream          — SSE: push updates to customer apps in real time
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.pg_connections import get_db
from database.pg_models import MvpFeature
from api.routes.dependencies import admin_required

logger = logging.getLogger(__name__)

# ── Routers ───────────────────────────────────────────────────────────────────
admin_router = APIRouter(prefix="/api/admin/mvp-features", tags=["admin-mvp"])
public_router = APIRouter(prefix="/api/mvp-features", tags=["mvp-features"])

# ── In-memory SSE subscriber queue ───────────────────────────────────────────
# Each connected customer-app tab registers a Queue here.  When a flag changes
# we put the new feature list into every queue so all tabs update instantly.
_sse_subscribers: list[asyncio.Queue] = []


def _broadcast(payload: dict) -> None:
    """Push a JSON payload to every active SSE subscriber."""
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_subscribers.remove(q)


# ── Default seed data (mirrors lib/data/mvp-features.ts) ─────────────────────
_SEED_FEATURES = [
    {"name": "Home feed",           "directory": "/dashboard",                              "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Decision Engine",     "directory": "/dashboard/decision-engine",              "is_in_mvp": True,  "has_sub_pages": True,  "is_in_next_feature_launch": False, "sub_pages": [
        {"name": "Result Page",   "directory": "/dashboard/decision-engine/result/[id]",    "is_in_mvp": True,  "is_in_next_feature_launch": False},
        {"name": "Missions Page", "directory": "/dashboard/decision-engine/missions",       "is_in_mvp": False, "is_in_next_feature_launch": True},
        {"name": "History Page",  "directory": "/dashboard/decision-engine/history",        "is_in_mvp": True,  "is_in_next_feature_launch": False},
        {"name": "Progress Page", "directory": "/dashboard/decision-engine/progress",       "is_in_mvp": False, "is_in_next_feature_launch": True},
    ]},
    {"name": "Profile",             "directory": "/dashboard/profile",                      "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Upgrade",             "directory": "/dashboard/upgrade",                      "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Settings",            "directory": "/dashboard/settings",                     "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Notifications",       "directory": "/dashboard/notifications",                "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Help & Support",      "directory": "/dashboard/customer-service",             "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Analyses",            "directory": "/dashboard/analyses",                     "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Opportunities",       "directory": "/dashboard/opportunity-alerts",           "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Leaderboard",         "directory": "/dashboard/leaderboard",                  "is_in_mvp": False, "has_sub_pages": False, "is_in_next_feature_launch": True,  "sub_pages": []},
    {"name": "Community",           "directory": "/dashboard/community",                    "is_in_mvp": False, "has_sub_pages": False, "is_in_next_feature_launch": True,  "sub_pages": []},
    {"name": "Market place",        "directory": "/dashboard/market-place",                 "is_in_mvp": False, "has_sub_pages": False, "is_in_next_feature_launch": True,  "sub_pages": []},
    {"name": "Reviews",             "directory": "/dashboard/reviews",                      "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Docs",                "directory": "/dashboard/docs",                         "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
    {"name": "Builder Bonus",       "directory": "/dashboard/builder-bonus",                "is_in_mvp": False, "has_sub_pages": False, "is_in_next_feature_launch": True,  "sub_pages": []},
    {"name": "Blog",                "directory": "/dashboard/blog",                         "is_in_mvp": False, "has_sub_pages": False, "is_in_next_feature_launch": True,  "sub_pages": []},
    {"name": "Earnings",            "directory": "/dashboard/earnings",                     "is_in_mvp": True,  "has_sub_pages": False, "is_in_next_feature_launch": False, "sub_pages": []},
]


def seed_features_if_empty(db: Session) -> None:
    """Populate mvp_features table from defaults if it is empty."""
    if db.query(MvpFeature).count() == 0:
        for f in _SEED_FEATURES:
            db.add(MvpFeature(
                name=f["name"],
                directory=f["directory"],
                is_in_mvp=f["is_in_mvp"],
                has_sub_pages=f["has_sub_pages"],
                is_in_next_feature_launch=f["is_in_next_feature_launch"],
                sub_pages=f["sub_pages"],
            ))
        db.commit()
        logger.info("[MvpFeatures] seeded default feature flags")


def _serialise(features: list) -> list:
    return [
        {
            "id": f.id,
            "name": f.name,
            "directory": f.directory,
            "isInMvp": f.is_in_mvp,
            "hasSubPages": f.has_sub_pages,
            "isInNextFeatureLaunch": f.is_in_next_feature_launch,
            "subPages": f.sub_pages or [],
            "updatedAt": f.updated_at.isoformat() if f.updated_at else None,
        }
        for f in features
    ]


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class ToggleFeatureRequest(BaseModel):
    is_in_mvp: bool
    sub_page_directory: str | None = None  # if set, toggle a specific sub-page


# ── Admin endpoints ───────────────────────────────────────────────────────────
@admin_router.get("")
async def list_mvp_features(
    _=Depends(admin_required),
    db: Session = Depends(get_db),
):
    seed_features_if_empty(db)
    features = db.query(MvpFeature).order_by(MvpFeature.id).all()
    return {"status": "success", "data": _serialise(features)}


@admin_router.patch("/{feature_id}")
async def toggle_mvp_feature(
    feature_id: int,
    body: ToggleFeatureRequest,
    _=Depends(admin_required),
    db: Session = Depends(get_db),
):
    feature = db.query(MvpFeature).filter(MvpFeature.id == feature_id).first()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    if body.sub_page_directory:
        # Toggle a specific sub-page
        sub_pages = list(feature.sub_pages or [])
        found = False
        for sp in sub_pages:
            if sp.get("directory") == body.sub_page_directory:
                sp["is_in_mvp"] = body.is_in_mvp
                found = True
                break
        if not found:
            raise HTTPException(status_code=404, detail="Sub-page not found")
        feature.sub_pages = sub_pages
    else:
        feature.is_in_mvp = body.is_in_mvp

    feature.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(feature)

    all_features = db.query(MvpFeature).order_by(MvpFeature.id).all()
    payload = {"type": "mvp_features_updated", "data": _serialise(all_features)}
    _broadcast(payload)

    logger.info(
        f"[MvpFeatures] feature_id={feature_id} sub_page={body.sub_page_directory} "
        f"toggled is_in_mvp={body.is_in_mvp}"
    )
    return {"status": "success", "data": _serialise([feature])[0]}


# ── Public endpoints ──────────────────────────────────────────────────────────
@public_router.get("")
async def get_mvp_features(db: Session = Depends(get_db)):
    """Customer app polls this to get current feature flags."""
    seed_features_if_empty(db)
    features = db.query(MvpFeature).order_by(MvpFeature.id).all()
    return {"status": "success", "data": _serialise(features)}


@public_router.get("/stream")
async def mvp_features_stream(db: Session = Depends(get_db)):
    """SSE endpoint — customer app subscribes here for real-time flag updates."""
    seed_features_if_empty(db)
    features = db.query(MvpFeature).order_by(MvpFeature.id).all()
    initial = json.dumps({"type": "mvp_features_updated", "data": _serialise(features)})

    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _sse_subscribers.append(queue)

    async def event_generator():
        # Send current state immediately on connect
        yield f"data: {initial}\n\n"
        try:
            while True:
                try:
                    # 15-second timeout keeps the stream alive under Railway's
                    # HTTP/2 proxy idle-reset threshold (~60 s).
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _sse_subscribers:
                _sse_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
