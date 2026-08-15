"""
Community Feature — Channels, Discussions, Events, Leaderboard, Saved Items
"""
import logging
import re
from typing import Optional, List
from datetime import datetime, timezone, timedelta
import json
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, String

from database.pg_connections import get_db, SessionLocal
from database.pg_models import (
    User,
    CommunityChannel, ChannelMember,
    CommunityDiscussion, DiscussionReply, DiscussionLike, DiscussionBookmark, DiscussionPollVote,
    CommunityEvent, EventRegistration,
    CommunityActivity, SavedItem,
    UserSettings, BusinessAnalysis,
    UserNotification, FounderInsightCard,
)
from api.routes.auth.login import get_current_user
from api.routes.dependencies import get_current_user_optional
from api.routes.user.missions import _flatten_roadmap_tasks
from api.cache import get_cached, set_cached, delete_cached

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/community", tags=["community"])


def _generate_grok_takeaways(title: str, content: str) -> Optional[List[str]]:
    """
    Calls xAI Grok (or OpenAI API format) using XAI_API_KEY to generate 3 bullet points
    for Decision Takeaways.
    """
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        logger.warning("XAI_API_KEY not set in environment — skipping AI takeaway generation")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=30.0,
            max_retries=2,
        )
        prompt = (
            f"Analyze this founder post from the Lavoo Build Room:\n\n"
            f"Headline: {title}\n"
            f"Content: {content}\n\n"
            f"Extract EXACTLY 3 concise, highly actionable 'Decision Takeaways' for solo founders.\n"
            f"Format your response as a strict JSON object: {{\"takeaways\": [\"Takeaway 1\", \"Takeaway 2\", \"Takeaway 3\"]}}"
        )
        models_to_try = ["grok-4-1-fast-reasoning", "grok-2-latest", "grok-4-1-fast-non-reasoning"]
        completion = None
        last_err = None

        for m in models_to_try:
            try:
                completion = client.chat.completions.create(
                    model=m,
                    messages=[
                        {"role": "system", "content": "You are the Lavoo Business Decision Engine AI. Extract 3 actionable decision takeaways for solo founders in strict JSON format."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=300,
                )
                if completion and completion.choices:
                    break
            except Exception as err:
                last_err = err
                continue

        if not completion or not completion.choices:
            if last_err:
                raise last_err
            return None

        raw_text = completion.choices[0].message.content.strip()
        if "```" in raw_text:
            raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
        parsed = json.loads(raw_text)
        takeaways = parsed.get("takeaways", [])
        if isinstance(takeaways, list) and len(takeaways) > 0:
            cleaned = [re.sub(r"^[›\-*\d.\s]+", "", str(t)).strip() for t in takeaways[:3]]
            return cleaned
    except Exception as e:
        logger.error(f"Grok AI takeaway generation error: {e}")
    return None


def generate_ai_takeaways_for_discussion(discussion_id: int, db: Session) -> Optional[List[str]]:
    """
    Synchronous / On-demand helper to generate and store takeaways for a discussion.
    Used for Strategy 2 (on-demand lazy generation for previous posts).
    """
    try:
        d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
        if not d:
            return None
        if d.ai_takeaways and isinstance(d.ai_takeaways, list) and len(d.ai_takeaways) > 0:
            return d.ai_takeaways

        takeaways = _generate_grok_takeaways(d.title, d.content)
        if takeaways:
            d.ai_takeaways = takeaways
            db.add(d)
            db.commit()
            db.refresh(d)
            return takeaways
    except Exception as e:
        logger.error(f"generate_ai_takeaways_for_discussion failed for post {discussion_id}: {e}")
        db.rollback()
    return None


def _async_generate_takeaways_worker(discussion_id: int):
    """
    Background worker that runs after FastAPI HTTP response is sent.
    """
    db = SessionLocal()
    try:
        generate_ai_takeaways_for_discussion(discussion_id, db)
    finally:
        db.close()


def _log_activity(db: Session, user_id: int, action_type: str, target_id: Optional[int] = None, target_type: Optional[str] = None, target_name: Optional[str] = None):
    try:
        db.add(CommunityActivity(user_id=user_id, action_type=action_type, target_id=target_id, target_type=target_type, target_name=target_name))
    except Exception as e:
        logger.warning(f"Activity logging failed: {e}")


def _channel_dict(ch: CommunityChannel, joined_ids: Optional[set] = None) -> dict:
    is_member = ch.id in joined_ids if joined_ids is not None else False
    return {
        "id": ch.id, "name": ch.name, "slug": ch.slug, "description": ch.description,
        "category": ch.category, "member_count": ch.member_count,
        "members": ch.member_count,  # frontend alias
        "active": True,
        "isJoined": is_member, "is_member": is_member,
        "post_count": ch.post_count,
        "icon": ch.icon, "is_public": ch.is_public,
        "created_at": ch.created_at.isoformat() if ch.created_at else None,
    }


class GiftChopsRequest(BaseModel):
    amount: int


_AUTHOR_GRADIENTS = [
    "from-orange-400 to-rose-500",
    "from-amber-400 to-orange-500",
    "from-rose-400 to-pink-500",
    "from-yellow-400 to-amber-500",
]

# All valid topic slugs that can be stored as post_type
_TOPIC_SLUGS = {
    'what-worked', 'tool-recommendations', 'collaboration-offers',
    'questions', 'shared-wins', 'resources', 'reflections', 'poll',
}


def _get_poll_payload(d: CommunityDiscussion, current_user: Optional[User] = None, db: Optional[Session] = None) -> Optional[dict]:
    poll_data = getattr(d, 'poll_data', None)
    if not poll_data or not isinstance(poll_data, dict):
        return None
    raw_options = poll_data.get("options", [])
    if not raw_options or not isinstance(raw_options, list):
        return None

    votes = getattr(d, 'poll_votes', None)
    if votes is None or not isinstance(votes, list) or len(votes) == 0:
        if db:
            votes = db.query(DiscussionPollVote).filter_by(discussion_id=d.id).all()
        else:
            try:
                from database.pg_connections import SessionLocal
                with SessionLocal() as s:
                    votes = s.query(DiscussionPollVote).filter_by(discussion_id=d.id).all()
            except Exception:
                votes = []

    total_votes = len(votes)
    counts = {}
    user_voted_idx = None

    for v in votes:
        opt_idx = getattr(v, 'option_index', 0)
        counts[opt_idx] = counts.get(opt_idx, 0) + 1
        if current_user and getattr(v, 'user_id', None) == current_user.id:
            user_voted_idx = opt_idx

    formatted_options = []
    for idx, opt in enumerate(raw_options):
        text = opt if isinstance(opt, str) else (opt.get("text", "") if isinstance(opt, dict) else str(opt))
        cnt = counts.get(idx, 0)
        pct = round((cnt / total_votes * 100), 1) if total_votes > 0 else 0.0
        formatted_options.append({
            "id": idx,
            "text": text,
            "votes": cnt,
            "count": cnt,
            "percentage": pct,
        })

    # Calculate expiration status
    expires_at_raw = poll_data.get("expires_at")
    duration_val = poll_data.get("duration", "none")
    is_closed = False
    time_remaining_str = None

    if expires_at_raw:
        try:
            clean_iso = str(expires_at_raw).replace('Z', '+00:00')
            expires_dt = datetime.fromisoformat(clean_iso)
            now = datetime.now(timezone.utc)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            
            diff = expires_dt - now
            if diff.total_seconds() <= 0:
                is_closed = True
                time_remaining_str = "Final results · Poll closed"
            else:
                secs = int(diff.total_seconds())
                days = secs // 86400
                hours = (secs % 86400) // 3600
                mins = (secs % 3600) // 60
                if days > 0:
                    time_remaining_str = f"{days}d {hours}h left" if hours > 0 else f"{days}d left"
                elif hours > 0:
                    time_remaining_str = f"{hours}h {mins}m left"
                else:
                    time_remaining_str = f"{max(1, mins)}m left"
        except Exception:
            is_closed = False
            time_remaining_str = None

    return {
        "options": formatted_options,
        "total_votes": total_votes,
        "user_voted_option": user_voted_idx,
        "is_closed": is_closed,
        "expires_at": expires_at_raw,
        "duration": duration_val,
        "time_remaining": time_remaining_str,
    }


def _discussion_dict(d: CommunityDiscussion, liked_ids: Optional[set] = None, saved_ids: Optional[set] = None, include_quoted: bool = True, current_user: Optional[User] = None) -> dict:
    has_liked = d.id in liked_ids if liked_ids is not None else False
    has_saved = d.id in saved_ids if saved_ids is not None else False
    post_type_val = getattr(d, 'post_type', None) or 'discussion'

    # Return topic slug as a plain string so the frontend can filter by it directly.
    # Generic 'discussion' posts fall back to the actual backend channel object.
    if post_type_val == 'reflection':
        channel_display: any = 'reflections'
    elif post_type_val in _TOPIC_SLUGS:
        channel_display = post_type_val
    else:
        channel_display = {"id": d.channel.id, "name": d.channel.name, "slug": d.channel.slug} if d.channel else None

    author_obj = None
    if d.user:
        name = d.user.name or "Member"
        author_obj = {
            "id": d.user.id,
            "name": name,
            "initials": name[:2].upper(),
            "gradient": _AUTHOR_GRADIENTS[d.user.id % len(_AUTHOR_GRADIENTS)],
            "role": getattr(d.user, 'role', '') or '',
            "total_chops": d.user.total_chops or 0,
        }

    quoted_dict = None
    if include_quoted and getattr(d, 'quoted_discussion', None) is not None:
        try:
            quoted_dict = _discussion_dict(d.quoted_discussion, liked_ids=liked_ids, saved_ids=saved_ids, include_quoted=False, current_user=current_user)
        except Exception:
            quoted_dict = None

    spice_cnt = getattr(d, 'spice_count', 0) or 0
    sub_status = getattr(current_user, 'subscription_status', None) if current_user else None
    is_subscribed = sub_status in ('active', 'trialing')
    raw_takeaways = getattr(d, 'ai_takeaways', None)

    tagged_ids = getattr(d, 'tagged_user_ids', None) or []
    visibility_val = getattr(d, 'visibility', 'public') or 'public'

    return {
        "id": d.id, "channel_id": d.channel_id, "title": d.title, "content": d.content,
        "tags": d.tags or [], "like_count": d.like_count, "reply_count": d.reply_count,
        "likes": d.like_count, "replies": d.reply_count,
        "pinned": d.is_pinned, "is_pinned": d.is_pinned,
        "hot": d.like_count >= 10,
        "view_count": d.view_count, "has_liked": has_liked, "liked_by_user": has_liked,
        "isBookmarked": has_saved, "is_saved": has_saved, "saved_by_user": has_saved, "bookmarked": has_saved,
        "chops_gifted": d.chops_gifted or 0,
        "spice_count": spice_cnt, "spiced": spice_cnt, "spices": spice_cnt,
        "quoted_discussion_id": getattr(d, 'quoted_discussion_id', None),
        "quoted_discussion": quoted_dict,
        "takeaways": raw_takeaways if (is_subscribed and raw_takeaways) else None,
        "has_takeaways": bool(raw_takeaways),
        "type": post_type_val,
        "tagged_user_ids": tagged_ids,
        "visibility": visibility_val,
        "poll": _get_poll_payload(d, current_user=current_user),
        "author": author_obj,
        "channel": channel_display,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


EVENT_TYPE_MAP = {"live": "Live", "workshop": "Workshop", "webinar": "Webinar"}

def _event_dict(ev: CommunityEvent, registered_ids: Optional[set] = None) -> dict:
    is_registered = ev.id in registered_ids if registered_ids is not None else False
    formatted_date = ev.scheduled_at.strftime("%b %d, %Y") if ev.scheduled_at else None
    formatted_time = ev.scheduled_at.strftime("%I:%M %p") if ev.scheduled_at else None
    frontend_type = EVENT_TYPE_MAP.get((ev.event_type or "").lower(), ev.event_type or "Live")
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "type": frontend_type,
        "date": formatted_date, "time": formatted_time,
        "attendees": ev.attendee_count, "attendee_count": ev.attendee_count,
        "registered": is_registered, "is_registered": is_registered,
        "scheduled_at": ev.scheduled_at.isoformat() if ev.scheduled_at else None,
        "duration_minutes": ev.duration_minutes, "max_attendees": ev.max_attendees,
        "host_name": ev.host_name, "meeting_link": ev.meeting_link,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


# ─── Content moderation ─────────────────────────────────────────────────────

_DEROGATORY_TERMS = {
    "nigger", "nigga", "faggot", "fag", "retard", "retarded", "cunt",
    "kike", "spic", "chink", "gook", "wetback", "tranny", "dyke",
    "whore", "slut", "bitch", "bastard", "asshole", "motherfucker",
    "fuck you", "go fuck", "piece of shit", "piece of crap",
}

def _contains_derogatory_content(text: str) -> bool:
    lowered = text.lower()
    for term in _DEROGATORY_TERMS:
        pattern = r'\b' + re.escape(term) + r'\b'
        if re.search(pattern, lowered):
            return True
    return False


# ─── Pydantic Models ────────────────────────────────────────────────────────

class CreateDiscussionRequest(BaseModel):
    title: str
    content: str
    channel_id: int
    tags: Optional[List[str]] = None
    type: Optional[str] = "discussion"  # "discussion" | "reflection" | "spiced" | "poll"
    tagged_user_ids: Optional[List[int]] = None
    visibility: Optional[str] = "public"  # "public" | "tagged_only"
    poll_options: Optional[List[str]] = None
    poll_duration: Optional[str] = "none"  # "none" | "24h" | "3d" | "7d"


class VotePollRequest(BaseModel):
    option_index: int


class SpiceDiscussionRequest(BaseModel):
    content: str


class CreateReplyRequest(BaseModel):
    content: str
    parent_reply_id: Optional[int] = None
    tagged_user_ids: Optional[List[int]] = None


class LikeReplyRequest(BaseModel):
    action: str  # "like" | "unlike"


class SaveItemRequest(BaseModel):
    item_id: int
    item_type: str


# ─── Stats ──────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_community_stats(
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    try:
        total_channels = db.query(CommunityChannel).count()
        total_discussions = db.query(CommunityDiscussion).count()
        total_events = db.query(CommunityEvent).filter_by(is_published=True).count()
        total_members = db.query(User).count()
        user_channels = db.query(ChannelMember).filter_by(user_id=current_user.id).count() if current_user else 0

        return {"success": True, "data": {
            "total_channels": total_channels,
            "total_discussions": total_discussions,
            "total_events": total_events,
            "total_members": total_members,
            "my_channels": user_channels,
        }}
    except Exception as e:
        logger.error(f"Community stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch community stats")


# ─── Channels ───────────────────────────────────────────────────────────────

@router.get("/channels/my")
async def get_my_channels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        memberships = db.query(ChannelMember).filter_by(user_id=current_user.id).all()
        joined = {m.channel_id for m in memberships}
        channels = [_channel_dict(m.channel, joined) for m in memberships if m.channel]
        return {"success": True, "data": channels}
    except Exception as e:
        logger.error(f"Get my channels error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch your channels")


@router.get("/channels/{channel_name}")
async def get_channel_by_name(
    channel_name: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    ch = db.query(CommunityChannel).filter_by(slug=channel_name).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    joined = {m.channel_id for m in db.query(ChannelMember.channel_id).filter_by(user_id=current_user.id).all()} if current_user else set()
    return {"success": True, "data": _channel_dict(ch, joined)}


@router.get("/channels")
async def get_channels(
    category: Optional[str] = Query(None),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    try:
        valid_slugs = list(_TOPIC_SLUGS)
        q = db.query(CommunityChannel).filter(CommunityChannel.slug.in_(valid_slugs))
        if category and category.lower() != "all":
            q = q.filter(CommunityChannel.category == category)
        channels = q.order_by(CommunityChannel.member_count.desc()).all()
        # Batch-fetch memberships to avoid N+1
        joined = {m.channel_id for m in db.query(ChannelMember.channel_id).filter_by(user_id=current_user.id).all()} if current_user else set()
        return {"success": True, "data": [_channel_dict(ch, joined) for ch in channels]}
    except Exception as e:
        logger.error(f"Get channels error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch channels")


@router.post("/channels/{channel_id}/join")
async def join_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ch = db.query(CommunityChannel).filter_by(id=channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    existing = db.query(ChannelMember).filter_by(user_id=current_user.id, channel_id=channel_id).first()
    if existing:
        return {"success": True, "message": "Already a member"}

    db.add(ChannelMember(user_id=current_user.id, channel_id=channel_id))
    ch.member_count = (ch.member_count or 0) + 1
    db.commit()
    _log_activity(db, current_user.id, "joined_channel", channel_id, "channel", ch.name)
    return {"success": True, "message": f"Joined {ch.name}"}


@router.post("/channels/{channel_id}/leave")
async def leave_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    membership = db.query(ChannelMember).filter_by(user_id=current_user.id, channel_id=channel_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member")

    ch = db.query(CommunityChannel).filter_by(id=channel_id).first()
    db.delete(membership)
    if ch and ch.member_count > 0:
        ch.member_count -= 1
    db.commit()
    return {"success": True, "message": "Left channel"}


# ─── Discussions ─────────────────────────────────────────────────────────────

def _summarize_mission_title(text: str, max_chars: int = 80) -> str:
    """Word-bounded summary of a mission's full task text for the reflection
    post card's title slot — caps length like the old `[:80]` slice did, but
    never cuts a word in half."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    words = text.split()
    out, length = [], 0
    for w in words:
        add = len(w) + (1 if out else 0)
        if length + add > max_chars - 1:  # leave room for the ellipsis
            break
        out.append(w)
        length += add
    return (' '.join(out) + '…') if out else text[:max_chars - 1] + '…'


@router.get("/discussions")
async def get_discussions(
    channel_id: Optional[int] = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    try:
        user_id_str = str(current_user.id) if current_user else "anon"
        cache_key = f"community:discussions:user:{user_id_str}:ch:{channel_id}:lim:{limit}:off:{offset}"
        cached = await get_cached(cache_key)
        if cached is not None:
            items = cached.get("data") if isinstance(cached, dict) else (cached if isinstance(cached, list) else None)
            if items and isinstance(items, list):
                # Bypass stale cache if any reflection item is missing mission_task
                has_stale_reflection = False
                for item in items:
                    if isinstance(item, dict) and item.get("type") == "mission_reflection" and not item.get("mission_task"):
                        has_stale_reflection = True
                        break
                if not has_stale_reflection:
                    post_ids = [i.get("id") for i in items if isinstance(i, dict) and isinstance(i.get("id"), int)]
                    if post_ids:
                        if current_user:
                            user_likes = {row[0] for row in db.query(DiscussionLike.discussion_id).filter(
                                DiscussionLike.user_id == current_user.id,
                                DiscussionLike.discussion_id.in_(post_ids)
                            ).all()}
                            bm_saves = {row[0] for row in db.query(DiscussionBookmark.discussion_id).filter(
                                DiscussionBookmark.user_id == current_user.id,
                                DiscussionBookmark.discussion_id.in_(post_ids)
                            ).all()}
                            tbl_saves = {row[0] for row in db.query(SavedItem.item_id).filter(
                                SavedItem.user_id == current_user.id,
                                SavedItem.item_id.in_(post_ids)
                            ).all()}
                            user_saves = bm_saves.union(tbl_saves)
                            for item in items:
                                if isinstance(item, dict) and isinstance(item.get("id"), int):
                                    pid = item["id"]
                                    item["has_liked"] = pid in user_likes
                                    item["liked_by_user"] = pid in user_likes
                                    item["isBookmarked"] = pid in user_saves
                                    item["is_saved"] = pid in user_saves
                                    item["saved_by_user"] = pid in user_saves
                                    item["bookmarked"] = pid in user_saves

                        # Rehydrate poll state in real-time for all cached items
                        poll_pids = [
                            i["id"] for i in items
                            if isinstance(i, dict) and isinstance(i.get("id"), int) and (i.get("type") == "poll" or i.get("poll") is not None)
                        ]
                        if poll_pids:
                            poll_discussions = db.query(CommunityDiscussion).filter(CommunityDiscussion.id.in_(poll_pids)).all()
                            poll_map = {p.id: _get_poll_payload(p, current_user=current_user) for p in poll_discussions}
                            for item in items:
                                if isinstance(item, dict) and item.get("id") in poll_map:
                                    item["poll"] = poll_map[item["id"]]
                    return cached

        q = db.query(CommunityDiscussion)
        if channel_id:
            q = q.filter_by(channel_id=channel_id)

        # Enforce Tag-Gated Visibility:
        # If visibility == 'tagged_only', post is visible ONLY IF user is author, tagged, or moderator/admin
        is_mod_or_admin = bool(current_user and (current_user.is_admin or (getattr(current_user, 'role', '') in ('moderator', 'admin'))))
        if not is_mod_or_admin:
            if current_user:
                q = q.filter(
                    or_(
                        CommunityDiscussion.visibility == 'public',
                        CommunityDiscussion.visibility == None,
                        CommunityDiscussion.user_id == current_user.id,
                        func.cast(CommunityDiscussion.tagged_user_ids, String).contains(str(current_user.id))
                    )
                )
            else:
                q = q.filter(
                    or_(
                        CommunityDiscussion.visibility == 'public',
                        CommunityDiscussion.visibility == None
                    )
                )

        discussions = q.order_by(CommunityDiscussion.is_pinned.desc(), CommunityDiscussion.created_at.desc()).offset(offset).limit(limit).all()
        # Batch-fetch likes and bookmarks to avoid N+1
        discussion_ids = [d.id for d in discussions]
        liked = {row[0] for row in db.query(DiscussionLike.discussion_id).filter(
            DiscussionLike.user_id == current_user.id,
            DiscussionLike.discussion_id.in_(discussion_ids)
        ).all()} if (current_user and discussion_ids) else set()

        bm_saved = {row[0] for row in db.query(DiscussionBookmark.discussion_id).filter(
            DiscussionBookmark.user_id == current_user.id,
            DiscussionBookmark.discussion_id.in_(discussion_ids)
        ).all()} if (current_user and discussion_ids) else set()

        tbl_saved = {row[0] for row in db.query(SavedItem.item_id).filter(
            SavedItem.user_id == current_user.id,
            SavedItem.item_id.in_(discussion_ids)
        ).all()} if (current_user and discussion_ids) else set()

        saved = bm_saved.union(tbl_saved)

        result = [_discussion_dict(d, liked_ids=liked, saved_ids=saved, current_user=current_user) for d in discussions]

        # Include mission roadmap comments from users who opted in.
        # Comments live in analysis.user_progress['roadmap_comments'] (a dict of task_id → [comment, ...]).
        try:
            opted_in_user_ids = db.query(UserSettings.user_id).filter(
                UserSettings.show_mission_comments_in_community == True
            ).all()
            opted_in_ids = [row[0] for row in opted_in_user_ids]
            if opted_in_ids:
                analyses = (
                    db.query(BusinessAnalysis, User)
                    .join(User, BusinessAnalysis.user_id == User.id)
                    .filter(BusinessAnalysis.user_id.in_(opted_in_ids))
                    .all()
                )
                any_summaries_changed = False
                for analysis, author in analyses:
                    up = analysis.user_progress or {}
                    roadmap_comments = up.get('roadmap_comments', {}) if isinstance(up, dict) else {}
                    if not isinstance(roadmap_comments, dict):
                        continue
                    # Map each task's frontend_id → its mission text, so the
                    # reflection post title is the mission it was submitted
                    # under, not the reflection body itself.
                    mission_titles = {
                        t['frontend_id']: t['text'] for t in _flatten_roadmap_tasks(analysis)
                    }
                    # Full mission text is often too long for the post card's
                    # title (wraps/overflows). Word-capped summaries are
                    # computed once per task and persisted in their own column
                    # (business_analyses.roadmap_task_summaries) so they're
                    # reused on every later fetch instead of being
                    # re-summarized — and re-truncated — on every request.
                    task_summaries = analysis.roadmap_task_summaries
                    if not isinstance(task_summaries, dict):
                        task_summaries = {}
                    analysis_summaries_changed = False
                    for task_id, comments in roadmap_comments.items():
                        if not isinstance(comments, list):
                            continue
                        for comment in comments:
                            text = comment.get('text', '').strip()
                            # Filter out empty and trivial single-word test placeholders
                            if not text or len(text) < 5 or text.lower().strip('.!') in ('test', 'testing', 'tes', 'tesst', 'text', 'test2', 'tessssst', 'reesss', 'done', 'testtt'):
                                continue
                            created_at = comment.get('createdAt')
                            comment_id = comment.get('id', f"rc_{analysis.id}_{task_id}")
                            full_title = mission_titles.get(task_id) or ""
                            result.append({
                                "id": f"rc_{comment_id}",
                                "type": "mission_reflection",
                                "title": full_title if full_title else text[:80],
                                "mission_task": full_title if full_title else text[:80],
                                "content": text,
                                "excerpt": text[:160],
                                "tags": [],
                                "like_count": 0, "reply_count": 0,
                                "likes": 0, "replies": 0,
                                "pinned": False, "is_pinned": False,
                                "hot": False,
                                "view_count": 0,
                                "has_liked": False, "liked_by_user": False,
                                "chops_gifted": 0,
                                "author": {
                                    "id": author.id,
                                    "name": author.name or "Member",
                                    "initials": (author.name or "M")[:2].upper(),
                                    "gradient": "from-orange-400 to-rose-400",
                                    "role": "",
                                    "total_chops": author.total_chops or 0,
                                },
                                "channel": "reflections",
                                "created_at": created_at,
                                "timeAgo": created_at,
                                "updated_at": None,
                            })
                    if analysis_summaries_changed:
                        analysis.roadmap_task_summaries = task_summaries
                if any_summaries_changed:
                    db.commit()
        except Exception as reflection_err:
            logger.warning(f"Mission reflections fetch error: {reflection_err}")

        # Interleave Founder Insights & Reflections into the discussion stream (4 Standard : 1 Insight : 4 Standard : 1 Reflection)
        try:
            insights_raw = db.query(FounderInsightCard).filter(FounderInsightCard.is_active == True).order_by(FounderInsightCard.created_at.desc()).all()
            insight_cards = [{
                "id": f"insight_{card.id}",
                "type": "founder_insight",
                "isStat": True,
                "big": card.highlight_stat or "",
                "highlight_stat": card.highlight_stat or "",
                "headline": card.insight_text,
                "insight_text": card.insight_text,
                "source": card.source,
                "accent": card.accent_color or "#e87a02",
                "accent_color": card.accent_color or "#e87a02",
                "created_at": card.created_at.isoformat() if card.created_at else None
            } for card in insights_raw]

            std_posts = [p for p in result if p.get("type") != "mission_reflection"]
            ref_posts = sorted(
                [p for p in result if p.get("type") == "mission_reflection"],
                key=lambda x: str(x.get("created_at") or ""),
                reverse=True
            )

            if (insight_cards or ref_posts) and std_posts:
                interleaved = []
                std_i, ins_i, ref_i = 0, 0, 0
                cycle = 0

                while std_i < len(std_posts) or ins_i < len(insight_cards) or ref_i < len(ref_posts):
                    # 1. Add up to 4 standard posts
                    for _ in range(4):
                        if std_i < len(std_posts):
                            interleaved.append(std_posts[std_i])
                            std_i += 1

                    # 2. Alternating cadence: Even cycle = Insight, Odd cycle = Reflection
                    if cycle % 2 == 0:
                        if ins_i < len(insight_cards):
                            interleaved.append(insight_cards[ins_i % len(insight_cards)])
                            ins_i += 1
                        elif ref_i < len(ref_posts):
                            interleaved.append(ref_posts[ref_i])
                            ref_i += 1
                    else:
                        if ref_i < len(ref_posts):
                            interleaved.append(ref_posts[ref_i])
                            ref_i += 1
                        elif ins_i < len(insight_cards):
                            interleaved.append(insight_cards[ins_i % len(insight_cards)])
                            ins_i += 1

                    cycle += 1
                    if std_i >= len(std_posts) and ins_i >= len(insight_cards) and ref_i >= len(ref_posts):
                        break

                result = interleaved
        except Exception as interleave_err:
            logger.warning(f"Feed interleaving error: {interleave_err}")

        response = {"success": True, "data": result}
        await set_cached(cache_key, response, ttl_seconds=15)
        return response
    except Exception as e:
        logger.error(f"Get discussions error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch discussions")

@router.post("/cron/process-reflections")
async def cron_process_pending_reflections(db: Session = Depends(get_db)):
    """
    Background Cron Service: Runs every 5 minutes to process completed mission reflections,
    respecting the user's 'show_mission_comments_in_community' preference setting.
    """
    try:
        opted_in_user_ids = {
            row[0] for row in db.query(UserSettings.user_id).filter(
                UserSettings.show_mission_comments_in_community == True
            ).all()
        }

        completed_analyses = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.status == "completed"
        ).order_by(BusinessAnalysis.updated_at.desc()).limit(20).all()

        published_count = 0
        for analysis in completed_analyses:
            if analysis.user_id not in opted_in_user_ids:
                continue

            up = analysis.user_progress or {}
            roadmap_comments = up.get("roadmap_comments", {}) if isinstance(up, dict) else {}
            if not isinstance(roadmap_comments, dict) or not roadmap_comments:
                continue

            for task_id, comments in roadmap_comments.items():
                if not isinstance(comments, list):
                    continue
                for comment in comments:
                    text = comment.get("text", "").strip()
                    if not text:
                        continue
                    user = db.query(User).filter_by(id=analysis.user_id).first()
                    if user:
                        user.total_chops = (user.total_chops or 0) + 50
                        published_count += 1

        db.commit()
        return {"success": True, "published_count": published_count}
    except Exception as e:
        logger.error(f"Cron process reflections error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/discussions/{discussion_id}")
async def get_discussion(
    discussion_id: int,
    background_tasks: BackgroundTasks,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")
    d.view_count = (d.view_count or 0) + 1
    db.commit()

    # Strategy 2: On-Demand "Lazy" Generation in background if ai_takeaways is NULL
    if d.ai_takeaways is None:
        background_tasks.add_task(_async_generate_takeaways_worker, d.id)

    def _serialise_reply(r) -> dict:
        return {
            "id": r.id,
            "content": r.content,
            "like_count": r.like_count,
            "parent_reply_id": r.parent_reply_id,
            "author": {
                "id": r.user.id,
                "name": r.user.name,
                "total_chops": r.user.total_chops or 0,
            } if r.user else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "sub_replies": [_serialise_reply(sr) for sr in (r.sub_replies or [])],
        }

    # Only return top-level replies; sub_replies are nested inside each
    top_level = [r for r in d.replies if r.parent_reply_id is None]
    replies = [_serialise_reply(r) for r in top_level]

    liked = {d.id} if (current_user and db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=d.id).first()) else set()
    saved = {d.id} if (current_user and db.query(SavedItem).filter_by(user_id=current_user.id, item_id=d.id).first()) else set()
    data = _discussion_dict(d, liked_ids=liked, saved_ids=saved, current_user=current_user)
    data["replies"] = replies
    return {"success": True, "data": data}


@router.get("/discussions/{discussion_id}/takeaways")
async def get_discussion_takeaways(
    discussion_id: int,
    background_tasks: BackgroundTasks,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    is_subscribed = bool(current_user and getattr(current_user, 'subscription_status', None) in ("active", "trialing"))
    if not is_subscribed:
        return {"status": "locked", "has_takeaways": True, "takeaways": None}

    if d.ai_takeaways and isinstance(d.ai_takeaways, list) and len(d.ai_takeaways) > 0:
        return {"status": "ready", "takeaways": d.ai_takeaways}

    # Strategy 2: On-Demand Lazy Generation (schedule in background and return status)
    background_tasks.add_task(_async_generate_takeaways_worker, d.id)
    return {"status": "generating", "takeaways": None}


@router.post("/discussions")
async def create_discussion(
    body: CreateDiscussionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if _contains_derogatory_content(body.title) or _contains_derogatory_content(body.content):
        raise HTTPException(
            status_code=422,
            detail="Your post contains language that isn't allowed in the Lavoo Build Room. Please review your content and try again."
        )

    ch = db.query(CommunityChannel).filter_by(id=body.channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    _all_valid_types = {'discussion', 'reflection'} | _TOPIC_SLUGS
    raw_type = (body.type or 'discussion').strip().lower()
    post_type = raw_type if raw_type in _all_valid_types else 'discussion'

    tagged_ids = body.tagged_user_ids or []
    if not tagged_ids:
        found_usernames = re.findall(r'@([a-zA-Z0-9_]+)', f"{body.title} {body.content}")
        if found_usernames:
            matching = db.query(User.id).filter(func.lower(User.username).in_([u.lower() for u in found_usernames])).all()
            tagged_ids = [m[0] for m in matching]

    visibility_val = "tagged_only" if body.visibility == "tagged_only" else "public"

    poll_payload = None
    clean_options = [o.strip() for o in (body.poll_options or []) if isinstance(o, str) and o.strip()]
    if post_type == 'poll' or len(clean_options) >= 2:
        if len(clean_options) >= 2:
            post_type = 'poll'
            duration_str = (body.poll_duration or "none").strip().lower()
            expires_at_iso = None
            
            now = datetime.now(timezone.utc)
            if duration_str == "24h" or duration_str == "1d":
                expires_at_iso = (now + timedelta(days=1)).isoformat()
            elif duration_str == "3d":
                expires_at_iso = (now + timedelta(days=3)).isoformat()
            elif duration_str == "7d":
                expires_at_iso = (now + timedelta(days=7)).isoformat()
            else:
                duration_str = "none"

            poll_payload = {
                "options": clean_options,
                "duration": duration_str,
                "expires_at": expires_at_iso
            }

    d = CommunityDiscussion(
        channel_id=body.channel_id, user_id=current_user.id,
        title=body.title.strip(), content=body.content.strip(),
        tags=body.tags or [], post_type=post_type,
        tagged_user_ids=tagged_ids, visibility=visibility_val,
        poll_data=poll_payload
    )
    db.add(d)
    ch.post_count = (ch.post_count or 0) + 1
    current_user.total_chops = (current_user.total_chops or 0) + 10
    db.commit()
    db.refresh(d)
    await delete_cached("community:discussions:*")
    
    _log_activity(db, current_user.id, "posted", d.id, "discussion", d.title)

    # Trigger notifications for tagged users
    if tagged_ids:
        for tagged_id in set(tagged_ids):
            if tagged_id != current_user.id:
                try:
                    n = UserNotification(
                        user_id=tagged_id,
                        type="user_tagged",
                        title="You were tagged in a Build Room post!",
                        message=f"{current_user.name} tagged you in: \"{d.title[:60]}\"",
                        link=f"/dashboard/community?discussionId={d.id}"
                    )
                    db.add(n)
                except Exception:
                    pass
        db.commit()

    # Automatically schedule background AI generation for new posts upon creation
    background_tasks.add_task(_async_generate_takeaways_worker, d.id)

    return {"success": True, "data": _discussion_dict(d, set(), current_user=current_user)}


@router.post("/discussions/{discussion_id}/poll/vote")
async def vote_poll(
    discussion_id: int,
    body: VotePollRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if not d.poll_data or not isinstance(d.poll_data, dict):
        raise HTTPException(status_code=400, detail="This discussion is not a poll")

    raw_options = d.poll_data.get("options", [])
    if body.option_index < 0 or body.option_index >= len(raw_options):
        raise HTTPException(status_code=422, detail="Invalid poll option index")

    poll_payload = _get_poll_payload(d, current_user=current_user, db=db)
    if poll_payload and poll_payload.get("is_closed"):
        raise HTTPException(status_code=400, detail="This poll has closed and is no longer accepting votes.")

    # Strictly enforce ONE VOTE per user: if user already voted, return status already_voted with canonical poll
    existing_vote = db.query(DiscussionPollVote).filter_by(
        discussion_id=discussion_id, user_id=current_user.id
    ).first()
    if existing_vote:
        return {
            "status": "already_voted",
            "message": "You have already voted in this poll.",
            "poll": _get_poll_payload(d, current_user=current_user, db=db)
        }

    vote = DiscussionPollVote(
        discussion_id=discussion_id,
        user_id=current_user.id,
        option_index=body.option_index
    )
    db.add(vote)
    db.commit()
    db.expire_all()
    await delete_cached("community:discussions:*")

    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    return {
        "status": "success",
        "poll": _get_poll_payload(d, current_user=current_user, db=db)
    }


@router.delete("/discussions/{discussion_id}")
async def delete_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Post not found")
    if d.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own posts")
    ch = db.query(CommunityChannel).filter_by(id=d.channel_id).first()
    if ch and ch.post_count > 0:
        ch.post_count -= 1
    db.delete(d)
    db.commit()
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:{d.channel_id}:lim:20:off:0")
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:None:lim:20:off:0")
    return {"success": True}


@router.post("/discussions/{discussion_id}/pin")
async def pin_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Post not found")
    # Anyone can pin/unpin any post
    d.is_pinned = not d.is_pinned
    db.commit()
    return {"success": True, "pinned": d.is_pinned}


@router.post("/discussions/{discussion_id}/like")
async def like_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    existing = db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=discussion_id).first()
    if existing:
        # Unlike
        db.delete(existing)
        d.like_count = max(0, (d.like_count or 0) - 1)
        db.commit()
        return {"success": True, "liked": False, "like_count": d.like_count}

    db.add(DiscussionLike(user_id=current_user.id, discussion_id=discussion_id))
    d.like_count = (d.like_count or 0) + 1
    db.commit()

    # Notify post owner (skip when liking own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            actor_name = current_user.name or 'Someone'
            notif = UserNotification(
                user_id=d.user_id,
                type="community_cooked",
                title=f"{actor_name} thinks you cooked.",
                message=f"{actor_name} thinks you cooked with your post '{d.title[:60]}'",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Cooked notification creation failed: {notif_err}")

    _log_activity(db, current_user.id, "cooked", discussion_id, "discussion", d.title)
    return {"success": True, "liked": True, "like_count": d.like_count}


@router.post("/discussions/{discussion_id}/gift_chops")
async def gift_chops_to_discussion(
    discussion_id: int,
    body: GiftChopsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if (current_user.total_chops or 0) < body.amount:
        raise HTTPException(status_code=400, detail="Insufficient chops")

    current_user.total_chops = (current_user.total_chops or 0) - body.amount

    if d.user_id and d.user_id != current_user.id:
        author = db.query(User).filter_by(id=d.user_id).first()
        if author:
            author.total_chops = (author.total_chops or 0) + body.amount

    d.chops_gifted = (d.chops_gifted or 0) + body.amount
    db.commit()

    # Notify post owner (skip when gifting self)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_chops",
                title=f"{current_user.name or 'Someone'} gifted you {body.amount} Chops!",
                message=f"You received {body.amount} Chops for your post '{d.title[:60]}'",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Gift chops notification creation failed: {notif_err}")

    _log_activity(db, current_user.id, "gifted_chops", discussion_id, "discussion", d.title)
    return {"success": True, "chops_gifted": d.chops_gifted, "remaining_chops": current_user.total_chops}


@router.post("/discussions/{discussion_id}/spice")
async def spice_discussion(
    discussion_id: int,
    body: SpiceDiscussionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    content_text = (body.content or "").strip()
    if not content_text:
        raise HTTPException(status_code=400, detail="Spice insight content cannot be empty")

    if _contains_derogatory_content(content_text):
        raise HTTPException(
            status_code=422,
            detail="Your spice contains language that isn't allowed in the Lavoo Build Room. Please review your content and try again."
        )

    # Increment spice count on original discussion
    d.spice_count = (d.spice_count or 0) + 1

    # Create new discussion referencing original post as a quoted post
    spiced_post = CommunityDiscussion(
        channel_id=d.channel_id,
        user_id=current_user.id,
        title=d.title[:80],
        content=content_text,
        tags=d.tags or [],
        post_type="spiced",
        quoted_discussion_id=d.id,
    )
    db.add(spiced_post)

    # Award user chops for spicing / contributing insight
    current_user.total_chops = (current_user.total_chops or 0) + 10
    db.commit()
    db.refresh(spiced_post)

    # Notify original post owner (unless spicing own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_spice",
                title=f"{current_user.name or 'Someone'} spiced your post",
                message=content_text[:120],
                link=f"/dashboard/community/post/{d.id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Spice notification creation failed: {notif_err}")

    _log_activity(db, current_user.id, "spiced", d.id, "discussion", d.title)
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:{d.channel_id}:lim:20:off:0")
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:None:lim:20:off:0")

    liked = {spiced_post.id} if db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=spiced_post.id).first() else set()
    return {
        "success": True,
        "data": _discussion_dict(spiced_post, liked),
        "original_spice_count": d.spice_count,
    }


@router.post("/discussions/{discussion_id}/replies")
async def reply_to_discussion(
    discussion_id: int,
    body: CreateReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    # Validate parent_reply_id belongs to the same discussion
    parent_reply_id = body.parent_reply_id
    if parent_reply_id is not None:
        parent = db.query(DiscussionReply).filter_by(id=parent_reply_id, discussion_id=discussion_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent reply not found")

    tagged_ids = body.tagged_user_ids or []
    if not tagged_ids:
        found_usernames = re.findall(r'@([a-zA-Z0-9_]+)', body.content)
        if found_usernames:
            matching = db.query(User.id).filter(func.lower(User.username).in_([u.lower() for u in found_usernames])).all()
            tagged_ids = [m[0] for m in matching]

    reply = DiscussionReply(
        discussion_id=discussion_id,
        user_id=current_user.id,
        content=body.content.strip(),
        parent_reply_id=parent_reply_id,
        tagged_user_ids=tagged_ids
    )
    db.add(reply)
    # Count all replies (including nested) so comment badge is accurate
    d.reply_count = (d.reply_count or 0) + 1
    current_user.total_chops = (current_user.total_chops or 0) + 5
    db.commit()
    db.refresh(reply)

    # Notify post owner (skip when replying to own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_reply",
                title=f"{current_user.name or 'Someone'} replied to your post",
                message=body.content.strip()[:120],
                link=f"/dashboard/community?discussionId={discussion_id}",
                is_read=False,
            )
            db.add(notif)

        # Notify tagged users in reply
        if tagged_ids:
            for tagged_id in set(tagged_ids):
                if tagged_id != current_user.id and tagged_id != d.user_id:
                    n = UserNotification(
                        user_id=tagged_id,
                        type="user_tagged",
                        title="You were tagged in a reply!",
                        message=f"{current_user.name} tagged you in a reply on: \"{d.title[:60]}\"",
                        link=f"/dashboard/community?discussionId={discussion_id}",
                        is_read=False
                    )
                    db.add(n)

        db.commit()
    except Exception as notif_err:
        logger.warning(f"Notification creation failed: {notif_err}")

    _log_activity(db, current_user.id, "replied", discussion_id, "discussion", d.title)
    return {"success": True, "data": {
        "id": reply.id, "content": reply.content, "like_count": 0,
        "parent_reply_id": reply.parent_reply_id,
        "author": {
            "id": current_user.id,
            "name": current_user.name,
            "total_chops": current_user.total_chops or 0,
        },
        "created_at": reply.created_at.isoformat() if reply.created_at else None,
        "sub_replies": [],
    }}


@router.post("/discussions/{discussion_id}/replies/{reply_id}/like")
async def like_reply(
    discussion_id: int,
    reply_id: int,
    body: LikeReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reply = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")
    if body.action == "like":
        reply.like_count = (reply.like_count or 0) + 1
    else:
        reply.like_count = max(0, (reply.like_count or 0) - 1)
    db.commit()
    return {"success": True, "like_count": reply.like_count}


@router.post("/discussions/{discussion_id}/replies/{reply_id}/gift_chops")
async def gift_chops_to_reply(
    discussion_id: int,
    reply_id: int,
    body: GiftChopsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reply = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")
    if reply.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot gift chops to yourself")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if (current_user.total_chops or 0) < body.amount:
        raise HTTPException(status_code=400, detail="Insufficient chops")
    current_user.total_chops = (current_user.total_chops or 0) - body.amount
    author = db.query(User).filter_by(id=reply.user_id).first()
    if author:
        author.total_chops = (author.total_chops or 0) + body.amount
    db.commit()

    # Notify reply owner (skip when gifting self)
    try:
        if reply.user_id and reply.user_id != current_user.id:
            notif = UserNotification(
                user_id=reply.user_id,
                type="community_chops",
                title=f"{current_user.name or 'Someone'} gifted you {body.amount} Chops!",
                message=f"You received {body.amount} Chops for your reply",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Reply gift chops notification creation failed: {notif_err}")

    return {"success": True, "remaining_chops": current_user.total_chops}


@router.delete("/discussions/{discussion_id}/replies/{reply_id}")
async def delete_reply(
    discussion_id: int,
    reply_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    r = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reply not found")
    if r.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own replies")
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if d and (d.reply_count or 0) > 0:
        d.reply_count = d.reply_count - 1
    db.delete(r)
    db.commit()
    return {"success": True}


# ─── Events ──────────────────────────────────────────────────────────────────

@router.get("/events/my")
async def get_my_events(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    regs = db.query(EventRegistration).filter_by(user_id=current_user.id).all()
    registered = {r.event_id for r in regs}
    events = [_event_dict(r.event, registered) for r in regs if r.event]
    return {"success": True, "data": events}


@router.get("/events")
async def get_events(
    type: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        q = db.query(CommunityEvent).filter_by(is_published=True)
        if type and type.lower() != "all":
            q = q.filter(CommunityEvent.event_type == type)
        events = q.order_by(CommunityEvent.scheduled_at.asc()).all()
        # Batch-fetch registrations to avoid N+1
        event_ids = [ev.id for ev in events]
        registered = {r.event_id for r in db.query(EventRegistration.event_id).filter(
            EventRegistration.user_id == current_user.id,
            EventRegistration.event_id.in_(event_ids)
        ).all()} if event_ids else set()
        return {"success": True, "data": [_event_dict(ev, registered) for ev in events]}
    except Exception as e:
        logger.error(f"Get events error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch events")


@router.post("/events/{event_id}/register")
async def register_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ev = db.query(CommunityEvent).filter_by(id=event_id, is_published=True).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    if ev.max_attendees and ev.attendee_count >= ev.max_attendees:
        raise HTTPException(status_code=400, detail="Event is fully booked")

    existing = db.query(EventRegistration).filter_by(user_id=current_user.id, event_id=event_id).first()
    if existing:
        return {"success": True, "message": "Already registered"}

    db.add(EventRegistration(user_id=current_user.id, event_id=event_id))
    ev.attendee_count = (ev.attendee_count or 0) + 1
    db.commit()
    _log_activity(db, current_user.id, "registered_event", event_id, "event", ev.title)
    return {"success": True, "message": f"Registered for {ev.title}"}


@router.post("/events/{event_id}/unregister")
async def unregister_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reg = db.query(EventRegistration).filter_by(user_id=current_user.id, event_id=event_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Not registered")

    ev = db.query(CommunityEvent).filter_by(id=event_id).first()
    db.delete(reg)
    if ev and ev.attendee_count > 0:
        ev.attendee_count -= 1
    db.commit()
    return {"success": True, "message": "Unregistered from event"}


# ─── User Activity, Profile, Leaderboard ────────────────────────────────────

@router.get("/activity")
async def get_my_activity(
    limit: int = Query(12, le=100),
    type_filter: Optional[str] = Query(None, alias="type"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    tf = type_filter.lower().strip() if type_filter else "all"
    all_events = []

    def clean_t(txt: Optional[str]) -> str:
        if not txt:
            return "a post"
        cleaned = re.sub(r"['\"]+", "", txt).strip()
        return cleaned if cleaned else "a post"

    # 1. Fetch User's Discussions (Posts & Reflections)
    if tf in ("all", "post", "discussions", "reflection", "reflections"):
        discs = db.query(CommunityDiscussion).filter_by(user_id=current_user.id).all()
        for d in discs:
            post_type_val = getattr(d, 'post_type', None) or 'discussion'
            title_text = clean_t(d.title or d.content[:80])
            
            if post_type_val == 'reflection':
                if tf not in ("all", "reflection", "reflections"):
                    continue
                action_title = f"Shared a reflection: {title_text}"
                type_str = "reflection"
                points = 50
            else:
                if tf not in ("all", "post", "discussions"):
                    continue
                action_title = f"Started a discussion: {title_text}"
                type_str = "post"
                points = 15

            all_events.append({
                "id": f"disc_{d.id}",
                "title": action_title,
                "type": type_str,
                "points": points,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "dt": d.created_at or datetime.min,
                "target_id": d.id,
                "target_type": "discussion",
                "target_name": title_text,
            })

    # 2. Fetch User's Replies
    if tf in ("all", "reply", "replies"):
        replies = db.query(DiscussionReply, CommunityDiscussion)\
            .join(CommunityDiscussion, DiscussionReply.discussion_id == CommunityDiscussion.id)\
            .filter(DiscussionReply.user_id == current_user.id).all()
        
        for reply, d in replies:
            title_text = clean_t(d.title)
            all_events.append({
                "id": f"reply_{reply.id}",
                "title": f"Replied to a post: {title_text}",
                "type": "reply",
                "points": 5,
                "created_at": reply.created_at.isoformat() if reply.created_at else None,
                "dt": reply.created_at or datetime.min,
                "target_id": d.id,
                "target_type": "discussion",
                "target_name": title_text,
            })

    # 3. Fetch User's Cooked Posts (Likes)
    if tf in ("all", "like", "cooked", "cook"):
        likes = db.query(DiscussionLike, CommunityDiscussion)\
            .join(CommunityDiscussion, DiscussionLike.discussion_id == CommunityDiscussion.id)\
            .filter(DiscussionLike.user_id == current_user.id).all()

        for like, d in likes:
            title_text = clean_t(d.title)
            all_events.append({
                "id": f"like_{like.id}",
                "title": f"You cooked a post: {title_text}",
                "type": "like",
                "points": 2,
                "created_at": like.created_at.isoformat() if like.created_at else None,
                "dt": like.created_at or datetime.min,
                "target_id": d.id,
                "target_type": "discussion",
                "target_name": title_text,
            })

    # 4. Fetch User's Bookmarks (DiscussionBookmark & SavedItem)
    if tf in ("all", "bookmark", "bookmarks", "save", "saved"):
        direct_bms = db.query(DiscussionBookmark, CommunityDiscussion)\
            .join(CommunityDiscussion, DiscussionBookmark.discussion_id == CommunityDiscussion.id)\
            .filter(DiscussionBookmark.user_id == current_user.id).all()

        for bm, d in direct_bms:
            title_text = clean_t(d.title)
            all_events.append({
                "id": f"bm_{bm.id}",
                "title": f"Saved a post: {title_text}",
                "type": "bookmark",
                "points": 3,
                "created_at": bm.created_at.isoformat() if bm.created_at else None,
                "dt": bm.created_at or datetime.min,
                "target_id": d.id,
                "target_type": "discussion",
                "target_name": title_text,
            })

        saved_items = db.query(SavedItem).filter_by(user_id=current_user.id).all()
        for item in saved_items:
            title_text = f"Saved item #{item.item_id}"
            target_id = item.item_id
            if item.item_type in ("discussion", "post"):
                d = db.query(CommunityDiscussion).filter_by(id=item.item_id).first()
                if d and d.title:
                    title_text = clean_t(d.title)
                    target_id = d.id

            all_events.append({
                "id": f"saved_{item.id}",
                "title": f"Saved a post: {title_text}",
                "type": "bookmark",
                "points": 3,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "dt": item.created_at or datetime.min,
                "target_id": target_id,
                "target_type": item.item_type,
                "target_name": title_text,
            })

    # 5. Fetch CommunityActivities for additional events (e.g. spiced)
    if tf in ("all", "post", "discussions"):
        activities = db.query(CommunityActivity).filter(
            CommunityActivity.user_id == current_user.id,
            CommunityActivity.action_type.in_(["spiced", "spice", "gifted_chops", "chops"])
        ).all()

        for a in activities:
            title_text = clean_t(a.target_name)
            act_type = (a.action_type or "post").lower()

            if act_type in ("spiced", "spice"):
                act_title = f"You spiced a post: {title_text}"
                pts = 10
            else:
                act_title = f"Gifted Chops to a post: {title_text}"
                pts = 5

            all_events.append({
                "id": f"act_{a.id}",
                "title": act_title,
                "type": "post",
                "points": pts,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "dt": a.created_at or datetime.min,
                "target_id": a.target_id,
                "target_type": a.target_type or "discussion",
                "target_name": title_text,
            })

    seen = set()
    unique_events = []
    for ev in all_events:
        key = (ev["type"], ev["target_id"], ev["title"])
        if key not in seen:
            seen.add(key)
            unique_events.append(ev)

    unique_events.sort(key=lambda x: x["dt"], reverse=True)

    for ev in unique_events:
        ev.pop("dt", None)

    return {"success": True, "data": unique_events[:limit]}


@router.get("/profile")
async def get_my_community_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    channels_joined = db.query(ChannelMember).filter_by(user_id=current_user.id).count()
    posts_count = db.query(CommunityDiscussion).filter_by(user_id=current_user.id).count()
    events_count = db.query(EventRegistration).filter_by(user_id=current_user.id).count()
    replies_count = db.query(DiscussionReply).filter_by(user_id=current_user.id).count()

    higher_users = db.query(User).filter(User.total_chops > (current_user.total_chops or 0)).count()
    rank = higher_users + 1

    badges = []
    if posts_count > 0:
        badges.append({"name": "First Post", "icon": "MessageSquare", "color": "bg-blue-100 text-blue-600"})
    if replies_count >= 1:
        badges.append({"name": "Contributor", "icon": "ThumbsUp", "color": "bg-green-100 text-green-600"})
    if (current_user.total_chops or 0) >= 20:
        badges.append({"name": "Active Builder", "icon": "Calendar", "color": "bg-orange-100 text-orange-600"})
    if (current_user.total_chops or 0) >= 100:
        badges.append({"name": "Top Builder", "icon": "Award", "color": "bg-purple-100 text-purple-600"})

    return {"success": True, "data": {
        "user_id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "total_chops": current_user.total_chops or 0,
        "totalPoints": current_user.total_chops or 0,
        "rank": rank,
        "streak": 7,
        "channels_joined": channels_joined,
        "posts": posts_count,
        "replies": replies_count,
        "events_registered": events_count,
        "badges": badges,
    }}


@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = Query(10, le=500),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    top_users = db.query(User).filter(User.is_active == True)\
        .order_by(User.total_chops.desc(), User.created_at.asc()).limit(limit).all()

    return {"success": True, "data": [{
        "rank": idx + 1,
        "user_id": u.id,
        "name": u.name,
        "total_chops": u.total_chops or 0,
        "referral_count": u.referral_count or 0,
        "joined_at": u.created_at.isoformat() if u.created_at else None,
    } for idx, u in enumerate(top_users)]}


# ─── Saved Items ─────────────────────────────────────────────────────────────

@router.get("/saved")
async def get_saved_items(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    bms = db.query(DiscussionBookmark, CommunityDiscussion)\
        .join(CommunityDiscussion, DiscussionBookmark.discussion_id == CommunityDiscussion.id)\
        .filter(DiscussionBookmark.user_id == current_user.id)\
        .order_by(DiscussionBookmark.created_at.desc()).all()

    formatted = []
    seen_ids = set()

    for bm, d in bms:
        seen_ids.add(d.id)
        clean_t = re.sub(r"['\"]+", "", d.title).strip() if d.title else "a post"
        ch_name = d.channel.name or d.channel.slug if d.channel else "community"
        formatted.append({
            "id": f"bm_{bm.id}",
            "item_id": d.id,
            "item_type": "discussion",
            "title": clean_t,
            "channel": ch_name,
            "created_at": bm.created_at.isoformat() if bm.created_at else None,
        })

    items = db.query(SavedItem).filter_by(user_id=current_user.id)\
        .order_by(SavedItem.created_at.desc()).all()
    
    for item in items:
        if item.item_id in seen_ids:
            continue
        title = f"Saved item #{item.item_id}"
        channel = "community"
        if item.item_type in ("discussion", "post"):
            d = db.query(CommunityDiscussion).filter_by(id=item.item_id).first()
            if d and d.title:
                clean_t = re.sub(r"['\"]+", "", d.title).strip()
                title = clean_t if clean_t else "a post"
                if d.channel:
                    channel = d.channel.name or d.channel.slug
        formatted.append({
            "id": item.id,
            "item_id": item.item_id,
            "item_type": item.item_type,
            "title": title,
            "channel": channel,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        })

    return {"success": True, "data": formatted}


@router.post("/saved")
async def save_item(
    body: SaveItemRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    is_subscribed = bool(current_user and getattr(current_user, 'subscription_status', None) in ("active", "trialing"))
    if not is_subscribed:
        raise HTTPException(
            status_code=403,
            detail="Bookmarking posts is a premium feature. Upgrade to Lavoo Pro to save posts to your personal library."
        )

    existing = db.query(SavedItem).filter_by(user_id=current_user.id, item_id=body.item_id, item_type=body.item_type).first()
    if existing:
        return {"success": True, "message": "Already saved", "id": existing.id}

    saved = SavedItem(user_id=current_user.id, item_id=body.item_id, item_type=body.item_type)
    db.add(saved)
    
    bm_existing = db.query(DiscussionBookmark).filter_by(user_id=current_user.id, discussion_id=body.item_id).first()
    if not bm_existing and body.item_type in ("discussion", "post"):
        db.add(DiscussionBookmark(user_id=current_user.id, discussion_id=body.item_id))

    db.commit()
    db.refresh(saved)

    target_title = "a post"
    if body.item_type in ("discussion", "post"):
        d = db.query(CommunityDiscussion).filter_by(id=body.item_id).first()
        if d and d.title:
            target_title = d.title

    _log_activity(db, current_user.id, "saved", body.item_id, body.item_type, target_title)
    await delete_cached(f"community:discussions:user:{current_user.id}:*")
    return {"success": True, "data": {"id": saved.id, "item_id": saved.item_id, "item_type": saved.item_type}}


@router.delete("/saved/{item_id}")
async def unsave_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    saved = db.query(SavedItem).filter(
        SavedItem.user_id == current_user.id,
        or_(SavedItem.id == item_id, SavedItem.item_id == item_id)
    ).first()

    bm = db.query(DiscussionBookmark).filter(
        DiscussionBookmark.user_id == current_user.id,
        DiscussionBookmark.discussion_id == item_id
    ).first()
    if bm:
        db.delete(bm)

    if saved:
        db.delete(saved)

    db.commit()
    await delete_cached(f"community:discussions:user:{current_user.id}:*")
    return {"success": True, "message": "Item unsaved"}


@router.get("/users/{user_identifier}/profile")
async def get_user_profile(
    user_identifier: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    if user_identifier.isdigit():
        user = db.query(User).filter_by(id=int(user_identifier)).first()
    else:
        user = db.query(User).filter(func.lower(User.username) == user_identifier.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_id = user.id
    is_owner = (current_user.id == user.id) if current_user else False
    hide_metrics = getattr(user, 'hide_public_metrics', False) and not is_owner

    posts_count = db.query(func.count(CommunityDiscussion.id)).filter(CommunityDiscussion.user_id == user_id).scalar() or 0
    replies_count = db.query(func.count(DiscussionReply.id)).filter(DiscussionReply.user_id == user_id).scalar() or 0
    build_room_contributions = posts_count + replies_count

    pots_earned = db.query(func.count(DiscussionLike.id)).join(
        CommunityDiscussion, DiscussionLike.discussion_id == CommunityDiscussion.id
    ).filter(CommunityDiscussion.user_id == user_id).scalar() or 0

    chops_gifted_sum = db.query(func.coalesce(func.sum(CommunityDiscussion.chops_gifted), 0)).filter(
        CommunityDiscussion.user_id == user_id
    ).scalar() or 0

    # Import badge computer
    from api.routes.user.stats import _compute_badges
    badges = _compute_badges(
        analyses_count=0,
        streak=user.login_streak or 0,
        missions_done=0,
        chops=user.total_chops or 0,
        build_room_contributions=build_room_contributions,
        chops_gifted=chops_gifted_sum,
        signal_contributions=0
    )

    # Pinned posts
    pinned_posts = []
    pinned_ids = getattr(user, 'pinned_profile_post_ids', None) or []
    if pinned_ids:
        discussions = db.query(CommunityDiscussion).filter(
            CommunityDiscussion.id.in_(pinned_ids),
            CommunityDiscussion.user_id == user_id
        ).all()
        for d in discussions:
            pinned_posts.append({
                "id": d.id,
                "title": d.title,
                "content": d.content[:140] if d.content else "",
                "channel": d.channel.name if d.channel else "general",
                "like_count": d.like_count or 0,
                "reply_count": d.reply_count or 0,
                "created_at": d.created_at.isoformat() if d.created_at else None
            })

    # Enforce expertise 5-item limit
    raw_expertise = getattr(user, "expertise", None) or ["Product design", "Community", "No-code", "Brand", "Growth loops"]
    expertise_limited = raw_expertise[:5] if isinstance(raw_expertise, list) else []

    return {"success": True, "data": {
        "id": user.id,
        "name": user.name or "Member",
        "email": user.email if is_owner else None,
        "company_name": user.company_name or "Lavoo Creators",
        "industry": user.industry or "Software",
        "location": getattr(user, "location", None) or "Lagos, NG",
        "venture_stage": getattr(user, "venture_stage", None) or "Pre-revenue",
        "bio": getattr(user, "bio", None),
        "total_chops": user.total_chops or 0,
        "post_count": posts_count,
        "subscription_status": user.subscription_status or "Free",
        "hide_public_metrics": getattr(user, "hide_public_metrics", False),
        "metrics_hidden": hide_metrics,
        "metrics": {
            "is_hidden": hide_metrics,
            "decision_score": 88 if not hide_metrics else None,
            "contribution_chops": user.total_chops or 0 if not hide_metrics else None,
            "day_streak": user.login_streak or 0 if not hide_metrics else None,
            "pots_earned": pots_earned if not hide_metrics else None,
        },
        "badges": badges[:5],
        "all_badges": badges,
        "expertise": expertise_limited,
        "open_to": getattr(user, "open_to", None) or ["Weekly decision swaps", "Co-founder conversations", "Beta testing partnerships", "Warm intros to creators"],
        "recent_wins": getattr(user, "recent_wins", None) or ["Crossed 40 activated beta founders", "Shipped the Build Room v2 prototype", "Featured as top contributor this month"],
        "pinned_posts": pinned_posts,
        "joined_at": user.created_at.isoformat() if getattr(user, "created_at", None) else None,
    }}


@router.get("/users/search")
async def search_community_users(
    q: str = Query("", min_length=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        search_str = f"%{q.strip().lower()}%"
        users = db.query(User).filter(
            or_(
                func.lower(User.name).like(search_str),
                func.lower(User.username).like(search_str),
                func.lower(User.email).like(search_str)
            )
        ).limit(10).all()

        results = []
        for u in users:
            username_val = u.username or re.sub(r'[^a-zA-Z0-9]', '', u.name.lower() if u.name else "user")
            results.append({
                "id": u.id,
                "name": u.name or "Member",
                "username": username_val,
                "avatar_url": u.avatar_url,
                "company_name": u.company_name or "Lavoo Creators"
            })

        return {"success": True, "users": results}
    except Exception as e:
        logger.error(f"Error searching community users: {e}")
        return {"success": True, "users": []}
